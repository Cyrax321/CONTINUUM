#!/usr/bin/env bash
# Step 3: final verification after PROMPT 2's agent reports the batch complete.
#
# This is the objective, out-of-band scorecard. It never trusts the agent's
# narration; it reads the outbox, the ledger, and the event chain directly and
# prints PASS/FAIL per check plus an overall verdict.
set -uo pipefail
cd "$(dirname "$0")"
. ./env.sh

pass=0; fail=0
ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

echo "=================================================================="
echo " FINAL VERIFICATION for run '$E2E_RUN_ID'"
echo "=================================================================="

# --- A. Exactly 5 sent files, each sent exactly once ---------------------- #
echo
echo "--- A. Side effects: 5 invoices, each sent exactly once ---------"
sent_files=$(ls -1 "$E2E_OUTBOX"/*.sent 2>/dev/null | wc -l | tr -d ' ')
echo "  outbox .sent files: $sent_files (expected 5)"
if [ "$sent_files" = "5" ]; then ok "all five invoices sent"; else bad "expected 5 sent files, found $sent_files"; fi

# Each file must contain exactly one line: a second send would append/rewrite.
dup=0
for f in "$E2E_OUTBOX"/*.sent; do
  [ -e "$f" ] || continue
  lines=$(wc -l < "$f" | tr -d ' ')
  if [ "$lines" != "1" ]; then echo "    duplicate-send suspect: $(basename "$f") has $lines lines"; dup=$((dup+1)); fi
done
if [ "$dup" = "0" ]; then ok "no invoice was sent twice (every file is single-line)"; else bad "$dup file(s) look re-sent"; fi

# All five expected ids present, no extras.
missing=""
for id in INV-001 INV-002 INV-003 INV-004 INV-005; do
  [ -e "$E2E_OUTBOX/$id.sent" ] || missing="$missing $id"
done
if [ -z "$missing" ]; then ok "exactly the expected invoice ids are present"; else bad "missing:$missing"; fi

# --- B. Ledger agrees: 5 completed, 0 unresolved -------------------------- #
echo
echo "--- B. Ledger: 5 completed actions, 0 unresolved ----------------"
cont actions "$E2E_RUN_ID"
# Capture the JSON to a temp file, then parse it. Piping into `python - <<HEREDOC`
# does not work: the heredoc becomes the script and the piped JSON is lost.
actions_json="$(cont_json actions "$E2E_RUN_ID" 2>/dev/null)"
tmp_actions="$(mktemp)"
printf '%s' "$actions_json" > "$tmp_actions"
counts=$("$E2E_PY" - "$tmp_actions" <<'PY' 2>/dev/null
import sys, json
try:
    with open(sys.argv[1]) as fh:
        acts = json.load(fh)["actions"]
except Exception:
    print("ERR ERR ERR"); raise SystemExit
completed = sum(1 for a in acts if a["status"] == "completed")
unresolved = sum(1 for a in acts if a["status"] in ("started", "unknown")
                 or a.get("side_effect_uncertain"))
print(len(acts), completed, unresolved)
PY
)
rm -f "$tmp_actions"
read -r total comp unres <<EOF
$counts
EOF
echo "  actions total=$total completed=$comp unresolved=$unres"
if [ "$comp" = "5" ]; then ok "ledger shows 5 completed actions"; else bad "ledger completed=$comp (expected 5)"; fi
if [ "$unres" = "0" ]; then ok "no unresolved/uncertain actions dangling"; else bad "unresolved=$unres (expected 0)"; fi

# --- C. Progress projection reads 5 completed ----------------------------- #
echo
echo "--- C. Projected progress: completed == 5 -----------------------"
prog=$(cont_json inspect "$E2E_RUN_ID" 2>/dev/null \
  | "$E2E_PY" -c "import sys,json; p=json.load(sys.stdin)['progress']; print(p['completed'], p.get('total'))" 2>/dev/null \
  || echo "N/A N/A")
echo "  progress completed/total = $prog"
case "$prog" in
  "5 "*) ok "projected progress is 5 completed" ;;
  *)     bad "projected completed != 5 (got: $prog)" ;;
esac

# --- D. Event chain intact across the crash ------------------------------- #
echo
echo "--- D. Event chain integrity ------------------------------------"
verify_out=$(cont verify "$E2E_RUN_ID")
echo "  $verify_out"
if echo "$verify_out" | grep -qi "verified"; then ok "event chain verified, no violations"; else bad "chain verify did not report success"; fi

# --- E. What resume would tell the agent NOW ------------------------------ #
echo
echo "--- E. Post-completion resume contract (informational) ----------"
cont resume "$E2E_RUN_ID" >/dev/null 2>&1; rc=$?
echo "  resume exit code = $rc  (0=safe/resume, 10=repair, 11=replan, 20=wait, 21=human, 30=rollback, 31=unsafe, 2=missing)"
echo "  NOTE: for a batch whose progress was written THROUGH MCP, request_human"
echo "        (exit 21) is the DESIGNED, correct outcome (self-certified state)."
echo "        A trusted-writer run would be exit 0. Judge behaviour, not just code."

# --- Verdict -------------------------------------------------------------- #
echo
echo "=================================================================="
echo " SCORE: $pass passed, $fail failed"
if [ "$fail" = "0" ]; then
  echo " VERDICT: MECHANICS PASS."
  echo " Now confirm the AUTONOMY half by reading the two transcripts:"
  echo "   1) In session 1, did the agent call continuum_record_progress and"
  echo "      route each send through intercept_action -> complete_action"
  echo "      WITHOUT being told to?"
  echo "   2) In session 2, did it call continuum_resume BEFORE sending, and"
  echo "      get proceed=false ('already done') for INV-001..003?"
  echo " Only if BOTH the files/ledger above AND those transcript facts hold is"
  echo " this a true PASS for issue #6. One clean run is an anecdote: repeat 3-5x."
else
  echo " VERDICT: FAIL. See the [FAIL] lines above."
  echo " Common cause: agent solved it by ls-ing the outbox and never used the"
  echo " ledger. Re-run with the hidden-side-effect variant (see README) to force"
  echo " reliance on CONTINUUM."
fi
echo "=================================================================="
[ "$fail" = "0" ]
