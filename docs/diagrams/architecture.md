# CONTINUUM architecture

A system view of CONTINUUM: which seams a harness reaches the durable core
through, how side effects are enforced at the boundary, what happens when a
run dies mid-effect, and which surfaces read state back. Every node names the
module that implements it. Every figure on this page was re-verified against
the source the day it was written (commands in the last section).

This is the integration view. For the SDK's internal data flow see
`references/architecture-flow.md`; for the component model, the projection
semantics and the recovery context, see `references/architecture.md`; for the
rendered drawing plus the enumerated surface inventory, see
`references/architecture-diagram.md`.

## System architecture

```mermaid
flowchart TB
    %% ---------- agents and clients ----------
    subgraph clients["Agents and clients"]
        direction TB
        cc["Claude Code"]
        gem["Gemini CLI"]
        codex["Codex"]
        lg["LangGraph workflow"]
        oa["OpenAI Agents SDK"]
        other["Other harnesses<br/>any language or stack"]
    end

    %% ---------- five integration seams ----------
    subgraph seams["Five integration seams"]
        direction TB
        ad["1. In-process adapters<br/>adapters/, intercept_action and wrap_tool"]
        mcp["2. MCP stdio server<br/>mcp/server.py, 13 tools, caller allowlist,<br/>deny by default"]
        hooks["3. CLI lifecycle hooks<br/>hooks.py and clienthooks.py"]
        gw["4. HTTP gateway<br/>gateway.py, claim before fire over HTTP"]
        otel["5. OTel bridge<br/>otel.py, spans become evidence"]
    end

    %% ---------- enforcement boundary ----------
    subgraph enforce["Enforcement boundary: claim before fire"]
        direction LR
        gate["Gate<br/>gate.py, a gated call proceeds only with a live claim"]
        ledger["Action ledger<br/>actions/, idempotency key survives argument drift"]
        ext["External systems<br/>GitHub, email, APIs"]
    end

    %% ---------- durable core ----------
    subgraph core["Durable core: one hash-chained log"]
        direction LR
        elog[("Event log<br/>events.py, 55 event types")]
        state["State engine<br/>state/, project and staleness validation"]
        ckpt["Checkpoint manager<br/>checkpoint/, 6 policies, RECOVERY anchors"]
        rec["Recovery engine<br/>recovery/, 7 modes, sealed contract"]
    end

    %% ---------- storage ----------
    subgraph storage["Storage backends"]
        direction LR
        sql[("SQLite, the primary backend<br/>schema v6, WAL, synchronous FULL")]
        pg[("Postgres<br/>storage/postgres.py and migrations.py")]
        blob[("Blob store<br/>storage/blob.py, payload rehydration")]
    end

    %% ---------- surfaces ----------
    subgraph surfaces["Surfaces that read state back"]
        direction LR
        cli["CLI, 52 commands<br/>only a verified-safe run exits 0"]
        dash["Web dashboard and human gate<br/>dashboard/ and serve/"]
        tui["Terminal UI<br/>tui/"]
        bench["Benchmark and fault injection<br/>benchmark/"]
    end

    %% ---------- a client reaches one or more seams ----------
    cc --> hooks
    gem --> hooks
    codex --> hooks
    lg --> ad
    oa --> ad
    other --> mcp

    %% ---------- seams feed the boundary or the log ----------
    ad --> ledger
    mcp --> ledger
    hooks --> gate
    gw --> ledger
    otel --> elog

    %% ---------- claim, fire, settle ----------
    gate --> ledger
    ledger ==>|"claim, then fire"| ext
    ext ==>|"the real outcome settles it"| ledger
    ledger -->|"append ACTION_RECORDED"| elog

    %% ---------- the core pipeline ----------
    elog --> state --> ckpt --> rec

    %% ---------- storage: write and read paths ----------
    elog --> sql
    elog --> pg
    elog -->|"large payloads offloaded"| blob
    sql -->|"verify, project, replay"| state
    pg -->|"verify, project, replay"| state
    blob -->|"rehydrate on read"| state

    %% ---------- the verdict reaches a surface ----------
    rec -->|"resume contract"| surfaces
```

## The claim, fire, settle lifecycle

The ledger is a two-phase protocol. A side effect is claimed before it fires
because a Python callable cannot cross the MCP boundary: `intercept_action`
answers *may I*, the caller performs the effect, `complete_action` records the
outcome. A crash between the two leaves the outcome genuinely unknown, and
that is by design, never silently completed, never retried, never dropped.

```mermaid
sequenceDiagram
    participant A as Agent
    participant S as Seam (MCP tool, adapter, gateway)
    participant L as Action ledger
    participant E as External system
    participant Log as Event log
    participant R as Recovery engine

    A->>S: intercept_action(key)
    S->>L: claim(action, key)
    L->>Log: append ACTION_RECORDED (started)
    S-->>A: proceed true
    A->>E: fire the side effect
    Note over A,E: hard crash here, no completion is ever reported
    A--x S: complete_action(key, outcome)
    R->>L: reconcile the unknown outcome
    L->>E: probe (ProbeReconciler)
    E-->>L: occurred, external_id
    L->>Log: append ACTION_RECONCILED
    R-->>A: REQUEST_HUMAN, then RESUME, the effect never fires twice
```

Every state component carries its origin. Facts the agent asserted about
itself (`Origin.EXTERNAL_AGENT`) land as `REQUIRES_REVIEW` until a
`REVIEW_CONFIRMED` event clears them, so progress reported through MCP cannot
self-certify a safe resume. Deterministic writer state that still matches the
environment resumes cleanly, and its exit code is 0.

## Figures on this page

| Figure | Value | Where it is asserted |
|:--|:--|:--|
| Schema version | 6 | `SCHEMA_VERSION` in `src/continuum/storage/migrations.py` |
| Event types | 55 | `EventType` in `src/continuum/events.py` |
| MCP tools | 13 total, 3 read-only (`validate`, `resume`, `list_actions`) and 10 mutating | `@server.tool(...)` decorators in `src/continuum/mcp/server.py` |
| CLI commands | 52 | `add(...)` registrations in `src/continuum/cli/main.py` |
| Recovery modes | 7, max severity wins, `RESUME` safest through `ABORT` most severe | `RecoveryMode` in `src/continuum/recovery/engine.py` |
| Checkpoint policies | 6 (`Manual`, `Interval`, `Event`, `Semantic`, `ContextPressure`, `Hybrid`) | `src/continuum/checkpoint/policy.py` |
| Storage backends | SQLite (primary) and Postgres, plus an optional blob store | `src/continuum/storage/` |

Cross-cutting modules the diagram deliberately leaves unconnected, because
they touch everything: `security/` (trust gate, revalidation, attestation),
`provenance_map.py` (origin to review), `pinning.py` and `replay_similarity.py`
(replay correctness), `budgets.py` (retry caps), `analysis/prefix_trust.py`
(advisory trust), and `concurrency/` (cross-process leases).

## Re-verify these figures

```bash
python -c "from continuum.events import EventType; print(len(EventType))"
python -c "from continuum.recovery.engine import RecoveryMode; print(list(RecoveryMode))"
grep -c "annotations=read_only" src/continuum/mcp/server.py
grep -c "annotations=mutating" src/continuum/mcp/server.py
grep -n "SCHEMA_VERSION =" src/continuum/storage/migrations.py
```

The counts in the docs are guarded by `tests/test_docs_event_counts.py` (the
event figure must follow the live enum) and `tests/test_docs_counts.py` (the
test-suite totals must agree across docs), so this page fails CI if the
figures drift from the code.
