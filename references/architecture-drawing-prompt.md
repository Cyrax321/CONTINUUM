# CONTINUUM architecture diagram: drawing brief for an image AI

Copy the entire contents of this file into an image-generation model (for
example GPT-Image, Gemini, or Claude) and ask it to draw the diagram. Every
label, count, and relationship below is verified against the source code, so
instruct the model to use these names exactly and not to invent or rename
anything.

---

## 1. What to draw

A single, professional system-architecture diagram for a software framework
called CONTINUUM. It must be clean, modern, and technical (like the architecture
diagrams in Stripe or Tailscale docs): white background, soft card shadows,
rounded rectangles, consistent sans-serif typography, generous whitespace, and a
clear top-to-bottom flow that uses the full width of a wide canvas
(roughly 16:9, e.g. 1600 x 900 or wider).

Do NOT draw isometric or 3D shapes. Do NOT use photos. Do NOT add any
component, company logo, or text that is not listed below.

---

## 2. Color palette (exact hex)

- Brand blue: #3B82F6 (primary containers, client/adapters tier)
- Light blue tint: #62AFE0, #F0F7FC (container fills)
- Accent orange: #FF5017 (highlights, the Event Log, the resume arrow, the
  MCP pill)
- Light orange tint: #FFF3EC (SDK container fill)
- Navy text: #071827
- Muted text: #6B7D8D
- Border gray: #E7ECEF
- White cards: #FFFFFF

---

## 3. Layout (six horizontal tiers, top to bottom)

Tier 1 - Agents and clients (4 cards in a row)
Tier 2 - Framework adapters, optional (3 cards in a row)
Tier 3 - CONTINUUM SDK (one large rounded container holding a 3x3 grid of
        subsystem cards, plus an MCP pill at top-right)
Tier 4 - Durable Storage and External Systems (2 cards side by side)
Tier 5 - Resume (1 centered card at the bottom)

Draw arrows (see section 5) connecting the tiers.

---

## 4. Exact node labels

### Tier 1: Agents and clients
Four equal cards:
- Claude Code
- LangGraph agent
- OpenAI agent
- Generic Python agent

### Tier 2: Framework adapters (optional)
Three equal cards (use these exact class names):
- GenericAgentAdapter
- OpenAIAgentAdapter
- LangGraphAgentAdapter

### Tier 3: CONTINUUM SDK (large container labeled "CONTINUUM SDK")
Inside, a 3x3 grid of cards:

Row 1:
- Extractors  (subtitle: Deterministic, LLM optional)
- Event Log   (subtitle: hash-chained, 29 event types)  <- HIGHLIGHT this card
  with an orange border; it is the source of truth
- Action Ledger (subtitle: claim, perform, complete)

Row 2:
- State Engine (subtitle: projection of events)
- Checkpoint Manager (subtitle: 6 policies)
- Environment (subtitle: snapshot + diff)

Row 3:
- Validator (subtitle: staleness propagation)
- Recovery Engine (subtitle: 7 modes, sealed contract)
- Security (subtitle: canonical hashing, provenance)

MCP pill (small rounded bar at the top-right inside the SDK container):
- Text: "MCP server: deny by default, 12 tools (3 read-only, 9 mutating)"

### Tier 4: two cards side by side
Left card:
- Durable Storage (subtitle: SQLite, WAL, synchronous FULL)
Right card:
- External Systems (subtitle: GitHub, email, APIs)

### Tier 5: one centered card
- Resume (subtitle: bounded recovery context)

---

## 5. Arrows (edges) with labels

- Agents tier -> Adapters tier (no label, or "calls")
- Adapters tier -> CONTINUUM SDK container (no label, or "calls")
- From inside SDK, Event Log -> Durable Storage (label: "events + state")
- From inside SDK, Action Ledger -> External Systems (thick orange arrow,
  label: "side effects (two-phase)")
- From External Systems -> Action Ledger (thick orange arrow, label: "outcome /
  probe")
- From inside SDK, Recovery Engine -> Resume (central orange arrow, label:
  "resume / recovery")
- Inside the SDK, draw a subtle arrow from Event Log toward Durable Storage to
  show persistence.

Do not draw arrows between every pair of cards; only the relationships above.

---

## 6. Facts the model must respect (do not contradict)

- The 9 MCP tool names are exactly, with the continuum_ prefix:
  continuum_validate, continuum_resume, continuum_list_actions (these three are
  read-only), and continuum_record_progress, continuum_checkpoint,
  continuum_intercept_action, continuum_complete_action, continuum_fail_action,
  continuum_reconcile_action (these six mutate). Do not drop the prefix.
- The 7 recovery modes, in order of increasing caution: RESUME,
  REPAIR_AND_RESUME, REPLAN, WAIT, REQUEST_HUMAN, ROLLBACK, ABORT.
- The 6 checkpoint policies: ManualPolicy, IntervalPolicy, EventPolicy,
  SemanticPolicy, ContextPressurePolicy, HybridPolicy (Hybrid is the default).
- The 3 reconcilers: ProbeReconciler, ManualReconciler, AssumeNotOccurredReconciler.
  There is no "AssumeOccurred".
- The 29 event types (do not list them all on the diagram; this is just to
  prevent invention): RUN_STARTED, RUN_COMPLETED, RUN_ABORTED, TASK_UPDATED,
  TOOL_CALLED, TOOL_COMPLETED, TOOL_FAILED, DECISION_CREATED,
  DECISION_INVALIDATED, EVIDENCE_ADDED, FINDING_ADDED, FINDING_INVALIDATED,
  WORK_ADDED, WORK_COMPLETED, DEPENDENCY_DECLARED, APPROVAL_REQUESTED,
  APPROVAL_GRANTED, APPROVAL_REVOKED, MODEL_CHANGED, MODEL_ASSUMPTION_RECORDED,
  STATE_CHECKPOINTED, STATE_VALIDATED, ENVIRONMENT_CHANGED, RECOVERY_STARTED,
  RECOVERY_COMPLETED, RECOVERY_BLOCKED, ACTION_RECORDED, ACTION_RECONCILED,
  ACTION_COMPENSATED.
- The SemanticState fields (do not all need to appear; for correctness): goal,
  progress, plan, decisions, findings, evidence, pending_work, approvals,
  external_dependencies, model.
- Storage is SQLite with WAL and synchronous=FULL; corruption is refused on read.

---

## 7. Title and footer

Optional small title at top-left of the canvas:
"CONTINUUM - SYSTEM ARCHITECTURE" (use a hyphen or middle dot, not an em dash).

Optional small footer at the bottom center:
"SDK uses only the Python standard library; the mcp extra adds the server."

---

## 8. One-line prompt you can paste with this brief

"Draw the professional system-architecture diagram described in the attached
brief. Follow every label, count, and arrow exactly. Use the specified color
palette. Do not add, rename, or remove any component."
