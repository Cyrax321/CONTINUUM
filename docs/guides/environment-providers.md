# Configured environment providers

> **Issue #762.** A run records which world-observers it trusts; resume resolves
> them and feeds the capture to the validator that already existed.

A checkpoint only means something relative to the world it was written against:
"3,421 documents analysed" says nothing if the dataset was replaced afterwards.
CONTINUUM has always been able to compare a checkpointed environment against a
current one, but the current side had to be handed over by whoever called
`resume`. An integration that wires a reliable observer into capture could still
resume through a path that validated only what the caller remembered to supply,
and nothing in the output said so.

Configured providers close that gap. A run names its observers once; at resume
the engine resolves them, captures the world with them, and validates against
that. The configuration is data in the event log, not code, and no provider is
ever loaded, imported or executed because a configuration mentions it.

## The trust boundary

A configuration cannot reach code. This is the whole design, and it is enforced
in five places:

1. **A provider name is a lookup key.** `ProviderSpec.provider` must match
   `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}`. It is never a path, a module reference or
   an import string. Resolution looks the name up in a table the host process
   built, and nothing else.
2. **Only built-ins and registrations resolve.** The built-ins are `file`,
   `git`, `static` and `value`. Any other name must have been handed to a
   `ProviderRegistry` in the host process. An unknown name is reported
   `unavailable` and fails closed, never autoloaded.
3. **Parameters are JSON-native or they are refused.** A callable in a
   parameter would be dropped on persistence and executed in memory, meaning
   different things before and after a restart. `ProviderSpec` rejects it.
4. **Secrets are refused by name.** A parameter whose name matches
   `password`, `secret`, `token`, `credential` or `api_key` is rejected at
   configuration time, so a secret never reaches the hashed event log, a
   checkpoint dump or a log dump in the first place. Configure credentials out
   of band.
5. **`CallableProvider` is not configurable.** A probe is live code and cannot
   be serialized. Register it by name with a `ProviderRegistry` and configure
   the name: the configuration stays inert data, the probe stays code.

Built-ins are constructed from their parameters by `from_config`, and each one
also exposes `scope_of`, which names the resource keys it will report *without
capturing*. That separation is what makes fail-closed possible: the resolver
knows what a provider owns before it asks the provider anything.

## Lifecycle

### 1. Configure

```python
from continuum.environment import ProviderConfig, ProviderSpec, record_provider_config

config = ProviderConfig(specs=[
    ProviderSpec(provider="git", params={"path": "/srv/app"}),
    ProviderSpec(provider="file", params={"paths": ["/srv/app/config.yaml"]}),
])
record_provider_config(storage, run_id, config, provenance="bootstrap")
```

The whole set is recorded in one `ENVIRONMENT_PROVIDERS_CONFIGURED` event, so
the log stays append-only: an add and a remove are both full rewrites, and the
newest event is the authoritative configuration. The event is not folded into
state, it configures validation.

From the CLI the same thing, with the file provider's resources standing in for
its `paths` parameter:

```console
$ continuum providers add "$RUN" --provider git --param path=/srv/app
$ continuum providers add "$RUN" --provider file --resource /srv/app/config.yaml
$ continuum providers list "$RUN"
$ continuum providers check "$RUN" --json
$ continuum providers remove "$RUN" --all
```

`check` is read-only and resolves exactly as resume would, so an operator can
see a failing observer before it gates a recovery.

### 2. Resume

`RecoveryEngine.assess` reads the configuration when the caller does not supply
a `current_environment`, resolves each provider, captures, and validates against
it. A run that never configured anything keeps today's behaviour exactly: no
event, no providers, no capture. A caller-supplied environment always wins, so
nothing that works today changes.

```python
from continuum.recovery import RecoveryEngine

decision = RecoveryEngine(storage).assess(run_id)
for diagnostic in decision.provider_diagnostics:
    print(diagnostic.render())
```

### 3. Survive compaction

Resume reads the archived prefix as well as the live tail, so a compacted run
still finds its configuration. The forced anchor checkpoint written at the
boundary inherits the environment the run validated against, so the comparison
has something to diff a fresh capture into.

## Fail-closed: the rule and the cases

An unavailable, disabled, malformed, conflicting or failing provider never
shrinks what validation covers. Every resource it declared is emitted as
`UNKNOWN_VERSION`, which never compares equal to a real version, so the
validator downgrades rather than assuming the best. The diagnostic names the
cause.

| status | meaning | why |
| --- | --- | --- |
| `ok` | reported its resources | nothing to do |
| `disabled` | the spec was recorded `enabled=False` | an operator suspended the observer; its resources are unknown, not assumed unchanged |
| `unavailable` | no such built-in and no registration | a typo, or a provider the deployment does not register |
| `malformed` | the provider rejected its parameters | a configuration error this deployment can fix |
| `conflict` | another spec declared the same resource key | both fail closed; neither silently wins |
| `failed` | `capture()` raised | a provider that broke its own contract |
| `undeclared` | the provider reported a key outside its scope | the surplus is reported unknown, not trusted |

`undeclared` is a warning, not a gap: the extra key is already `UNKNOWN` in the
snapshot, so it cannot have been trusted. Every other status sets
`ConfiguredCapture.fail_closed`.

Two resources declared by different specs is the subtlest case. Whichever spec
won the collision would silently demote the other's observation, so both specs
report `conflict` and the key is `UNKNOWN`. Order never decides safety.

## Provenance in evidence

Every captured resource is labelled with the provider that produced it, in
`EnvResource.metadata["provider"]`, and validation entries carry it into
evidence: a dependency entry reads `verified unchanged (provider: git)` rather
than `verified unchanged`. The recovery contract's evidence list inherits those
strings, so an auditor can see which observer vouched for what without leaving
the contract.

## Safe defaults

- **Configure nothing by default.** An unconfigured run behaves exactly as it
  did before this feature. Discovery is opt-in per run.
- **Prefer observers that read, not assert.** `git` and `file` discover what
  the world actually contains; `static` asserts a version the caller chose, and
  asserts are only as trustworthy as the caller.
- **Declare the scope for anything you register.** A registered provider's
  resource keys are not derivable from parameters, so `--resource` is required
  for it. A provider that reports beyond its scope has those keys marked
  unknown.
- **Treat `check` output as a health signal.** A provider that reads
  `unavailable` or `malformed` is a deployment problem, and it is visible before
  it gates a recovery, not after.
- **Never put a credential in a parameter.** It is refused; put it in the
  environment of the process that registers the probe.
