# CONTINUUM Integration Architecture: attaching to any system

This document does three things. First it records the concrete limitations of
CONTINUUM as built today. Second it surveys how the field actually solves the
"attach recovery to an arbitrary agent system" problem, drawing on shipping
frameworks (DeepSeek Harness, Dapr, LangGraph, Temporal) and recent research.
Third it proposes a plugin architecture for CONTINUUM that borrows the parts
that work and fits the differentiator CONTINUUM already has.

The goal is not to make CONTINUUM another agent framework. The goal is to make
it attachable as a recovery and verification layer to systems it was not written
for, with as little per-system code as possible.

## 1. Limitations of CONTINUUM as it stands

These are the blockers to universal attachment, in plain terms.

1. **In-process Python library, not a boundary.** Recovery only happens if the
   agent's process imports `continuum` and calls `checkpoint` / `resume` /
   `intercept_action`. A system in another language, or a separate orchestrator,
   cannot call it without embedding Python. The MCP server is a step toward a
   boundary but MCP itself is a specific integration, not a universal one.
2. **Opt-in instrumentation.** The agent must *willingly* record progress, route
   effects through the ledger, and call `resume` before acting. There is no
   generic auto-instrumentation (wrapping an HTTP client or DB driver) that
   captures effects and state without code changes.
3. **CONTINUUM-specific semantic model.** Checkpoints expect `goal / progress /
   decisions / findings / evidence / external_dependencies`. Mapping an arbitrary
   system's internal state onto that shape is manual. The deterministic
   extractor reads explicit metadata and tool calls; there is no universal
   adapter contract that says "here is my state, recover me."
4. **Environment is declared, not discovered.** `--env name=version` and
   `StaticProvider` require the agent to *assert* what the environment is. Real
   environments (live DB schemas, API versions, filesystem trees, git HEAD) are
   not auto-observed, so validation is only as good as what was declared.
5. **Side effects tracked only if routed through the ledger.** Any external
   effect performed outside `claim` / `complete` is invisible.
6. **Local SQLite, single agent.** No shared store, no multi-agent coordination,
   no distributed locking. The spec's PostgreSQL / cloud / coordination tiers
   (Phases 13, 23) are unbuilt.
7. **No migration story for long-lived state.** Open issue #17: old-schema
   databases are silently accepted or break. A tool whose value is durable state
   across time and versions must migrate both the event schema and the
   projector logic, or recovered state drifts when the projector changes.
8. **Trust gaps undermine the core claim.** #1 (MCP auth is client-asserted),
   #19 (`resume --repair` is a no-op), #17. The "can this state be trusted"
   promise is only as strong as its weakest unresolved item.
9. **Reconciliation is manual.** `UnknownSideEffect` is raised and a human or
   reconciler must intervene. There is no library of common reconcilers.
10. **No observability.** No metrics, no dashboard (Phase 14), no alerting.
11. **Validation rules are environment-centric.** Staleness propagation is good,
    but it assumes the dependency graph is declared. Domain-specific staleness
    needs pluggable rules.
12. **Adversarial and durability edge cases are not fully covered.** Clock skew,
    partial writes, and projector version drift across upgrades are under
    addressed.

## 2. How the field solves attachment and recovery

### 2.1 DeepSeek Harness (dsh) and Cordis: everything is a plugin

DeepSeek open-sourced its agent harness in August 2026. Its defining idea,
powered by the Cordis framework, is that *every* capability is a plugin: model,
tools, UI, storage, security policy, context management, and even the agent loop
itself. The parts that matter for CONTINUUM's attachment problem:

- **Service plugins and a context registry.** Any module is an injectable
  `Service` registered into a global `Context` (e.g. `ctx.llm`, `ctx.tools`).
  Plugins call each other by key, not by concrete class, which decouples them
  deeply.
- **Declarative dependencies.** Plugins declare required services via `inject`,
  and Cordis starts them in topological order.
- **Typed event communication** with four dispatch modes: `emit` (async
  broadcast), `waterfall` (middleware), `parallel`, and `serial`. This is how
  permissions, hooks, and retries are injected without touching core logic.
- **Reversible registrations.** Every contribution to the system goes through
  `ctx.effect()` and is guaranteed reversible, which gives clean hot-module
  replacement and stability for long-running sessions.
- **The capability seam.** A capability is split into three roles: the service
  *definition* (interface), the *provider* (backend implementation), and the
  *consumer* (model-facing tool). Swapping the provider (e.g. local bash for a
  sandbox) changes the execution environment without changing any tool code.
- **Policy pipeline via waterfall events.** A tool call passes through
  `tools/pre-execute`, `ctx.approval`, `tools/execute`; policies such as
  permission checks and approvals attach as listeners, separate from the tool.
- **Logs as the source of truth.** The session is an append-only event stream,
  and what the model sees is a deterministic projection (`deriveMessages()`) of
  that log. The invariant is "what the model sees must be identical to what is
  logged," which gives byte-for-byte reproducibility and auditability.
- **Self-bootstrapping.** An agent can mount or unmount temporary plugins at
  runtime via a sandboxed tool, enabling it to grow its own capabilities.

DeepSeek Harness's own stated limits are relevant: its session format is
explicitly unstable (`SESSION_FORMAT_VERSION = 0`), it is not recommended for
production persistent storage yet, and the plugin model carries a steep
learning curve. But the *architecture* is the closest existing match to what
CONTINUUM needs: a registry plus dependency injection plus reversible effects
plus a capability seam plus an event waterfall. CONTINUUM should adopt the shape,
not the TypeScript implementation.

Reference: github.com/deepseek-ai/deepseek-harness, and the Cordis paper on
spatiotemporal composability it cites.

### 2.2 Dapr 1.18 Verifiable Execution

Dapr (CNCF) shipped "Verifiable Execution" in June 2026 with three capabilities:
**Workflow History Signing**, **Workflow History Propagation**, and **Workflow
Attestation**. Signing uses SPIFFE-based workload identities to cryptographically
sign every history event, building a blockchain-style append-only log; if anyone
alters data, the next continue fails because the current signature no longer
matches the previous one. Propagation carries execution lineage across service
and agent boundaries. Attestation lets a downstream system make a policy decision
on verified provenance. The maintainers stress *real-time enforcement*: the
workflow refuses to continue when a precondition is missing, rather than failing
in a post-mortem.

This validates the attestation layer CONTINUUM just built. CONTINUUM's
`storage/sqlite.py` already enforces `prev_hash` chaining and per-event hash
verification on every append, which is the log-level equivalent of Dapr's history
signing. The gaps are: (a) tying signatures to a *workload identity* rather than
an ad-hoc key file, (b) propagating attestations across system boundaries, and
(c) emitting a portable attestation token a downstream system can check. The
direction is correct; the production shape is now a known quantity.

References: cncf.io/blog/2026/06/11/introducing-verifiable-execution-in-dapr-1-18,
and the Diagrid and InfoQ write-ups of the same release.

### 2.3 LangGraph checkpointers: a minimal interface grows an ecosystem

LangGraph persists agent state through "checkpointers" that conform to
`BaseCheckpointSaver` with a small set of methods (`put`, `put_writes`, `get`,
`get_tuple`, `list`). On top of that one interface the community has produced
SQLite, PostgreSQL, Redis, and MongoDB implementations, plus a conformance
harness (`@langchain/langgraph-checkpoint-validation`) that tests any custom
checkpointer against the contract. The lesson is direct: a tiny, well-specified
interface plus a conformance test is what lets a plugin ecosystem form.

LangGraph's own admitted gap (Diagrid's 2026 critique, echoed across the
durable-execution literature) is that checkpoints are *durable data, not durable
execution*: a run lives in one process, recovery is manual, and nothing prevents
two workers from resuming the same `thread_id` concurrently. That gap is exactly
CONTINUUM's niche: independent revalidation, staleness propagation, and
(optionally) lease coordination are the value it adds on top of an existing
checkpointer. The roadmap's "keep your checkpointer, add the validator" adapter
is the right framing.

References: github.com/langchain-ai/langgraph (libs/checkpoint), and the
LangGraph v0.2 announcement on checkpointer libraries.

### 2.4 Temporal and durable execution: separate orchestration from reasoning

Temporal journals every workflow step to an Event History and, on recovery,
*replays* the workflow code against that history to reconstruct exactly where it
stopped. Non-deterministic calls (LLM, tools, HTTP) must live in Activities that
run outside the replay path. The hard constraint that makes "exactly where it
stopped" possible is the determinism contract. Temporal's 2026 LangGraph plugin
runs a LangGraph graph as a Temporal Workflow with nodes as Activities, so the
run itself survives crashes. The pattern the industry has converged on: LangGraph
(or any agent framework) models *reasoning*, Temporal (or an equivalent) owns
*durable orchestration*. CONTINUUM fits as a third layer, the semantic
verification and side-effect reconciliation layer, complimentary to both.

References: temporal.io/blog/temporal-langgraph-plugin-durable-execution, and
the numerous 2026 "LangGraph vs Temporal" comparisons (cordum.io, agentmarketcap.ai,
dreaming.press).

### 2.5 Research

- **Semantic checkpointing** (Roshan et al., 2025, "Semantic checkpointing for
  stateless LLM agents in multi-tenant enterprise systems") is the academic
  grounding for CONTINUUM's central idea: store meaningful context summaries
  externally so an agent resumes without recomputing entire histories. Owen et
  al. (2025, "Fault Tolerance and Recovery Strategies for LLM Agents in
  Distributed Systems") synthesize semantic checkpointing, redundancy, and
  graceful degradation for agent recovery.
- **AgentTether** (arXiv 2605.22343) is a run-time repair framework that
  automates post-run diagnosis and guided recovery *without modifying the
  underlying agent or environment*, explicitly positioned as a reliability layer
  that wraps existing deployments. That is CONTINUUM's positioning stated as
  research.
- **Saga-style compensation** for multi-step tool pipelines (e.g. the
  awrr-runtime-agent prototype) maps directly onto CONTINUUM's `COMPENSATED`
  action status: a way to model "the effect was undone, so repeating it is
  legitimate."
- **Concordia** (arXiv 2606.23521) checkpoints GPU-resident KV caches by
  inserting delta handlers at device synchronization points. It is about
  inference serving, not agent task state, but its pattern (checkpoint at
  synchronization points, compute deltas, keep the host off the critical path) is
  the right shape for any high-frequency state capture and worth noting as a
  boundary of scope.

## 3. A plugin architecture for CONTINUUM (the Cordis seam, adapted)

CONTINUUM already has extension points in `project.md` Section 38
(`StatePlugin`, `EnvironmentProvider`, `ActionReconciler`, `RecoveryStrategy`)
and the adapter base classes. What is missing is the *registry and dispatch*
that makes them composable and discoverable, plus a policy event pipeline. The
design below adapts Cordis's ideas to CONTINUUM's Python, stdlib-only core.

### 3.1 Registry and dependency injection

A single `Registry` holds named services. Plugins declare what they provide and
what they need; the runtime resolves them. This is how an arbitrary system plugs
in its own environment scanner or state extractor without CONTINUUM knowing about
it in advance.

```python
class Registry:
    def register(self, name: str, service: object) -> None: ...
    def get(self, name: str, type_: type[T]) -> T: ...

    # reversible: removing a plugin tears down only what it contributed
```

### 3.2 The four capability seams

Each seam is an interface (definition), one or more providers (implementation),
and consumers (CONTINUUM internals). Swapping a provider changes behavior
without changing call sites.

- **EnvironmentProvider** (discover, not declare). Replaces `StaticProvider`.
  Instead of the agent asserting `dataset=v3`, a provider observes it:

  ```python
  class EnvironmentProvider(Protocol):
      def resources(self, run_id: str) -> dict[str, EnvResource]: ...
      def diff(self, before, after) -> EnvironmentDiff: ...
  ```
  Ships with providers for filesystem trees, SQL schema versions, REST API
  version endpoints, and git HEAD. This closes limitation #4.

- **StateExtractor** (map arbitrary internal state). The pluggable extractor from
  `project.md` Section 8, promoted to a first-class seam so any framework can
  teach CONTINUUM its state shape:

  ```python
  class StateExtractor(Protocol):
      def extract(self, trajectory, environment) -> SemanticState: ...
  ```
  Closes limitation #3.

- **ActionReconciler** (common idempotency / read-back). A library of reconcilers
  so `UnknownSideEffect` is usually resolved automatically:

  ```python
  class ActionReconciler(Protocol):
      def reconcile(self, action: Action) -> Reconciliation: ...
  ```
  Ships with idempotency-key read-back and "does this external record exist?"
  checks. Closes limitation #9.

- **ValidationRule** (domain staleness). Beyond environment changes, domains
  have their own staleness logic (e.g. "a approved decision is invalid if the
  regulation it cites was revoked"):

  ```python
  class ValidationRule(Protocol):
      def evaluate(self, state, environment) -> list[StateStatus]: ...
  ```
  Closes limitation #11.

### 3.3 Policy event pipeline

Adopt the waterfall pattern for the moments CONTINUUM already hooks: before
checkpoint, before resume, after action claim, on unknown side effect.
Listeners attach without modifying core code, which is how permissions,
approvals, and retries compose.

### 3.4 Logs as the source of truth (already true)

CONTINUUM's append-only event log already is the source of truth. Make the
invariant explicit and first-class: the recovered semantic state is a
deterministic projection of the event log (it already is, via `project()`), and
the attestation layer signs that projection's root. This aligns CONTINUUM with
both DeepSeek's reproducibility invariant and Dapr's signed-history model.

## 4. The service boundary (Tier 0 of attachment)

To attach to systems that are not Python agents, CONTINUUM must stop being only
a library and become a service:

- A **sidecar or server** exposing a stable wire API (reuse the MCP surface, or
  add HTTP/gRPC) that any process calls to record progress, intercept actions,
  and resume.
- **Thin SDKs in several languages** so a Go service or a TypeScript agent
  attaches with a few calls, not by embedding Python.
- A **generic auto-instrumentation shim** (HTTP client / DB driver wrappers) so
  effects and state are captured with minimal per-system code. This is the
  practical meaning of "attach to any system": not zero changes, but a thin
  adapter plus an SDK.

## 5. Aligning attestation with Verifiable Execution

CONTINUUM's chain-hash signing already matches Dapr's history signing at the log
level. To reach Dapr's production shape:

- Tie signatures to a **workload identity** (optional SPIFFE/SPIRE, or a simpler
  deployment-issued key) instead of an ad-hoc PEM file. The `attest` command we
  built takes the first step; identity binding is the next.
- Emit a **propagation token** so a downstream system can verify provenance of
  delegated work, matching Dapr's Workflow Attestation.
- Add a **real-time enforcement hook** in `resume`: refuse to continue if the
  attestation or chain no longer matches, rather than only reporting after the
  fact. This is the "safe to resume" gate CONTINUUM already computes; attestation
  makes it cryptographically defensible.

## 6. What "attach to any system" realistically requires

A prioritized program, building on the tiers sketched earlier.

- **Tier 0, boundary:** sidecar/server + wire API + multi-language SDKs +
  auto-instrumentation shim. Without this, attachment is Python-only.
- **Tier 1, teachability:** the plugin registry (Section 3) plus the four seams,
  each with at least one built-in provider and a conformance test (borrow
  LangGraph's lesson: small interface + conformance test = ecosystem).
- **Tier 2, production durability:** PostgreSQL storage, a centralized server,
  distributed locking / lease coordination for runs, and a schema plus projector
  migration framework (closes #17 and the long-lived-state risk).
- **Tier 3, trust and ops:** close #1, #17, #19; add a dashboard, metrics, and
  alerting; stream the signed audit log.
- **Tier 4, portability:** define a "Recovery State" interchange schema (the way
  OpenTelemetry standardized traces) so different systems and versions
  interoperate and external tools can verify CONTINUUM output.

Suggested first issues: (a) define `Registry` and the four seam protocols with
conformance tests, (b) ship one `EnvironmentProvider` that discovers a real
backend (postgres schema version or git HEAD), (c) add a `continuum serve`
sidecar exposing the existing CLI surface over a wire protocol, (d) bind
attestation to a workload identity and emit a propagation token.

## 7. Honest limits of the plugin approach

Even with all of this, "attach to anything with zero changes" is impossible for
*semantic* recovery. CONTINUUM must at minimum receive state and see effects; the
plugin seams exist precisely to make that teaching cheap and per-system code
small. The trust boundary also shifts: a plugin that claims to "discover" the
environment is itself a component that can be wrong or malicious, so plugins need
the same provenance and review discipline as the core. And a rich plugin
ecosystem is a governance surface, not just a convenience, which is why
reversible registration and conformance tests matter from day one.

## 8. References

- DeepSeek Harness: github.com/deepseek-ai/deepseek-harness (Cordis "everything
  is a plugin" model; session event stream as source of truth).
- Dapr 1.18 Verifiable Execution: cncf.io/blog/2026/06/11/
  introducing-verifiable-execution-in-dapr-1-18 (history signing, propagation,
  attestation, SPIFFE identity, real-time enforcement).
- LangGraph checkpointers: github.com/langchain-ai/langgraph (libs/checkpoint,
  BaseCheckpointSaver, checkpointer-validation conformance tool).
- Temporal durable execution and the LangGraph plugin:
  temporal.io/blog/temporal-langgraph-plugin-durable-execution.
- Roshan et al. 2025, Semantic checkpointing for stateless LLM agents in
  multi-tenant enterprise systems.
- Owen et al. 2025, Fault Tolerance and Recovery Strategies for LLM Agents in
  Distributed Systems.
- AgentTether: arXiv 2605.22343 (wrap-and-repair reliability layer).
- Concordia: arXiv 2606.23521 (GPU-resident KV checkpointing, sync-point hooks).
- awrr-runtime-agent: github.com/LaoZhongjie/awrr-runtime-agent (Saga-style
  compensation for tool pipelines).
