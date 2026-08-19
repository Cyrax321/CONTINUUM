"""Which MCP callers may change a run.

The problem this solves is coexistence, not intrusion. Several agents can be
configured against the same database at once — Kilo, Gemini CLI and Claude Code
have all pointed at this project's ``continuum.db`` simultaneously — and until
now any of them could overwrite another's progress, checkpoint over its state,
or claim its actions. This layer keeps honestly-named agents out of each other's
runs.

It is a security boundary only when authentication is turned on. By default
``clientInfo`` is asserted by the client during the initialize handshake and
never verified, so a caller that wants to be called ``claude-code`` simply says
so. What the transport does guarantee is that the name is fixed at connection
time and injected server-side: a caller cannot elevate itself mid-session by
passing a forged ``clientInfo`` in tool arguments. That is enough to separate
cooperating agents, and not enough to stop a hostile one on its own.

When ``CONTINUUM_MCP_TOKEN`` is set, the server verifies one shared secret the
client presents in the handshake's ``_meta.authToken``. Per-client credentials
are available via ``CONTINUUM_MCP_CLIENT_TOKENS`` (``name:secret`` pairs): each
caller's secret is bound to the identity it claims, so a token issued to one
client cannot be replayed by another. A caller that cannot prove possession of
the expected secret (for its own name, under per-client mode) is refused every
mutating tool regardless of the name it claims, which is what turns "cooperating
agents kept apart" into "hostile caller stopped". The check is fail-closed: a
missing, empty, or mismatched secret always refuses, and an unset secret leaves
authentication disabled so the default local, single-user, no-account behavior
is unchanged. A hostile process with direct filesystem access to the database
can still edit it without the server, which is outside this layer's scope.

Read-only tools stay open
-------------------------

Only mutating tools are gated. ``validate``, ``resume`` and ``list_actions``
cannot alter a run, and their whole value is that anyone can ask "is this safe
to continue?" without first being granted permission. Gating them would also
leave an unlisted caller unable to discover *why* its writes are failing.

The split is driven by the ``read_only_hint`` annotation each tool already
declares, rather than a second hand-maintained list that could drift out of
step with it.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "AuthorizationPolicy",
    "NotAuthorized",
    "UnknownCaller",
    "AuthPolicy",
    "NotAuthenticated",
    "POLICY_ENV_VAR",
    "POLICY_ENV_VAR_ALIAS",
    "POLICY_FILENAME",
    "AUTH_ENV_VAR",
    "load_policy",
    "load_auth",
    "caller_name",
    "token_from",
    "CLIENT_TOKENS_ENV_VAR",
]

POLICY_ENV_VAR = "CONTINUUM_MCP_ALLOW"

#: Alias for ``POLICY_ENV_VAR``, preserved from the closed PR #3. The longer
#: name states what is being allowed rather than leaving it to be inferred, so
#: it wins when both are set — a reader who followed that PR's history will
#: reach for it first, and silently preferring the vaguer name would surprise
#: them. Same precedence position: an alias, not an extra config source.
POLICY_ENV_VAR_ALIAS = "CONTINUUM_MCP_MUTATING_CLIENTS"

POLICY_FILENAME = ".continuum/mcp-policy.json"

#: Used when the handshake supplied no client name at all.
UNKNOWN_CALLER = "<unidentified>"


class NotAuthorized(PermissionError):
    """A caller attempted a mutating tool it is not permitted to use."""


class UnknownCaller(NotAuthorized):
    """The connection never identified itself, so nothing can be authorized."""


class NotAuthenticated(PermissionError):
    """The caller did not prove possession of the expected shared secret."""


class AuthPolicy:
    """Verifies that a caller possesses the expected shared secret.

    Disabled by default: with no expected secret configured, ``verify`` is a
    no-op and the server behaves as before (authorization by declared name
    only). When a secret is configured, every mutating call must present it
    through the handshake's ``_meta.authToken``, or it is refused.

    The check is fail-closed on purpose. The closed PR #3 is the cautionary
    tale: its guard authorized the caller after catching a ``ValueError``, so
    a malformed request fell through to "allowed". Here a missing, empty, or
    mismatched secret always refuses, and a misconfigured empty secret refuses
    rather than opening the door. No exception path resolves in the caller's
    favour.
    """

    __slots__ = ("expected", "tokens", "source")

    def __init__(
        self,
        expected: str | None = None,
        *,
        tokens: Mapping[str, str] | None = None,
        source: str = "default",
    ) -> None:
        self.expected = expected
        self.tokens = dict(tokens) if tokens else None
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        mode = "disabled" if self.disabled else ("per-client" if self.tokens else "shared-secret")
        return f"AuthPolicy(mode={mode}, source={self.source!r})"

    @property
    def disabled(self) -> bool:
        # Only an absent secret disables authentication. An explicit empty
        # secret is a misconfiguration and must refuse, not open the door.
        return self.expected is None and not self.tokens

    def verify(self, caller: str | None, token: str | None) -> None:
        """Raise ``NotAuthenticated`` unless ``token`` proves the secret.

        ``caller`` is the declared name (used for per-client secrets); it is
        never trusted on its own. Any failure refuses the call.
        """
        if self.disabled:
            return
        expected: str | None
        if self.tokens is not None:
            if caller not in self.tokens:
                raise NotAuthenticated(f"caller {caller!r} is not registered for authentication")
            expected = self.tokens[caller]
        else:
            expected = self.expected
        # An empty expected secret cannot be presented, so it must refuse.
        if not expected or not token or token != expected:
            raise NotAuthenticated("the caller did not present the expected shared secret")


AUTH_ENV_VAR = "CONTINUUM_MCP_TOKEN"

#: Per-client credentials: a ``name:secret`` mapping (whitespace or comma
#: separated) that binds each caller's shared secret to the identity it claims,
#: so a token issued to one client cannot be replayed by another (issue #7).
CLIENT_TOKENS_ENV_VAR = "CONTINUUM_MCP_CLIENT_TOKENS"


def _parse_client_tokens(value: str | None) -> dict[str, str] | None:
    """Parse ``CONTINUUM_MCP_CLIENT_TOKENS`` into ``{name: secret}``."""
    if not value:
        return None
    tokens: dict[str, str] = {}
    for part in value.replace(",", " ").split():
        name, sep, secret = part.partition(":")
        if not sep:
            raise ValueError(f"{CLIENT_TOKENS_ENV_VAR} entries must be 'name:secret', got {part!r}")
        name, secret = name.strip(), secret.strip()
        if not name or not secret:
            raise ValueError(
                f"{CLIENT_TOKENS_ENV_VAR} entry {part!r} needs a non-empty name and secret"
            )
        tokens[name] = secret
    return tokens or None


def load_auth(
    expected: str | None = None,
    *,
    tokens: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
) -> AuthPolicy:
    """Resolve the authentication policy.

    Precedence: explicit ``tokens`` argument, then the
    ``CONTINUUM_MCP_CLIENT_TOKENS`` environment variable (per-client secrets),
    then the explicit ``expected`` argument, then the ``CONTINUUM_MCP_TOKEN``
    environment variable (a single shared secret), then disabled. An empty
    variable is treated as unset, so a blank configuration is the same as "no
    authentication".
    """
    if tokens is not None:
        return AuthPolicy(None, tokens=tokens, source="argument")
    if expected is not None:
        return AuthPolicy(expected, source="argument")
    environ = os.environ if env is None else env
    client_tokens = _parse_client_tokens(environ.get(CLIENT_TOKENS_ENV_VAR))
    if client_tokens:
        return AuthPolicy(None, tokens=client_tokens, source=CLIENT_TOKENS_ENV_VAR)
    value = environ.get(AUTH_ENV_VAR)
    if value:
        return AuthPolicy(value, source=AUTH_ENV_VAR)
    return AuthPolicy(source="default (disabled)")


def token_from(context: Any) -> str | None:
    """Read the shared secret a client presented in the initialize handshake.

    Carried in ``_meta.authToken`` of the ``initialize`` request params, which
    the transport injects server-side and the caller cannot forge through tool
    arguments. Returns ``None`` when no context or no token is present.
    """
    if context is None:
        return None
    try:
        params = context.session.client_params
    except AttributeError:
        return None
    meta = getattr(params, "meta", None)
    if not isinstance(meta, dict):
        return None
    token = meta.get("authToken")
    return str(token) if token else None


class AuthorizationPolicy:
    """Decides whether a named caller may invoke a mutating tool.

    Deny by default. An unlisted caller is not a caller we have decided to
    trust — it is one nobody has made a decision about, and treating an absent
    decision as approval is how the whole point of the layer gets lost. This
    mirrors the validator's stance elsewhere in CONTINUUM: uncertainty degrades
    rather than resolving in its own favour.
    """

    __slots__ = ("allowed", "source")

    def __init__(self, allowed: Iterable[str] = (), *, source: str = "default") -> None:
        self.allowed = frozenset(n.strip() for n in allowed if n and n.strip())
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        listed = ", ".join(sorted(self.allowed)) or "(none)"
        return f"AuthorizationPolicy(allowed=[{listed}], source={self.source!r})"

    @property
    def denies_everything(self) -> bool:
        return not self.allowed

    def permits(self, caller: str | None) -> bool:
        """Whether ``caller`` may invoke mutating tools."""
        if not caller:
            return False
        return caller in self.allowed

    def require(self, caller: str | None, tool: str) -> None:
        """Raise unless ``caller`` may invoke the mutating tool ``tool``."""
        if caller:
            if caller in self.allowed:
                return
            raise NotAuthorized(
                f"caller {caller!r} is not permitted to use the mutating tool "
                f"{tool!r}. {self._remedy(caller)}"
            )
        raise UnknownCaller(
            f"the connection did not identify itself, so the mutating tool "
            f"{tool!r} is refused. {self._remedy(None)}"
        )

    def _remedy(self, caller: str | None) -> str:
        name = caller or "<your-client-name>"
        if self.denies_everything:
            base = "No callers are currently permitted to make changes."
        else:
            base = f"Permitted callers: {', '.join(sorted(self.allowed))}."
        return (
            f"{base} Read-only tools remain available. To grant access, set "
            f"{POLICY_ENV_VAR_ALIAS}={name!r} or add it to {POLICY_FILENAME}."
        )


def _from_env(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in value.replace(",", " ").split() if part]


def _from_file(path: Path) -> tuple[list[str], str] | None:
    """Read an allowlist from ``path``. Returns ``None`` when absent.

    A malformed policy file raises rather than falling back to the default. A
    file that exists is a deliberate statement of intent; silently ignoring a
    typo in it and denying everything would be baffling, and silently ignoring
    it and *allowing* everything would be dangerous.
    """
    if not path.is_file():
        return None
    try:
        data: Any = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read MCP policy at {path}: {exc}") from exc

    if isinstance(data, list):
        names = data
    elif isinstance(data, Mapping):
        names = data.get("allow", data.get("allowed", []))
    else:
        raise ValueError(
            f'MCP policy at {path} must be a list of client names or an object with an "allow" key'
        )
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError(f"MCP policy at {path}: 'allow' must be a list of strings")
    return list(names), str(path)


def load_policy(
    allow: Iterable[str] | None = None,
    *,
    root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AuthorizationPolicy:
    """Resolve the policy: explicit argument, then env var, then file, then deny.

    Each source replaces the ones below it rather than merging, so a caller can
    always see exactly where a grant came from by reading ``policy.source``.
    """
    if allow is not None:
        return AuthorizationPolicy(allow, source="argument")

    environ = os.environ if env is None else env
    for var in (POLICY_ENV_VAR_ALIAS, POLICY_ENV_VAR):
        from_env = _from_env(environ.get(var))
        if from_env:
            return AuthorizationPolicy(from_env, source=var)

    base = Path.cwd() if root is None else root
    found = _from_file(base / POLICY_FILENAME)
    if found is not None:
        names, source = found
        return AuthorizationPolicy(names, source=source)

    return AuthorizationPolicy((), source="default (deny)")


def caller_name(context: Any) -> str | None:
    """Extract the client's declared name from an MCP request context.

    Read from the initialize handshake, which the transport injects server-side.
    A caller cannot override it by passing ``clientInfo`` in tool arguments —
    verified by test. It is still only what the client *claims* to be.
    """
    if context is None:
        return None
    try:
        info = context.session.client_params.client_info
    except AttributeError:
        return None
    name = getattr(info, "name", None)
    return str(name) if name else None
