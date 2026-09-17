# Evidence-backed reconciliation: settle from observation, not memory

An action left STARTED by a crash between intercept and complete blocks resume
until something settles it. That something used to be a person, or a command
probe you wrote by hand. CONTINUUM also ships two probes that need no external
script, and a check that catches the ledger when it disagrees with the world.

This guide covers the built-in probes, the contradiction check, and how to feed
eBPF collector output into the same machinery. It pairs with the `reconcile`
section of [docs/api/cli.md](../api/cli.md), which documents the registry file
shape, and with [authority-probes.md](authority-probes.md), which covers the
authority half of the same command.

## What changed and what did not

Before, every probe was a command in `.continuum/reconcilers.json`:

```json
{"probes": {"send_invoice": {"command": "check-outbox", "timeout": 10}}}
```

That still works exactly as before. A probe spec may now also name a built-in
type, and the two below are the ones that ship:

```json
{"probes": {
  "write_report": {"type": "artifact_check", "path_key": "path"},
  "tool.call":    {"type": "otel_span", "identity": ["path"]}
}}
```

What did not change is the safety posture. A probe that reaches a definitive
verdict settles the action; a probe that cannot reach one leaves it pending, or
escalates it to review in strict mode. Nothing here widens what an agent may
certify about its own work.

## The otel_span probe

`continuum.otel` already mirrors tool-call spans into the event log as
`TOOL_COMPLETED` and `TOOL_FAILED` observations, with no code changes in the
traced application. The `otel_span` probe treats one of those observations as
evidence.

A span matches the claim when it names the same tool as the action's type,
agrees on the identity tokens (the path, by default), and was recorded after the
claim sequence. A matching completed span settles the action as occurred, and
the settlement event cites the span and trace ids along with the event id, so
the chain from "settled" back to "what the trace actually said" is auditable.

```json
{"probes": {"write_file": {"type": "otel_span", "identity": ["path"]}}}
```

`identity` defaults to `["path"]`; only tokens present on both the action and
the span constrain the match, so an action carrying no path does not match every
write the tool ever made.

A span that ended in error settles nothing. A failed tool call is not proof the
side effect did not happen, and treating it as such would license a retry
against a write that may have landed partially. Such an action stays in the
human queue, where an `artifact_check` probe or a person can settle it.

## The artifact_check probe

For path-scoped actions the question is usually simpler than the trace suggests:
did the file actually appear? `artifact_check` answers it from the filesystem,
with a SHA-256 digest as the receipt.

```json
{"probes": {"write_report": {"type": "artifact_check", "path_key": "path"}}}
```

`path_key` names the action argument carrying the path, so one entry serves
every target the action writes. A literal `path` also works. A missing file
settles the action as not-occurred, which is what a retry needs before it can
safely run again.

Pass `expect_sha256` when the content matters as much as the existence. A file
that exists but does not match is somebody else's write, so the probe declines to
attribute it and the action stays uncertain rather than being settled from
evidence that is not actually about it.

## Strict mode

`continuum reconcile --strict` escalates any action a probe could not settle to
`REQUIRES_REVIEW` instead of leaving it pending. Strict mode never refuses a
definitive verdict, that is the point of running a probe. It refuses to leave
unverifiable work sitting in a state a lenient resume might walk past. For a run
that tolerates no uncertainty, this is the "degrade to a human" path: exit code
20, and the recovery engine's existing strict handling takes over from there.

## Contradictions are findings, not settlements

Probing settles pending actions. A second pass, run by the same command,
cross-checks the ones already settled against the filesystem. For action types
carrying an `artifact_check` probe, two contradictions are escalated to
`REQUIRES_REVIEW` rather than resolved:

- the ledger says completed but the artifact is missing;
- the artifact exists but the ledger never recorded the completion.

Either way the command exits 20 and names the contradiction, because picking a
side silently is how work gets dropped. Flagging writes the finding into the
log, so it blocks a resume instead of vanishing with the terminal that noticed
it.

## eBPF collectors: out of process, by design

Kernel-observed truth is the most independent evidence available, and CONTINUUM
consumes it without shipping or loading a BPF program. The boundary is a tiny
adapter that reads collector JSON and speaks the probe envelope: the action as
JSON on stdin, one verdict line on stdout.

`examples/ebpf_reconciler_adapter.py` is that adapter. It normalises Tetragon
and AgentSight output, matches the action's identity tokens against the observed
syscalls and tool calls, and answers `occurred=true` or `occurred=unknown`.

Register it as an ordinary command probe:

```json
{"probes": {"write_file": {"command": "python examples/ebpf_reconciler_adapter.py --events /var/log/tetragon/events.json"}}}
```

To stream instead of reading a dump, tee the collector into a file the adapter
reads:

```bash
tetra getevents --output json > /var/log/tetragon/events.json &
continuum reconcile <run_id>
```

Collector silence is `occurred=unknown`, never `occurred=false`. The collector
may not have been running when the action happened, and absence of observation
is not observation of absence. This is why the adapter is a command probe rather
than a built-in type: the collector's availability is the operator's to assert,
and its evidence is EXTERNAL_AGENT provenance, recorded as such, not trusted
state.

eBPF is Linux-only. Tests that need a real collector skip when one is absent
(`continuum.evidence.ebpf_collector_available`), so a machine without Tetragon or
AgentSight on PATH stays green rather than faking kernel evidence.
