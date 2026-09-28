# CONTINUUM architecture flow graph

Verified Mermaid flow graph. Every node label uses the exact names from
`references/architecture-data.md` (source-checked). Render it in any Mermaid
viewer, or hand this to an image generator as the graph specification.

```mermaid
flowchart TB
    %% ---------- Agents and clients ----------
    subgraph AGENTS["Agents and clients"]
        cc["Claude Code"]
        lg["LangGraph agent"]
        oa["OpenAI agent"]
        gn["Generic Python agent"]
    end

    %% ---------- Framework adapters (optional) ----------
    subgraph ADAPT["Framework adapters (optional)"]
        gad["GenericAgentAdapter"]
        oad["OpenAIAgentAdapter"]
        lgad["LangGraphAgentAdapter"]
    end

    %% ---------- CONTINUUM SDK ----------
    subgraph SDK["CONTINUUM SDK (standard library only)"]
        direction TB
        ext["Extractors<br/>Deterministic, LLM (optional)"]
        proj["State Engine<br/>projection of events"]
        ver["Versioning<br/>content-addressed"]
        dif["Semantic diff"]
        elog[("Event Log<br/>hash-chained, 51 types")]
        led["Action Ledger<br/>claim, perform, complete"]
        ckpt["Checkpoint Manager<br/>6 policies"]
        env["Environment<br/>snapshot + diff"]
        val["Validator<br/>staleness propagation"]
        rec["Recovery Engine<br/>7 modes, sealed contract"]
        sec["Security<br/>canonical hashing, provenance"]
    end

    %% ---------- Storage / external / output ----------
    store[("Durable Storage<br/>SQLite, WAL, synchronous FULL")]
    extsys["External Systems<br/>GitHub, email, APIs"]
    resume["Resume<br/>bounded recovery context"]

    %% ---------- Flows ----------
    AGENTS --> ADAPT
    ADAPT --> SDK

    SDK --> elog
    elog --> store
    elog --> proj
    proj --> ver
    proj --> val
    env --> val
    val --> rec
    led --> rec
    rec --> store
    ckpt --> store

    led ==>|claim, perform, complete| extsys
    extsys ==>|outcome or probe| led

    rec --> resume

    sec -. secures .-> elog
    sec -. secures .-> ver
```

## Edge legend

- `Agents -> Adapters -> SDK`: the call path.
- `SDK -> Event Log -> Durable Storage`: every event is appended and persisted.
- `Event Log -> State Engine -> Versioning / Validator`: state is projected,
  versioned, and validated from the log.
- `Environment -> Validator -> Recovery Engine`: staleness drives the decision.
- `Action Ledger <-> External Systems`: two-phase side effects (thick arrows),
  reconciled when the outcome is unknown.
- `Recovery Engine -> Resume`: the single sealed next action / recovery context.
- `Security` (dotted): secures the Event Log and Versioning via canonical hashing.
