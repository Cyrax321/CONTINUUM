// CONTINUUM — Interactive Application Logic

document.addEventListener('DOMContentLoaded', () => {
  initSimulator();
  initDiffViewer();
  initCalculator();
  initCodeTabs();
  initQuickstartCopy();
  initArchTooltips();
  initCustomCursor();
  initFaqAccordion();
  initLiveTime();
  initStudioTime();
  initRuler();
});

// ---------------------------------------------------------------------------
// Custom Cursor
// ---------------------------------------------------------------------------
function initCustomCursor() {
  const cursor = document.createElement('div');
  cursor.className = 'custom-cursor';
  cursor.innerHTML = `
    <svg class="cursor-arrow" width="12" height="16" viewBox="0 0 12 16" fill="none">
      <path d="M2 2L12 11L7 11L5 16L2 2Z" fill="white" stroke="#071827" stroke-width="1"/>
    </svg>
    <div class="cursor-label">You</div>
  `;
  document.body.appendChild(cursor);

  let mouseX = window.innerWidth / 2;
  let mouseY = window.innerHeight / 2;
  let cursorX = mouseX;
  let cursorY = mouseY;

  document.addEventListener('pointermove', (e) => {
    mouseX = e.clientX;
    mouseY = e.clientY;
  });

  document.addEventListener('pointerover', (e) => {
    const target = e.target.closest('a, button, [role="button"], .faq-item, .clickable');
    if (target) {
      cursor.classList.add('clickable');
      cursor.querySelector('.cursor-label').textContent = 'click';
    } else {
      cursor.classList.remove('clickable');
      cursor.querySelector('.cursor-label').textContent = 'You';
    }
  });

  function animate() {
    cursorX += (mouseX - cursorX) * 0.12;
    cursorY += (mouseY - cursorY) * 0.12;
    cursor.style.transform = `translate3d(${cursorX}px, ${cursorY}px, 0)`;
    requestAnimationFrame(animate);
  }

  requestAnimationFrame(animate);
}

// ---------------------------------------------------------------------------
// FAQ Accordion
// ---------------------------------------------------------------------------
function initFaqAccordion() {
  const faqItems = document.querySelectorAll('.faq-item');

  faqItems.forEach(item => {
    const plusBtn = item.querySelector('.faq-plus');
    if (!plusBtn) return;

    plusBtn.addEventListener('click', () => {
      const isOpen = item.classList.contains('open');

      faqItems.forEach(other => {
        other.classList.remove('open');
        const btn = other.querySelector('.faq-plus');
        if (btn) {
          btn.textContent = '+';
          btn.setAttribute('aria-expanded', 'false');
        }
      });

      if (!isOpen) {
        item.classList.add('open');
        plusBtn.textContent = '−';
        plusBtn.setAttribute('aria-expanded', 'true');
      }
    });
  });
}

// ---------------------------------------------------------------------------
// Live Time
// ---------------------------------------------------------------------------
function initLiveTime() {
  function update() {
    const el = document.getElementById('liveTime');
    if (el) el.textContent = new Date().toLocaleTimeString('en-US', { hour12: true });
  }
  update();
  setInterval(update, 1000);
}

// ---------------------------------------------------------------------------
// Studio Time (IST)
// ---------------------------------------------------------------------------
function initStudioTime() {
  function update() {
    const el = document.getElementById('footerTime');
    if (!el) return;
    const now = new Date();
    const utc = now.getTime() + now.getTimezoneOffset() * 60000;
    const ist = new Date(utc + 5.5 * 3600000);
    const hh = ist.getHours().toString().padStart(2, '0');
    const mm = ist.getMinutes().toString().padStart(2, '0');
    el.textContent = `${hh}:${mm} IST`;
  }
  update();
  setInterval(update, 60000);
}

// ---------------------------------------------------------------------------
// Ruler
// ---------------------------------------------------------------------------
function initRuler() {
  const ruler = document.getElementById('topRuler');
  if (!ruler) return;
  const width = ruler.offsetWidth || 800;
  const spacing = 30;
  let html = '';
  for (let i = 0; i < width; i += spacing) {
    const isMajor = i % 100 === 0;
    html += `<div style="position:absolute;bottom:0;left:${i}px;width:1px;height:${isMajor ? '10px' : '6px'};background:rgba(7,19,30,0.2)"></div>`;
    if (isMajor && i > 0) {
      html += `<div style="position:absolute;bottom:12px;left:${i}px;font-family:'JetBrains Mono',monospace;font-size:8px;color:rgba(7,19,30,0.4);transform:translateX(-50%)">${i}</div>`;
    }
  }
  ruler.innerHTML = html;
}

// ---------------------------------------------------------------------------
// 1. Live Fault Recovery Simulator Engine
// ---------------------------------------------------------------------------
const scenarioData = {
  crash: {
    badgeClass: "ok",
    badgeText: "RESUME [SAFE]",
    terminal: `<span class="hl-dim">$ continuum resume run_4821</span>

<span class="hl-bold">CONTINUUM RECOVERY ENGINE v0.1.0</span>
Run ID: run_4821
Checkpoint Version: v17 (SHA-256: 8f3a92b1...)
Event Log Chain Audit: <span class="hl-green">INTEGRITY_VERIFIED (102/102 events trusted)</span>

<span class="hl-dim">--- State Audit ---</span>
<span class="hl-green">[VALID]</span> Goal: "Analyze 10,000 research documents"
<span class="hl-green">[VALID]</span> Progress: 3,421 completed, 6,579 pending
<span class="hl-green">[VALID]</span> 127 findings preserved (100% evidence verified)
<span class="hl-green">[VALID]</span> 14 decisions valid
<span class="hl-green">[VALID]</span> Action Ledger: 8 side effects verified (0 duplicated)

<span class="hl-dim">--- Recovery Decision ---</span>
Recovery Safety: <span class="hl-green">SAFE_TO_RESUME</span>
Mode: RESUME
Next permitted action: process_batch(start_index=3422)

<span class="hl-green">✓ Task resumed from verified progress. Zero duplicated work.</span>`,

    stateJson: `{
  "run_id": "run_4821",
  "goal": {
    "description": "Analyze 10,000 research documents",
    "version": 1
  },
  "progress": { "completed": 3421, "pending": 6579, "failed": 0 },
  "decisions": [
    {
      "decision_id": "dec_014",
      "decision": "Include peer-reviewed meta-analyses",
      "status": "valid",
      "evidence": ["ev_088", "ev_089"]
    }
  ],
  "findings_count": 127,
  "source_sequence": 102
}`,

    contractJson: `{
  "run_id": "run_4821",
  "recovery_status": "safe_to_resume",
  "verified": ["goal", "progress", "decisions", "evidence"],
  "invalidated": [],
  "required_actions": [],
  "next_allowed_action": "process_batch(3422)"
}`
  },

  dataset: {
    badgeClass: "warn",
    badgeText: "REPAIR_AND_RESUME",
    terminal: `<span class="hl-dim">$ continuum resume run_4821</span>

<span class="hl-bold">CONTINUUM RECOVERY ENGINE v0.1.0</span>
Run ID: run_4821
Checkpoint Version: v17

<span class="hl-dim">--- Environment Snapshot Audit ---</span>
<span class="hl-green">[VALID]</span> Goal & Progress verified
<span class="hl-red">[FAIL] Resource Mismatch</span>: Dependency 'dataset' changed from version 'v3' -> 'v4'
<span class="hl-amber">[STALE]</span> 4 findings tied to dataset v3 marked REQUIRES_REVALIDATION
<span class="hl-red">[INVALIDATED]</span> Decision #7 ('Only subset A analyzed') marked INVALID

<span class="term-dim">--- Recovery Contract Generated ---</span>
Recovery Safety: <span class="hl-amber">REQUIRES_REPAIR</span>
Mode: REPAIR_AND_RESUME
Invalidated Components: ["dataset_v3", "decision_7"]
Required Actions: ["revalidate experiments 14-17 against dataset v4"]
Next Allowed Action: dataset_revalidation

<span class="hl-amber">⚠ Environment shift detected. Unsafe replay prevented by contract.</span>`,

    stateJson: `{
  "run_id": "run_4821",
  "external_dependencies": [
    {
      "resource": "dataset",
      "version": "v4",
      "status": "conflicted",
      "metadata": { "previous_version": "v3" }
    }
  ],
  "decisions": [
    {
      "decision_id": "dec_007",
      "decision": "Only subset A analyzed",
      "status": "invalidated",
      "invalidated_reason": "Dependency dataset updated v3 -> v4"
    }
  ]
}`,

    contractJson: `{
  "run_id": "run_4821",
  "recovery_status": "requires_repair",
  "verified": ["goal", "completed_documents_1_to_3400"],
  "invalidated": ["dataset_v3", "decision_7"],
  "required_actions": ["revalidate_experiments_14_to_17"],
  "next_allowed_action": "dataset_revalidation"
}`
  },

  model: {
    badgeClass: "warn",
    badgeText: "MODEL TRANSITION",
    terminal: `<span class="hl-dim">$ continuum resume run_4821 --model claude-3-5-sonnet</span>

<span class="hl-bold">CONTINUUM RECOVERY ENGINE v0.1.0</span>
Run ID: run_4821
Checkpoint Version: v17

<span class="hl-dim">--- Model Transition Audit ---</span>
Previous Model: gpt-4o (provider: openai)
Target Model: claude-3-5-sonnet (provider: anthropic)

<span class="hl-amber">[MODEL_SPECIFIC_STATE]</span> Assumption 'prompt_format_v1' requires re-eval
<span class="hl-green">[VALID]</span> Task Goal, Verified Progress, Findings, and Evidence preserved
<span class="hl-cyan">[RECONSTRUCTION]</span> Bounded recovery context reconstructed (3,800 tokens vs 182,000 transcript)

<span class="hl-dim">--- Recovery Decision ---</span>
Recovery Safety: <span class="hl-green">SAFE_TO_RESUME</span>
Mode: RESUME (Framework-Agnostic Context Transferred)

<span class="hl-green">✓ Switched models safely without replaying full prompt transcript.</span>`,

    stateJson: `{
  "run_id": "run_4821",
  "model": {
    "model": "claude-3-5-sonnet",
    "provider": "anthropic",
    "model_specific_state": [
      {
        "item_id": "model_state_001",
        "description": "GPT-4 prompt formatting assumption",
        "required_validation": "Must be revalidated after model change"
      }
    ]
  }
}`,

    contractJson: `{
  "run_id": "run_4821",
  "recovery_status": "safe_to_resume",
  "verified": ["goal", "progress", "evidence"],
  "invalidated": ["model_specific_state_001"],
  "required_actions": ["reevaluate_prompt_format"],
  "next_allowed_action": "continue_execution"
}`
  },

  sideeffect: {
    badgeClass: "err",
    badgeText: "HUMAN REVIEW",
    terminal: `<span class="hl-dim">$ continuum resume run_4821</span>

<span class="hl-bold">CONTINUUM ACTION LEDGER RECONCILIATION</span>
Run ID: run_4821
Last Action: action_812 ("github.create_issue")

<span class="hl-red">[UNKNOWN_SIDE_EFFECT]</span> Process killed during external write call.
Action status: STARTED (completion unconfirmed by server ack).

<span class="hl-dim">--- Reconciliation Guard ---</span>
Action ID: action_812
Arguments Hash: e3b0c442...
Outcome: UNCERTAIN

Recovery Safety: <span class="hl-red">REQUIRES_HUMAN</span>
Mode: REQUEST_HUMAN
Action: Execution halted to prevent duplicate issue creation.

<span class="hl-red">× Automatic retry blocked to protect external system idempotency.</span>`,

    stateJson: `{
  "action_id": "action_812",
  "action_type": "github.create_issue",
  "arguments_hash": "e3b0c442...",
  "status": "started",
  "side_effect_uncertain": true
}`,

    contractJson: `{
  "run_id": "run_4821",
  "recovery_status": "requires_human",
  "verified": ["prior_actions_1_to_811"],
  "invalidated": [],
  "required_actions": ["human_reconcile_action_812"],
  "next_allowed_action": null
}`
  }
};

let currentScenario = 'crash';
let currentTab = 'terminal';

function initSimulator() {
  const navBtns = document.querySelectorAll('.sim-nav-btn');
  const tabBtns = document.querySelectorAll('.sim-tab-btn');

  if (!navBtns.length) return;

  navBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const scenarioKey = btn.getAttribute('data-scenario');
      if (!scenarioData[scenarioKey]) return;

      navBtns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');

      currentScenario = scenarioKey;
      renderSimulatorOutput();
    });
  });

  tabBtns.forEach(btn => {
    btn.addEventListener('click', () => {
      const tabKey = btn.getAttribute('data-tab');
      tabBtns.forEach(t => t.classList.remove('active'));
      btn.classList.add('active');

      currentTab = tabKey;
      renderSimulatorOutput();
    });
  });

  renderSimulatorOutput();
}

function renderSimulatorOutput() {
  const outputEl = document.getElementById('simOutputArea');
  if (!outputEl) return;

  const data = scenarioData[currentScenario];
  if (!data) return;

  if (currentTab === 'terminal') {
    outputEl.innerHTML = data.terminal;
  } else if (currentTab === 'state') {
    outputEl.textContent = data.stateJson;
  } else if (currentTab === 'contract') {
    outputEl.textContent = data.contractJson;
  }
}

// ---------------------------------------------------------------------------
// 2. Interactive Checkpoint Diff Engine
// ---------------------------------------------------------------------------
const diffLeftState = `{
  "checkpoint_id": "chk_v16",
  "version": 16,
  "progress": { "completed": 3400, "pending": 6600 },
  "external_dependencies": [
    { "resource": "dataset", "version": "v3" }
  ],
  "decisions": [
    { "id": "dec_007", "decision": "Subset A filter", "status": "valid" }
  ]
}`;

const diffRightState = `{
  "checkpoint_id": "chk_v17",
  "version": 17,
  "progress": { "completed": 3421, "pending": 6579 },
  "external_dependencies": [
    <span class="diff-mod">~ "resource": "dataset", "version": "v4" (CHANGED)</span>
  ],
  "decisions": [
    <span class="diff-del">- { "id": "dec_007", "status": "invalidated" } (INVALIDATED)</span>,
    <span class="diff-add">+ { "id": "dec_014", "decision": "Peer review filter", "status": "valid" }</span>
  ],
  "findings": [
    <span class="diff-add">+ { "id": "finding_127", "claim": "Correlation verified", "confidence": 0.94 }</span>
  ]
}`;

function initDiffViewer() {
  const leftEl = document.getElementById('diffLeftBox');
  const rightEl = document.getElementById('diffRightBox');

  if (leftEl && rightEl) {
    leftEl.textContent = diffLeftState;
    rightEl.innerHTML = diffRightState;
  }
}

function initCalculator() {
  const agentsRange = document.getElementById('calcAgentsRange');
  const turnsRange = document.getElementById('calcTurnsRange');
  const hiddenModelSelect = document.getElementById('calcModelSelect');
  const crashRange = document.getElementById('calcCrashRange');

  const agentsDisplay = document.getElementById('calcAgentsDisplay');
  const turnsDisplay = document.getElementById('calcTurnsDisplay');
  const modelDisplay = document.getElementById('calcModelDisplay');
  const crashDisplay = document.getElementById('calcCrashDisplay');

  const costSavedDisplay = document.getElementById('calcCostSavedDisplay');
  const ratioDisplay = document.getElementById('calcRatioDisplay');
  const tokensSavedDisplay = document.getElementById('calcTokensSavedDisplay');
  const actionsDisplay = document.getElementById('calcActionsDisplay');
  const hoursDisplay = document.getElementById('calcHoursDisplay');
  const speedupDisplay = document.getElementById('calcSpeedupDisplay');

  if (!agentsRange || !turnsRange || !hiddenModelSelect || !crashRange) return;

  const btnToggleFormula = document.getElementById('btnToggleFormula');
  const formulaBox = document.getElementById('formulaBox');

  if (btnToggleFormula && formulaBox) {
    btnToggleFormula.addEventListener('click', () => {
      const isHidden = formulaBox.style.display === 'none';
      formulaBox.style.display = isHidden ? 'block' : 'none';
      btnToggleFormula.classList.toggle('active');
    });
  }

  function update() {
    const agents = parseInt(agentsRange.value, 10);
    const turns = parseInt(turnsRange.value, 10);
    const costPerM = parseFloat(hiddenModelSelect.value) || 3.0;
    const crashRate = parseInt(crashRange.value, 10) / 100.0;

    agentsDisplay.textContent = `${agents} runs/day`;
    turnsDisplay.textContent = `${turns} turns`;
    
    // Model display badge
    const activeOpt = document.querySelector('.select-option.active');
    const modelName = activeOpt ? activeOpt.getAttribute('data-name') : 'Custom Model';
    modelDisplay.textContent = modelName;
    crashDisplay.textContent = `${Math.round(crashRate * 100)}% failure`;

    // Monthly runs = agents * 30 days
    const monthlyRuns = agents * 30;
    const monthlyCrashes = Math.max(1, Math.round(monthlyRuns * crashRate));

    // Raw transcript tokens per turn = ~1,500 tokens.
    // On crash, naive replay d...
    const rawReplayTokensPerCrash = 0.5 * turns * 1500;
    const recoveryTokens = 3500;

    // Net tokens saved per crash recovery
    const netTokensSavedPerCrash = Math.max(0, rawReplayTokensPerCrash - recoveryTokens);
    const totalTokensSavedMo = netTokensSavedPerCrash * monthlyCrashes;

    // Context compression ratio
    const ratio = ((turns * 1500) / recoveryTokens).toFixed(1);

    // Dollar cost saved per month
    const dollarsSavedMo = (totalTokensSavedMo / 1000000) * costPerM;

    // Duplicate side effects prevented = avg 1.5 external tool calls per crash
    const duplicateActions = Math.round(monthlyCrashes * 1.5);

    // Developer hours saved debugging corrupt state (0.5 hrs / crash)
    const hoursSaved = Math.round(monthlyCrashes * 0.5);

    // Speedup factor
    const speedup = Math.round((turns * 0.4) / 0.4);

    // Render formatted numbers
    costSavedDisplay.textContent = `$${Math.round(dollarsSavedMo).toLocaleString()}`;
    ratioDisplay.textContent = `${ratio}x`;

    if (totalTokensSavedMo >= 1000000000) {
      tokensSavedDisplay.textContent = `${(totalTokensSavedMo / 1000000000).toFixed(1)}B`;
    } else if (totalTokensSavedMo >= 1000000) {
      tokensSavedDisplay.textContent = `${(totalTokensSavedMo / 1000000).toFixed(1)}M`;
    } else {
      tokensSavedDisplay.textContent = `${Math.round(totalTokensSavedMo / 1000)}k`;
    }

    actionsDisplay.textContent = duplicateActions.toLocaleString();
    hoursDisplay.textContent = `${hoursSaved} hrs`;
    speedupDisplay.textContent = `${speedup}x`;
  }

  // Initialize custom dropdown component
  initCustomModelSelector(update);

  [agentsRange, turnsRange, crashRange].forEach(input => {
    input.addEventListener('input', update);
    input.addEventListener('change', update);
  });

  update();
}

function initCustomModelSelector(onModelChange) {
  const wrapper = document.getElementById('customModelWrapper');
  const trigger = document.getElementById('customModelTrigger');
  const dropdown = document.getElementById('customModelDropdown');
  const label = document.getElementById('customModelSelectedLabel');
  const hiddenInput = document.getElementById('calcModelSelect');
  const customPriceContainer = document.getElementById('customPriceInputContainer');
  const customPriceInput = document.getElementById('customPriceInput');

  if (!wrapper || !trigger || !dropdown) return;

  trigger.addEventListener('click', (e) => {
    e.stopPropagation();
    wrapper.classList.toggle('open');
  });

  document.addEventListener('click', (e) => {
    if (!wrapper.contains(e.target)) {
      wrapper.classList.remove('open');
    }
  });

  const options = dropdown.querySelectorAll('.select-option');
  options.forEach(opt => {
    opt.addEventListener('click', () => {
      options.forEach(o => o.classList.remove('active'));
      opt.classList.add('active');

      const inVal = opt.getAttribute('data-in');
      const outVal = opt.getAttribute('data-out');
      const name = opt.getAttribute('data-name');

      if (inVal === 'custom') {
        customPriceContainer.style.display = 'block';
        const customVal = parseFloat(customPriceInput.value) || 3.0;
        hiddenInput.value = customVal;
        label.textContent = `${name} ($${customVal.toFixed(2)} / 1M)`;
      } else {
        customPriceContainer.style.display = 'none';
        const priceIn = parseFloat(inVal);
        const priceOut = parseFloat(outVal);
        // Blended token cost (80% input, 20% output)
        const blended = priceIn * 0.8 + priceOut * 0.2;
        hiddenInput.value = blended;
        label.textContent = `${name} ($${priceIn.toFixed(2)} in / $${priceOut.toFixed(2)} out)`;
      }

      wrapper.classList.remove('open');
      if (onModelChange) onModelChange();
    });
  });

  if (customPriceInput) {
    ['input', 'change'].forEach(evtName => {
      customPriceInput.addEventListener(evtName, () => {
        const val = parseFloat(customPriceInput.value) || 0.1;
        hiddenInput.value = val;
        label.textContent = `Custom Model ($${val.toFixed(2)} / 1M)`;
        if (onModelChange) onModelChange();
      });
    });
  }
}

// ---------------------------------------------------------------------------
// 4. Quickstart Code Tabs & Copy
// ---------------------------------------------------------------------------
const codeSnippets = {
  sdk: `# CONTINUUM Python SDK
from continuum.storage.sqlite import SQLiteStorage
from continuum.checkpoint import CheckpointManager
from continuum.recovery import RecoveryEngine
from continuum.actions import ActionLedger
from continuum.models import Goal, Progress, SemanticState

store = SQLiteStorage("sqlite:///agent.db")
manager = CheckpointManager(store)

# Record a checkpoint of semantic state
state = SemanticState(
    run_id="run_4821",
    goal=Goal("Analyze 10,000 research documents"),
    progress=Progress(completed=3421, pending=6579),
)
manager.checkpoint("run_4821", state=state, reason="milestone")

# Record external side effects idempotently
ledger = ActionLedger(store, "run_4821")
ledger.claim("github.create_issue", arguments={"title": "Bug found"})

# Decide how to resume after a crash
decision = RecoveryEngine(store).assess("run_4821")
if decision.safe:
    print("safe to resume:", decision.next_allowed_action)`,

  cli: `# CONTINUUM CLI

# Initialize storage
continuum init

# Force a checkpoint for a run
continuum checkpoint run_4821

# Validate state against the live environment
continuum validate run_4821

# Inspect state at a specific version
continuum inspect run_4821 --version 17

# Diff two state versions
continuum diff 16 17

# Decide how to resume safely after a crash
continuum resume run_4821

# Confirm self-reported progress so the run may resume
continuum confirm run_4821`,

  extractor: `# Custom deterministic state extractor
from continuum.state import StateExtractor, ExtractionContext
from continuum.models import Goal, SemanticState

class CustomExtractor:
    name = "custom"

    def extract(self, context: ExtractionContext) -> SemanticState:
        # context.trajectory holds the run's recorded events.
        decisions = [
            e.payload for e in context.trajectory
            if e.type.value == "DECISION_CREATED"
        ]
        return SemanticState(
            run_id=context.run_id,
            goal=Goal("recovered goal"),
            decisions=[...],  # build Decision objects from the decisions list
        )`,

  events: `# Hash-Chained Event Log Audit
from continuum.events import EventLog

log = EventLog()

# Append sealed (SHA-256 chained) events
event1 = log.append(run_id="run_4821", type="RUN_STARTED")
event2 = log.append(run_id="run_4821", type="DECISION_CREATED", payload={"claim": "x"})

# Verify chain integrity (tamper detection)
report = log.verify(run_id="run_4821")
print(f"Chain healthy: {report.ok}")
print(f"Trusted through sequence: {report.trusted_through['run_4821']}")`
};

function initCodeTabs() {
  const tabs = document.querySelectorAll('.code-tab-btn');
  const codeBody = document.getElementById('codeBody');
  const copyBtn = document.getElementById('copyBtn');

  if (!tabs.length || !codeBody) return;

  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const tabKey = tab.getAttribute('data-tab');
      if (!codeSnippets[tabKey]) return;

      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');

      codeBody.textContent = codeSnippets[tabKey];
    });
  });

  if (copyBtn) {
    copyBtn.addEventListener('click', () => {
      navigator.clipboard.writeText(codeBody.textContent).then(() => {
        const originalText = copyBtn.textContent;
        copyBtn.textContent = 'Copied!';
        showCopiedToast('Copied to clipboard');
        setTimeout(() => copyBtn.textContent = originalText, 2000);
      });
    });
  }
}

// ---------------------------------------------------------------------------
// 4b. Quickstart Static Code Blocks Copy
// ---------------------------------------------------------------------------
function showCopiedToast(message) {
  let toast = document.getElementById('copyToast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'copyToast';
    toast.className = 'copy-toast';
    document.body.appendChild(toast);
  }
  toast.textContent = message || 'Copied to clipboard';
  toast.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toast.classList.remove('show'), 1600);
}

function initQuickstartCopy() {
  const buttons = document.querySelectorAll('.qs-copy');

  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      const pre = btn.parentElement.querySelector('pre.qs-code');
      if (!pre) return;
      const text = pre.textContent;

      const done = () => {
        const original = btn.textContent;
        btn.textContent = 'Copied';
        btn.classList.add('copied');
        showCopiedToast('Copied to clipboard');
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove('copied');
        }, 1500);
      };

      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(fallback);
      } else {
        fallback();
      }

      function fallback() {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); done(); } catch (e) { /* no-op */ }
        document.body.removeChild(ta);
      }
    });
  });
}

// ---------------------------------------------------------------------------
// 5. Architecture Hover Tooltips
// ---------------------------------------------------------------------------
function initArchTooltips() {
  const nodes = document.querySelectorAll('.arch-node');
  const titleEl = document.getElementById('archInfoTitle');
  const descEl = document.getElementById('archInfoDesc');
  const btnToggleFlow = document.getElementById('btnToggleFlow');
  const flowsGroup = document.querySelector('.arch-pulses');

  if (btnToggleFlow && flowsGroup) {
    btnToggleFlow.addEventListener('click', () => {
      flowsGroup.classList.toggle('paused');
      btnToggleFlow.classList.toggle('active');
      if (flowsGroup.classList.contains('paused')) {
        btnToggleFlow.innerHTML = '<span>▶</span> Resume Flow Animation';
      } else {
        btnToggleFlow.innerHTML = '<span class="pulse-dot"></span> Live Data Flow Animation';
      }
    });
  }

  if (!nodes.length || !titleEl) return;

  const nodeInfo = {
    agent: {
      title: "AI Agent Engine",
      desc: "Any agent framework (LangGraph, OpenAI SDK, custom python agent). Interacts exclusively through the lightweight CONTINUUM SDK."
    },
    stateEngine: {
      title: "State Projection Engine",
      desc: "Folds the append-only event log into a compact, versioned SemanticState tree (goals, findings, evidence, decisions)."
    },
    ledger: {
      title: "Idempotent Action Ledger",
      desc: "Tracks external API calls and side-effects. Intercepts duplicate calls on recovery and returns original cached results."
    },
    evidence: {
      title: "Evidence Registry",
      desc: "Maintains checksums and source references for all claims and decisions asserted by the agent."
    },
    checkpoint: {
      title: "Semantic Checkpoint",
      desc: "The minimal verified task snapshot. Replaces massive raw conversation dumps with versioned structured state."
    },
    validator: {
      title: "Environment Validator",
      desc: "Compares saved checkpoint against live environment (file hashes, dataset versions, permissions) before resuming."
    },
    contract: {
      title: "Recovery Contract",
      desc: "Deterministic machine-readable contract stipulating state validity, invalidated items, and next allowed recovery actions."
    }
  };

  nodes.forEach(node => {
    node.addEventListener('mouseenter', () => {
      const key = node.getAttribute('data-node');
      const info = nodeInfo[key];
      if (info) {
        titleEl.textContent = info.title;
        descEl.textContent = info.desc;
      }
    });
  });
}

// ---------------------------------------------------------------------------
// 6. Scroll Animations & UI Enhancements
// ---------------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
  initScrollEffects();
});

function initScrollEffects() {
  // Scroll progress bar
  const progressBar = document.getElementById('scrollProgress');
  const backToTop = document.getElementById('backToTop');
  const navbar = document.querySelector('.navbar-wrapper');

  window.addEventListener('scroll', () => {
    const scrollTop = window.scrollY;
    const docHeight = document.documentElement.scrollHeight - window.innerHeight;
    const progress = docHeight > 0 ? (scrollTop / docHeight) * 100 : 0;
    if (progressBar) progressBar.style.width = progress + '%';

    // Navbar scrolled state
    if (navbar) {
      if (scrollTop > 20) navbar.classList.add('scrolled');
      else navbar.classList.remove('scrolled');
    }

    // Back to top visibility
    if (backToTop) {
      if (scrollTop > 600) backToTop.classList.add('visible');
      else backToTop.classList.remove('visible');
    }
  }, { passive: true });

  // Back to top click
  if (backToTop) {
    backToTop.addEventListener('click', () => {
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  // Scroll reveal animations
  const reveals = document.querySelectorAll('.reveal');
  if (!reveals.length) return;

  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -40px 0px' });

  reveals.forEach(el => observer.observe(el));
}
