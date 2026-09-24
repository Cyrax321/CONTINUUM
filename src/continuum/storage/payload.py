"""Out-of-band storage for oversized event payloads (issue #254).

Event payloads above a configurable size are written to a content-addressed
blob file and replaced inline with a small marker carrying the digest. The
log row stays cheap, so the live log and the archive do not bloat with
gateway evidence bodies, reasoning summaries or OTel attribute bundles, and
replay latency stays bounded no matter how large one payload grows.

The pattern is the payload codec durable-execution platforms use (Temporal's
large-payload codec is the reference): keep a digest inline, store the bytes
out of band. The digest is the file's name, so the codec is its own
deduplicator -- two events with the same payload share one blob, and the same
row can be archived without moving any bytes, because the archived row points
at the same digest as the live one did.

The feature is off unless an operator turns it on with
``CONTINUUM_PAYLOAD_OFFLOAD_BYTES``. Below the threshold the serialized form a
row stores is byte-identical to what it stored before the codec existed, so
the hash chain and every existing reader are untouched.

Nothing here deletes blobs. Reclaiming space is operator-owned, same as the
archive itself: a blob file is the only copy of a payload, so automatic
collection would need a reachability scan that is wrong the moment a new
event with the same content arrives. An operator who deletes a blob the log
still references gets a ``CorruptedRecord`` naming the digest on read and a
``BLOB_MISSING`` violation from ``verify --deep`` -- never a silent empty
payload.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from continuum.storage.base import CorruptedRecord

__all__ = [
    "BLOB_DIGEST_MISMATCH",
    "BLOB_MISSING",
    "OFFLOAD_MARKER",
    "OFFLOAD_THRESHOLD_ENV",
    "BlobStore",
    "is_marker",
    "offload_threshold",
]

#: The environment key that opts the codec in. Zero or unset means off, which
#: is the default: the feature trades inline simplicity for out-of-band
#: durability, and that trade is the operator's to make.
OFFLOAD_THRESHOLD_ENV = "CONTINUUM_PAYLOAD_OFFLOAD_BYTES"

#: The inline marker a stored payload is replaced with. The name is the one
#: #254 specifies. ``keys`` records the original payload's own keys so a
#: reader can see what the blob holds without opening it.
OFFLOAD_MARKER = "__offloaded"

#: The two kinds of blob damage ``verify --deep`` reports.
BLOB_MISSING = "BLOB_MISSING"
BLOB_DIGEST_MISMATCH = "BLOB_DIGEST_MISMATCH"

#: A marker payload is exactly these three keys, and nothing else. The check
#: is what keeps a real payload that happens to use the marker name from
#: being read as a reference to a blob that does not exist.
_MARKER_KEYS = frozenset({OFFLOAD_MARKER, "keys", "bytes"})


def is_marker(payload: Any) -> bool:
    """Whether ``payload`` is the inline reference ``encode`` writes.

    Exact key set, not a substring test: a real payload that happens to use the
    marker name alongside other keys must not be read as a blob reference,
    either on rehydration or in a deep audit. Centralised so the read path and
    the audit path cannot drift apart on what counts as a marker.
    """
    return isinstance(payload, dict) and set(payload) == _MARKER_KEYS


def offload_threshold(environ: Mapping[str, str] | None = None) -> int:
    """The byte threshold above which a payload is stored out of band.

    ``environ`` defaults to the process environment and is a parameter so a
    test can set the threshold without mutating global state. Zero or a
    negative value disables the codec; a value that is not an integer disables
    it too, because silently ignoring a typo in the key would look like the
    feature working while it did nothing.
    """
    raw = (os.environ if environ is None else environ).get(OFFLOAD_THRESHOLD_ENV)
    if not raw:
        return 0
    try:
        # Clamped here rather than at every reader so a negative value means
        # the same thing everywhere: the codec is off.
        return max(int(raw), 0)
    except ValueError:
        return 0


class BlobStore:
    """Content-addressed store for payloads too large to keep inline.

    ``directory`` is the ``<db>.blobs`` directory the engine derives from its
    database path. ``None`` means the engine has no filesystem to side into
    (an in-memory SQLite database), and the codec stays a no-op there: an
    in-memory database is not durable in the first place, so out-of-band
    storage would add a second, separately-undurable copy of nothing.
    """

    def __init__(self, directory: Path | None, *, threshold: int | None = None) -> None:
        self._directory = directory
        self._threshold = offload_threshold() if threshold is None else max(int(threshold), 0)

    @property
    def directory(self) -> Path | None:
        """Where blobs live, or None when the codec cannot apply."""
        return self._directory

    @property
    def enabled(self) -> bool:
        """Whether ``encode`` will ever move a payload out of band."""
        return self._directory is not None and self._threshold > 0

    def _blob_path(self, digest: str) -> Path:
        assert self._directory is not None  # guarded by every public caller
        return self._directory / digest

    def encode(self, payload: Mapping[str, Any]) -> str:
        """Serialize ``payload`` for a row, offloading it if it is oversized.

        Returns the JSON text the row stores. Under the threshold -- or when
        the codec is off -- that is exactly what the row stored before the
        codec existed, so nothing downstream notices. Over it, the blob is
        written *first* and the marker second: a crash between the two leaves
        an orphaned blob (disk waste an operator can collect) rather than a
        row pointing at a blob that was never written, which is unreadable.
        """
        text = json.dumps(dict(payload), sort_keys=True)
        if not self.enabled or len(text.encode("utf-8")) <= self._threshold:
            return text
        return self._write_blob(payload, text)

    def _write_blob(self, payload: Mapping[str, Any], text: str) -> str:
        assert self._directory is not None
        blob = text.encode("utf-8")
        digest = hashlib.sha256(blob, usedforsecurity=False).hexdigest()
        path = self._blob_path(digest)
        if not path.exists():
            # The directory is made here rather than in __init__ so a store
            # that never offloads never creates one, and so the codec is a
            # pure no-op while the feature is off.
            self._directory.mkdir(parents=True, exist_ok=True)
            # Write-then-rename so a reader never observes a half-written
            # blob: the digest names the finished content, so a truncated file
            # under the final name would rehydrate to a payload whose hash the
            # chain no longer matches.
            tmp = self._directory / f".{digest}.tmp"
            tmp.write_bytes(blob)
            os.replace(tmp, path)
        return json.dumps(
            {
                OFFLOAD_MARKER: digest,
                "keys": sorted(str(k) for k in payload),
                "bytes": len(blob),
            },
            sort_keys=True,
        )

    def decode(self, payload: Any) -> Any:
        """Restore a payload ``encode`` may have replaced with its marker.

        A payload that is not a marker is returned unchanged, so every reader
        works on un-offloaded and offloaded stores alike. Off entirely, no
        marker is ever interpreted: a store that never opted in keeps
        round-tripping a payload that happens to use the marker keys. A marker
        whose blob cannot be read raises ``CorruptedRecord`` naming the
        digest, because returning an empty payload instead would let a run
        resume having silently lost the event that described what it did.
        """
        if not self.enabled or not is_marker(payload):
            return payload
        digest = str(payload[OFFLOAD_MARKER])
        path = self._blob_path(digest)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            raise CorruptedRecord(
                f"payload blob {digest} is missing: the event's contents were "
                "stored out of band and the blob file is gone. Restore it or "
                "repair from a backup; the payload cannot be reconstructed "
                "from the log."
            ) from None
        except OSError as exc:
            raise CorruptedRecord(f"payload blob {digest} is unreadable: {exc}") from exc
        try:
            return json.loads(blob)
        except json.JSONDecodeError as exc:
            raise CorruptedRecord(f"payload blob {digest} is not valid JSON: {exc}") from exc

    def audit(self, digest: str) -> str | None:
        """Check one referenced blob, returning the violation kind or None.

        Reads the raw bytes rather than going through :meth:`decode` so an
        audit reports the blob's own damage as a named violation instead of
        the generic unreadable-record the chain walk would file it under.
        """
        if self._directory is None:
            return None
        path = self._blob_path(digest)
        try:
            blob = path.read_bytes()
        except FileNotFoundError:
            return BLOB_MISSING
        except OSError:
            return BLOB_MISSING
        if hashlib.sha256(blob, usedforsecurity=False).hexdigest() != digest:
            return BLOB_DIGEST_MISMATCH
        return None
