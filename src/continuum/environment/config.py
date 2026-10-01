"""Configured environment providers: discovery without untrusted execution.

:mod:`continuum.environment.snapshot` ships providers for files, git, values,
callables and static inputs, but nothing applied them on resume unless the
caller remembered to pass them in. An integration that wires a reliable
world-observer into capture could still resume through a path that validated
only what the caller supplied, and nothing in the output would say so
(issue #762).

This module closes that gap without opening a larger one. A run *configures*
providers by name with a bounded resource scope; on resume the engine resolves
only built-in or explicitly registered providers, captures the world with them,
and feeds the result to the same validator that already existed. There is no
plugin auto-discovery, no import of untrusted code, and no execution of
configuration: a provider identifier is a name looked up in a table the host
process controls, nothing more.

Fail-closed is the whole point of the design. An unavailable, disabled,
malformed, conflicting or raising provider never shrinks what validation
covers: every resource it declared is emitted as ``UNKNOWN_VERSION`` with a
diagnostic, and ``UNKNOWN`` is the state the validator already downgrades on.
A provider that cannot speak must not be heard as silence. Concretely, the
failure modes and what each one produces:

``unavailable``  no such built-in and no registration under the name.
``disabled``     the spec was recorded with ``enabled=False``.
``malformed``    the config or the provider's own parameters were rejected.
``conflict``     another spec declared the same resource key.
``failed``       ``capture()`` raised, which providers contractually never do.
``undeclared``   the provider returned a key outside its declared scope.

Configuration is append-only. Each ``ENVIRONMENT_PROVIDERS_CONFIGURED`` event
records the complete set, so the newest one is authoritative and a later change
cannot rewrite what an earlier checkpoint trusted. Compaction cannot hide it,
because resume reads the archived prefix too (issue #553's family).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from continuum.environment.snapshot import (
    UNKNOWN_VERSION,
    ConfigurableProvider,
    EnvironmentProvider,
    FileProvider,
    GitProvider,
    StaticProvider,
    ValueProvider,
    capture,
)
from continuum.events import Event, EventType
from continuum.models import EnvResource

__all__ = [
    "ProviderSpec",
    "ProviderConfig",
    "ProviderRegistry",
    "ProviderStatus",
    "ProviderDiagnostic",
    "ConfiguredCapture",
    "BUILTIN_PROVIDER_NAMES",
    "SCHEMA_VERSION",
    "resolve_and_capture",
    "config_from_events",
    "record_provider_config",
]

#: Configuration model version. Bumped only when the recorded shape changes in
#: a way an older reader cannot ignore; the loader refuses a mismatch rather
#: than guessing at a foreign schema.
SCHEMA_VERSION = 1

#: Event payload key carrying the recorded configuration.
CONFIG_PAYLOAD_KEY = "providers"

#: Provider identifiers a run may configure without any registration. Anything
#: else must be handed to a :class:`ProviderRegistry` in the host process,
#: which is how a callable probe becomes discoverable without becoming
#: serializable.
BUILTINS: dict[str, type[ConfigurableProvider]] = {
    FileProvider.name: FileProvider,
    GitProvider.name: GitProvider,
    ValueProvider.name: ValueProvider,
    StaticProvider.name: StaticProvider,
}

BUILTIN_PROVIDER_NAMES = frozenset(BUILTINS)

# A provider name is a lookup key into a table the host controls, not a path or
# a module reference, so it stays a plain identifier.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Resource keys are compared verbatim against checkpointed snapshots, so any
# non-empty string of sane length is admissible; leading/trailing whitespace is
# rejected because it is how a key silently stops matching itself. Absolute file
# paths are legitimate keys and routinely exceed 128 characters, so the bound is
# a sanity guard, not a tight one.
_RESOURCE_MAX = 1024
_RESOURCE_RE = re.compile(rf"^[^\s].{{0,{_RESOURCE_MAX - 1}}}$")

# A configuration is persisted in the event log, which is hashed and audited.
# Refusing to record a parameter *by name* means a secret never reaches the log
# at all, so there is nothing to redact later.
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|token|credential|apikey|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)

_MAX_RESOURCES = 64
_MAX_PARAMS = 32
_MAX_PROVENANCE = 256

_JSON_SCALARS = (str, int, float, bool, type(None))


def _check_json_native(value: Any, *, path: str) -> None:
    """Reject anything that would not survive a storage round-trip.

    A callable in a parameter would be silently dropped on persistence and
    quietly executed in memory, which is the two-faced failure this whole
    design exists to avoid: the same configuration would mean different things
    before and after a restart. Raises ``ValueError`` so a caller that wants to
    treat a bad configuration as data can catch one type.
    """
    if isinstance(value, _JSON_SCALARS):
        return
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_PARAMS:
            raise ValueError(f"{path} has {len(value)} entries, max {_MAX_PARAMS}")
        for index, item in enumerate(value):
            _check_json_native(item, path=f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} has a non-string key {key!r}")
            _check_json_native(item, path=f"{path}.{key}")
        return
    raise ValueError(
        f"{path} is {type(value).__name__}, expected a JSON-native value "
        "(providers must be configured by name, not by object)"
    )


class ProviderSpec(BaseModel):
    """One configured provider: an identifier plus the resources it owns.

    ``resources`` is the fail-closed contract. It is knowable without executing
    the provider, so a provider that cannot be reached still has its resources
    reported as ``UNKNOWN`` instead of vanishing from validation. Built-ins can
    derive it from their parameters; a spec may declare it explicitly to bound
    a provider that could otherwise report anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    resources: frozenset[str] = frozenset()
    params: Mapping[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    #: Who or what recorded the spec: a CLI invocation, an adapter, a run
    #: bootstrap. Diagnostic only; it never selects a code path.
    provenance: str = ""

    @field_validator("provider")
    @classmethod
    def _provider_is_a_plain_name(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                f"provider {value!r} is not a plain identifier (letters, digits, "
                "'.', '_' or '-', max 64 chars); a provider name is looked up, "
                "never imported or executed"
            )
        return value

    @field_validator("resources")
    @classmethod
    def _resources_are_bounded_keys(cls, value: frozenset[str]) -> frozenset[str]:
        if len(value) > _MAX_RESOURCES:
            raise ValueError(f"a provider may own at most {_MAX_RESOURCES} resources")
        for key in value:
            if not isinstance(key, str) or not _RESOURCE_RE.match(key):
                raise ValueError(
                    f"resource key {key!r} must be non-empty, must not start with "
                    f"whitespace, and must be at most {_RESOURCE_MAX} characters"
                )
        return value

    @field_validator("params")
    @classmethod
    def _params_are_json_native_and_secret_free(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        if len(value) > _MAX_PARAMS:
            raise ValueError(f"a provider accepts at most {_MAX_PARAMS} parameters")
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"a parameter name must be a non-empty string, got {key!r}")
            if _SECRET_KEY_RE.search(key):
                # Never recorded, so never hashed into the log, so never leaked
                # by a log dump or a checkpoint dump.
                raise ValueError(
                    f"parameter {key!r} looks like a secret; configure it out of "
                    "band, never in the event log"
                )
            _check_json_native(item, path=f"params.{key}")
        return value

    @field_validator("provenance")
    @classmethod
    def _provenance_is_bounded(cls, value: str) -> str:
        if len(value) > _MAX_PROVENANCE:
            raise ValueError(f"provenance is at most {_MAX_PROVENANCE} characters")
        return value

    def declared_resources(self) -> frozenset[str]:
        """Resources this spec owns, deriving them when it declares none.

        Derivation never executes the provider: built-ins compute their keys
        from their parameters alone. An empty result with no derivation is a
        spec that owns nothing, which cannot fail closed for anything and is
        therefore malformed rather than useless.
        """
        if self.resources:
            return self.resources
        builtin = BUILTINS.get(self.provider)
        if builtin is None:
            return frozenset()
        try:
            derived = builtin.scope_of(self.params)
        except Exception:
            # A provider that cannot say what it owns cannot be bounded, so the
            # caller must declare the scope explicitly. Reported as malformed by
            # the caller, not raised here: a spec is data, and data must not be
            # able to crash a resume.
            return frozenset()
        return derived


class ProviderConfig(BaseModel):
    """The whole, versioned configuration for one run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION)
    specs: tuple[ProviderSpec, ...] = ()

    @field_validator("schema_version")
    @classmethod
    def _known_schema(cls, value: int) -> int:
        if value != SCHEMA_VERSION:
            raise ValueError(
                f"provider configuration schema version {value} is unsupported "
                f"(this build understands {SCHEMA_VERSION})"
            )
        return value

    def conflicts(self) -> Mapping[str, tuple[ProviderSpec, ...]]:
        """Resource keys owned by more than one spec, in deterministic order.

        A collision is not resolved by order: whichever spec won would silently
        demote the other's observation, and the losing provider's resources
        would quietly leave validation. Both specs fail closed instead.
        """
        owners: dict[str, list[ProviderSpec]] = {}
        for spec in self.specs:
            for key in sorted(spec.declared_resources()):
                owners.setdefault(key, []).append(spec)
        return {key: tuple(specs) for key, specs in owners.items() if len(specs) > 1}


class ProviderStatus(StrEnum):
    """What happened to one configured provider at capture time."""

    OK = "ok"
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
    FAILED = "failed"
    UNDECLARED = "undeclared"

    @property
    def blocking(self) -> bool:
        """Whether this status left the provider's resources untrustworthy.

        ``UNDECLARED`` is not blocking: the surplus keys are already reported as
        unknown rather than trusted, so it is a boundary warning, not a gap.
        """
        return self in {
            ProviderStatus.DISABLED,
            ProviderStatus.UNAVAILABLE,
            ProviderStatus.MALFORMED,
            ProviderStatus.CONFLICT,
            ProviderStatus.FAILED,
        }


@dataclass(frozen=True, slots=True)
class ProviderDiagnostic:
    """One provider's outcome at capture, for evidence and JSON diagnostics."""

    provider: str
    status: ProviderStatus
    resources: frozenset[str]
    detail: str

    def render(self) -> str:
        """One human-readable line, stable across runs."""
        keys = ", ".join(sorted(self.resources)) or "(none)"
        return f"[{self.status.value}] {self.provider}: {self.detail} ({keys})"


@dataclass(frozen=True, slots=True)
class ConfiguredCapture:
    """A snapshot plus the per-provider record of how it was produced."""

    snapshot: Any
    diagnostics: tuple[ProviderDiagnostic, ...] = ()

    @property
    def fail_closed(self) -> bool:
        """Whether any provider did not report its resources cleanly.

        ``UNDECLARED`` is excluded: it is a warning that a provider returned
        more than it was given, and the extra keys are already reported as
        ``UNKNOWN`` rather than trusted.
        """
        return any(d.status.blocking for d in self.diagnostics)

    def diagnostics_for(self, key: str) -> tuple[ProviderDiagnostic, ...]:
        """Every diagnostic touching one resource key."""
        return tuple(d for d in self.diagnostics if key in d.resources)


class ProviderRegistry:
    """Resolves configured provider identifiers to live providers.

    Only names the host process handed over are resolvable, so a configuration
    is a claim about providers the deployment already trusts, not an
    instruction to load one. A :class:`~continuum.environment.snapshot.CallableProvider`
    reaches the table this way: it is registered as an object, never serialized,
    and a configuration references it by name.
    """

    def __init__(self) -> None:
        self._providers: dict[str, EnvironmentProvider] = {}

    def register(self, name: str, provider: EnvironmentProvider) -> str:
        """Make ``provider`` resolvable as ``name``. Returns the name.

        Overwriting an existing name is allowed and returns the same name: a
        host re-registering a probe between runs is a normal redeploy, and a
        second registration is not a conflict, because the name is not
        authoritative for anything until a configuration uses it.
        """
        if not _NAME_RE.match(name):
            raise ValueError(f"provider name {name!r} is not a plain identifier")
        if not isinstance(provider, EnvironmentProvider):
            raise TypeError(
                f"{name!r} must be an EnvironmentProvider, got {type(provider).__name__}"
            )
        self._providers[name] = provider
        return name

    def unregister(self, name: str) -> None:
        self._providers.pop(name, None)

    def __contains__(self, name: object) -> bool:
        return name in self._providers

    def __len__(self) -> int:
        return len(self._providers)

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def registered(self, name: str) -> EnvironmentProvider | None:
        """A provider registered under ``name``, or ``None`` when none was."""
        return self._providers.get(name)


def _resolve(
    spec: ProviderSpec, registry: ProviderRegistry
) -> tuple[EnvironmentProvider | None, ProviderStatus, str]:
    """Resolve one spec, reporting *why* it failed when it did.

    Distinguishing ``MALFORMED`` from ``UNAVAILABLE`` matters to an operator:
    the first is a configuration error this deployment can fix, the second is a
    missing registration or a typo in a name.
    """
    factory = BUILTINS.get(spec.provider)
    if factory is not None:
        try:
            return factory.from_config(spec.params), ProviderStatus.OK, ""
        except Exception as exc:
            return None, ProviderStatus.MALFORMED, f"{type(exc).__name__}: {exc}"
    registered = registry.registered(spec.provider)
    if registered is not None:
        return registered, ProviderStatus.OK, ""
    return None, ProviderStatus.UNAVAILABLE, ""


def _unknown(key: str, *, provider: str, status: ProviderStatus, detail: str) -> EnvResource:
    """A resource a provider could not vouch for, stamped with why.

    ``UNKNOWN_VERSION`` never compares equal to a real version, so this is the
    mechanism that turns an unreachable provider into a validation finding
    instead of an omission. The reason rides in ``metadata`` so evidence and
    contracts can name it.
    """
    return EnvResource(
        name=key,
        kind="provider",
        version=UNKNOWN_VERSION,
        metadata={
            "provider": provider,
            "provider_status": status.value,
            "provider_detail": detail,
        },
    )


def _stamp(resource: EnvResource, *, provider: str) -> EnvResource:
    """Label a captured resource with the provider that produced it."""
    metadata = dict(resource.metadata)
    metadata.setdefault("provider", provider)
    return resource.model_copy(update={"metadata": metadata})


def resolve_and_capture(
    run_id: str,
    config: ProviderConfig | None,
    *,
    registry: ProviderRegistry | None = None,
    extra: Mapping[str, EnvResource] | None = None,
) -> ConfiguredCapture:
    """Resolve the configured providers and capture the environment with them.

    Never raises. Every failure a provider can produce is turned into an
    ``UNKNOWN_VERSION`` resource for the keys it declared, plus a diagnostic,
    because a resume must always be able to answer "is this safe" even when the
    world is partly uninspectable.

    Providers are applied in the order they were configured, and resource keys
    are emitted in sorted order, so the same configuration always produces the
    same snapshot bytes.
    """
    if config is None or not config.specs:
        return ConfiguredCapture(
            snapshot=capture(run_id, providers=(), extra=extra),
            diagnostics=(),
        )

    registry = registry or ProviderRegistry()
    conflicts = config.conflicts()
    conflicted_keys = frozenset(conflicts)
    diagnostics: list[ProviderDiagnostic] = []
    resources: dict[str, EnvResource] = {}

    def fail_closed(
        keys: Iterable[str],
        *,
        provider: str,
        status: ProviderStatus,
        detail: str,
    ) -> None:
        for key in sorted(set(keys)):
            if key in resources:
                # A conflicted key is already UNKNOWN and only reported once;
                # a provider failing twice for the same key keeps the first,
                # richer reason.
                continue
            resources[key] = _unknown(key, provider=provider, status=status, detail=detail)

    for spec in config.specs:
        declared = spec.declared_resources()
        if not declared:
            diagnostics.append(
                ProviderDiagnostic(
                    provider=spec.provider,
                    status=ProviderStatus.MALFORMED,
                    resources=frozenset(),
                    detail=(
                        "declares no resources and none can be derived from its "
                        "parameters; declare the resource keys explicitly"
                    ),
                )
            )
            continue

        shared = declared & conflicted_keys
        if shared:
            others = {
                other.provider
                for key, specs in conflicts.items()
                for other in specs
                if key in shared and other is not spec
            }
            diagnostics.append(
                ProviderDiagnostic(
                    provider=spec.provider,
                    status=ProviderStatus.CONFLICT,
                    resources=shared,
                    detail=(
                        "resource key declared by more than one provider: "
                        + ", ".join(sorted(others))
                    ),
                )
            )
            fail_closed(
                shared,
                provider=spec.provider,
                status=ProviderStatus.CONFLICT,
                detail="claimed by multiple configured providers",
            )
            # A conflicted provider still captures its uncontested keys, so an
            # overlap on one resource does not blind the rest.
        uncontested = declared - conflicted_keys

        if not spec.enabled:
            diagnostics.append(
                ProviderDiagnostic(
                    provider=spec.provider,
                    status=ProviderStatus.DISABLED,
                    resources=uncontested,
                    detail=(
                        "provider is disabled, so its resources are reported "
                        "unknown rather than assumed unchanged"
                    ),
                )
            )
            fail_closed(
                uncontested,
                provider=spec.provider,
                status=ProviderStatus.DISABLED,
                detail="provider is disabled",
            )
            continue

        if not uncontested:
            continue

        provider, why, why_detail = _resolve(spec, registry)
        if provider is None:
            if why is ProviderStatus.MALFORMED:
                detail = f"rejected its parameters: {why_detail}"
            else:
                detail = (
                    f"no provider named {spec.provider!r} is a built-in "
                    f"({', '.join(sorted(BUILTINS))}) or registered with this engine"
                )
            diagnostics.append(
                ProviderDiagnostic(
                    provider=spec.provider,
                    status=why,
                    resources=uncontested,
                    detail=detail,
                )
            )
            fail_closed(
                uncontested,
                provider=spec.provider,
                status=why,
                detail=detail,
            )
            continue

        try:
            captured = provider.capture()
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"
            diagnostics.append(
                ProviderDiagnostic(
                    provider=spec.provider,
                    status=ProviderStatus.FAILED,
                    resources=uncontested,
                    detail=f"capture raised {detail}",
                )
            )
            fail_closed(
                uncontested,
                provider=spec.provider,
                status=ProviderStatus.FAILED,
                detail=f"capture raised {detail}",
            )
            continue

        for key in sorted(captured):
            resource = captured[key]
            if key not in uncontested:
                # A provider reporting beyond its declared scope cannot widen
                # what validation trusts on its say-so. Reported as unknown, not
                # dropped, so the boundary violation is visible in evidence.
                diagnostics.append(
                    ProviderDiagnostic(
                        provider=spec.provider,
                        status=ProviderStatus.UNDECLARED,
                        resources=frozenset({key}),
                        detail=(f"reported resource {key!r} outside its declared scope"),
                    )
                )
                resources[key] = _unknown(
                    key,
                    provider=spec.provider,
                    status=ProviderStatus.UNDECLARED,
                    detail="reported outside the provider's declared scope",
                )
                continue
            if key in resources:
                continue
            resources[key] = _stamp(resource, provider=spec.provider)

        # Every declared key the provider never returned is an absence, which
        # diff_environments reads as REMOVED only when the key is present here
        # with a version. Reporting it unknown is the conservative choice: the
        # provider said it owned the key and then said nothing about it.
        missing = sorted(uncontested - frozenset(captured))
        if missing:
            diagnostics.append(
                ProviderDiagnostic(
                    provider=spec.provider,
                    status=ProviderStatus.FAILED,
                    resources=frozenset(missing),
                    detail="declared resources the provider did not report",
                )
            )
            for key in missing:
                if key in resources:
                    continue
                resources[key] = _unknown(
                    key,
                    provider=spec.provider,
                    status=ProviderStatus.FAILED,
                    detail="declared but not reported by the provider",
                )

    if extra:
        for key in sorted(extra):
            resources.setdefault(key, extra[key])

    return ConfiguredCapture(
        snapshot=capture(run_id, providers=(), extra=resources),
        diagnostics=tuple(diagnostics),
    )


def config_from_events(events: Iterable[Event]) -> ProviderConfig | None:
    """The run's authoritative provider configuration.

    ``None`` means unconfigured: no event has ever recorded providers, so the
    run keeps today's behaviour exactly. An event recording an empty set is a
    deliberate clear, and returns an empty configuration, which is a
    configuration that owns nothing.

    The newest configuration wins, and the scan includes the archived prefix so
    compaction cannot retire a configuration. A payload that fails validation is
    skipped with the others considered: a malformed configuration must not
    silence a later, well-formed one.
    """
    latest: dict[str, Any] | None = None
    for event in events:
        if event.type is not EventType.ENVIRONMENT_PROVIDERS_CONFIGURED:
            continue
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        if CONFIG_PAYLOAD_KEY in payload:
            latest = dict(payload)
    if latest is None:
        return None
    return ProviderConfig.model_validate(
        {
            "schema_version": latest.get("schema_version", SCHEMA_VERSION),
            "specs": latest.get(CONFIG_PAYLOAD_KEY) or (),
        }
    )


def record_provider_config(
    storage: Any,
    run_id: str,
    config: ProviderConfig,
    *,
    provenance: str = "",
    source: Any = None,
) -> ProviderConfig:
    """Append the configuration as the run's new authoritative set.

    The whole configuration is recorded every time, because the log is
    append-only: an "add" and a "remove" are both a full rewrite of the set, and
    the newest event is the truth. Returns the configuration unchanged so a
    caller can chain on it.
    """
    from continuum.models import Origin

    payload = {
        CONFIG_PAYLOAD_KEY: [spec.model_dump(mode="json") for spec in config.specs],
        "schema_version": config.schema_version,
    }
    if provenance:
        payload["provenance"] = provenance[:_MAX_PROVENANCE]
    storage.append_event(
        run_id,
        EventType.ENVIRONMENT_PROVIDERS_CONFIGURED,
        payload,
        source=source if source is not None else Origin.DETERMINISTIC,
    )
    return config


def parse_params(pairs: Sequence[str]) -> dict[str, Any]:
    """Parse ``key=value`` CLI pairs into JSON-native parameters.

    A value is read as JSON when it parses and kept as text otherwise, so
    ``--param paths='["a.txt"]'`` gives a list while ``--param note=hello`` gives
    a string. Empty values are refused: an empty parameter is almost always an
    unexpanded shell variable, and it is not a value any provider expects.
    """
    import json

    params: dict[str, Any] = {}
    for pair in pairs:
        key, separator, raw = pair.partition("=")
        if not key or not separator:
            raise ValueError(f"--param expects key=value, got {pair!r}")
        if not raw:
            raise ValueError(
                f"--param {key}= has an empty value; omit the parameter if it is unknown"
            )
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params
