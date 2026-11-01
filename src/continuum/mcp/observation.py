"""Which client names actually connected, recorded beside the database.

``mcp install`` bakes one client name per host into a registration and never
learns what the host sends in the handshake, so a wrong guess is invisible:
install succeeds, the server starts, and the agent silently keeps only the
read-only tools. The only place the truth exists is the ``initialize`` request
the host sends, which the server reads to authorize a call and then discards.
This module is where that value is kept instead.

Two properties make keeping it safe, and both are why this lives in a side
file rather than beside the run data:

- **It is self-asserted.** Whoever connects chooses the string, so anyone can
  claim to be ``cursor-vscode``. It is therefore recorded and reported, never
  consulted: authorization is decided by the allowlist in ``authz`` and by
  nothing else. A caller whose name appears here is refused unless the
  allowlist independently lists it.
- **The event log is the trust anchor.** A run's hash-chained events carry
  attested facts, and agent-reported ones already have to be marked
  ``Origin.EXTERNAL_AGENT``. Appending a self-asserted name to that chain
  would launder a claim into an attested fact, so the observation goes to its
  own file under ``.continuum/`` instead, where nothing hashes it and nothing
  replays it.

Pure standard library, and it reads like the rest of the MCP package: the
doctor has to work on a machine where the ``mcp`` extra is missing, so it must
not drag the server (or the storage layer) in to learn a filename.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = [
    "MAX_OBSERVED_CLIENTS",
    "MAX_OBSERVED_NAME_LENGTH",
    "OBSERVATION_DIRNAME",
    "OBSERVATION_FILENAME",
    "declared_client_name",
    "observation_path",
    "read_observed_clients",
    "record_observed_client",
]

#: The side file's directory, beside the database. ``.continuum/`` is already
#: the project's MCP side-file home (``authz`` reads its policy from there),
#: so the observation lands where an operator would look for it and is never
#: mistaken for run data.
OBSERVATION_DIRNAME = ".continuum"

#: The side file's name.
OBSERVATION_FILENAME = "mcp-client-observations.json"

#: Bumped when the shape changes, so a reader can tell an old file from a
#: broken one instead of parsing either as the current format. 2: names are
#: recorded verbatim (#1805); a file written by the 1 code that split a
#: declared name on allowlist separators may hold fragments no host sent, so
#: it is discarded rather than carried into the doctor's report.
SCHEMA_VERSION = 2

#: A client name arrives from an untrusted peer, so it is bounded before it
#: reaches the filesystem: longer than any real host needs, short enough that a
#: hostile client cannot fill the file with megabytes of its own choosing.
MAX_OBSERVED_NAME_LENGTH = 128

#: How many distinct names one database remembers. A workstation has a handful
#: of hosts; a bound keeps a client that randomizes its name per connection
#: from growing the file without limit, evicting the least recently seen first.
MAX_OBSERVED_CLIENTS = 32


def _normalized_name(name: Any) -> str | None:
    """One declared name, stripped and bounded, or ``None`` when unusable.

    The bound is what keeps an untrusted peer's string off the filesystem as
    more than a bounded number of bytes. A name that is blank after stripping
    or past the length bound is unusable rather than truncatable: a truncated
    name is a name the host never sent, which is exactly the guess this
    module's contract says it must never make.
    """
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text or len(text) > MAX_OBSERVED_NAME_LENGTH:
        return None
    return text


def observation_path(database: str) -> Path:
    """Where the observed client names for ``database`` are recorded.

    Scoped to the database's *directory*, not its filename, so the record is
    per project rather than per database file: ``.continuum/`` is the project's
    side-file home, and "which hosts have connected to this project" is the
    question the doctor needs answered. Two databases in one directory share
    the record, which is the same answer to that question.

    Derived from the database rather than the cwd because a host-spawned
    server's cwd is neither documented nor guaranteed to be the project root,
    which is the same reason ``install`` bakes an absolute ``--db``. The
    database is the thing the host actually opens, so its directory is where
    the record belongs.
    """
    return Path(database).resolve().parent / OBSERVATION_DIRNAME / OBSERVATION_FILENAME


def declared_client_name(params: Any) -> str | None:
    """The ``clientInfo.name`` an ``initialize`` request declares, or ``None``.

    Reads the raw inbound params a middleware is handed, which the SDK has not
    validated yet, so every shape is tolerated: a missing ``clientInfo``, a
    non-mapping one, a non-string name, a blank name, or one past the length
    bound. An unusable declaration records nothing, which is the honest
    outcome: the doctor must see "unknown", never a guess.
    """
    if not isinstance(params, dict):
        return None
    info = params.get("clientInfo")
    if not isinstance(info, dict):
        return None
    return _normalized_name(info.get("name"))


def read_observed_clients(database: str) -> dict[str, dict[str, Any]]:
    """The client names recorded for ``database``, or ``{}`` when unusable.

    Fails toward unknown in every case: absent file, unreadable file, malformed
    JSON, a different schema, or a value of the wrong shape all yield nothing
    rather than raising. Callers must read an empty result as "no host has been
    observed", never as permission; nothing here can grant anything, because
    the function only returns what an earlier connection wrote down.

    A single entry of an unexpected shape is skipped rather than discarding
    the whole file, so one corrupt key does not erase the rest of the history.
    """
    try:
        raw = observation_path(database).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        data: Any = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return {}
    clients = data.get("observed_clients")
    if not isinstance(clients, dict):
        return {}
    return {
        name: entry
        for name, entry in clients.items()
        if isinstance(name, str) and isinstance(entry, dict)
    }


def record_observed_client(database: str, name: str) -> None:
    """Record the ``clientInfo.name`` one connection declared. Best effort.

    Never raises and never blocks a handshake: a connection that cannot be
    recorded is still served, because losing the observation is a diagnostic
    gap while refusing the connection would be an outage. Writes are atomic, so
    a failure leaves the previous contents intact rather than a truncated file
    a reader would have to treat as corruption.

    ``name`` is one connection's declared name and is recorded verbatim. A
    host's name is whatever the host sent: splitting it on the separators an
    allowlist uses (which is how ``authz`` reads a value that names several
    authorized callers) would record names the host never declared. One
    connection from a host named ``"Visual Studio Code"`` used to fill the
    record with three names no host sent, and the doctor then reported a
    mismatch against clients that do not exist; the name is also validated the
    same way :func:`declared_client_name` validates it, so an unusable value
    records nothing rather than a bounded fragment of itself.
    """
    declared = _normalized_name(name)
    if declared is None:
        return
    path = observation_path(database)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        clients = read_observed_clients(database)
        stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        # Eviction order comes from an explicit counter, not the timestamp.
        # The stamp has one-second resolution, so a burst of connections lands
        # on identical values and "least recently seen" would be decided by
        # whatever order the file happened to be written in.
        sequence = max((_sequence_of(entry) for entry in clients.values()), default=0)
        entry = clients.get(declared)
        count = entry.get("count") if isinstance(entry, dict) else None
        sequence += 1
        clients[declared] = {
            "count": (count if isinstance(count, int) and count > 0 else 0) + 1,
            "last_seen": stamp,
            "seq": sequence,
        }
        if len(clients) > MAX_OBSERVED_CLIENTS:
            clients = dict(
                sorted(
                    clients.items(),
                    key=lambda item: _sequence_of(item[1]),
                    reverse=True,
                )[:MAX_OBSERVED_CLIENTS]
            )
        payload = json.dumps(
            {"schema": SCHEMA_VERSION, "observed_clients": clients}, indent=2, sort_keys=True
        )
        _replace_atomically(path, payload)
    except OSError:
        return


def _sequence_of(entry: Any) -> int:
    """An entry's ordering number, treating a hand-edited one as oldest.

    The file is readable and writable by the operator, so its contents are
    input rather than state this module can assume. A missing or nonsensical
    sequence sorts to the front and is therefore the first evicted, never a
    comparison that raises mid-write.
    """
    if isinstance(entry, dict):
        sequence = entry.get("seq")
        if isinstance(sequence, int):
            return sequence
    return 0


def _replace_atomically(path: Path, payload: str) -> None:
    """Write ``payload`` so a concurrent reader sees the old or new file, never half.

    The temporary file lives in the destination directory so the rename stays
    within one filesystem, which is the only thing that makes ``os.replace``
    atomic rather than a copy that can be interrupted half way.
    """
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".observed-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        # A failed observation must leave no debris beside the database.
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
