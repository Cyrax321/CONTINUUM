#!/usr/bin/env bash
#
# Setup Anya and Yuki agents on a GitHub repository.
#
# Usage:
#   export AGENTS_ANYA_TOKEN=ghp_...
#   export AGENTS_OPENCODE_KEY=sk-...
#   export AGENTS_YUKI_TOKEN=ghp_...
#   export AGENTS_OPENCODE_KEY_YUKI=sk-...
#   ./scripts/setup-agents.sh OWNER/REPO
#
# What it does:
#   1. Invites anya-research and <yuki-user> as write collaborators
#   2. Sets 4 secrets on the target repo
#   3. Commits .github/workflows/anya.yml and yuki.yml via the GitHub API
#
# Notes:
#   - Keys are read from env vars only; never hardcode them here.
#   - Collaborator invites must be accepted by each bot account before
#     their tokens can post comments.
#   - To customize triggers/models, edit WORKFLOW_TEMPLATES below.

set -euo pipefail

REPO="${1:?Usage: $0 OWNER/REPO}"
YUKI_USER="${AGENTS_YUKI_USER:-yuki-research}"

: "${AGENTS_ANYA_TOKEN:?Set AGENTS_ANYA_TOKEN}"
: "${AGENTS_OPENCODE_KEY:?Set AGENTS_OPENCODE_KEY}"
: "${AGENTS_YUKI_TOKEN:?Set AGENTS_YUKI_TOKEN}"
: "${AGENTS_OPENCODE_KEY_YUKI:?Set AGENTS_OPENCODE_KEY_YUKI}"

echo "==> Inviting bot collaborators to $REPO"
for bot in anya-research "$YUKI_USER"; do
  gh api -X PUT "repos/$REPO/collaborators/$bot" -f permission=push >/dev/null \
    && echo "    invited $bot (must accept via email/github.com)"
done

echo "==> Setting secrets"
gh secret set OPENCODE_API_KEY --repo "$REPO" --body "$AGENTS_OPENCODE_KEY"
gh secret set ANYA_TOKEN --repo "$REPO" --body "$AGENTS_ANYA_TOKEN"
gh secret set OPENCODE_API_KEY_YUKI --repo "$REPO" --body "$AGENTS_OPENCODE_KEY_YUKI"
gh secret set YUKI_TOKEN --repo "$REPO" --body "$AGENTS_YUKI_TOKEN"

put_file() {
  local path="$1" branch="$2"
  local content_b64
  content_b64=$(base64 < "$path")
  # Check if file exists to build the proper payload
  local sha=""
  sha=$(gh api "repos/$REPO/contents/$path?ref=$branch" --jq '.sha' 2>/dev/null || true)
  if [ -n "$sha" ]; then
    gh api -X PUT "repos/$REPO/contents/$path" \
      -f message="ci: update $(basename "$path")" \
      -f content="$content_b64" -f branch="$branch" -f sha="$sha" >/dev/null
  else
    gh api -X PUT "repos/$REPO/contents/$path" \
      -f message="ci: add $(basename "$path")" \
      -f content="$content_b64" -f branch="$branch" >/dev/null
  fi
  echo "    wrote $path"
}

echo "==> Writing workflow files to default branch"
DEFAULT_BRANCH=$(gh api "repos/$REPO" --jq '.default_branch')
TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

cat > "$TMPDIR/anya.yml" <<'EOF'
name: anya

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

jobs:
  anya:
    if: |
      contains(github.event.comment.body, ' /anya') ||
      startsWith(github.event.comment.body, '/anya')
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: write
      pull-requests: write
      issues: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          persist-credentials: false

      - name: Run Anya
        uses: anomalyco/opencode/github@latest
        env:
          OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY }}
          GITHUB_TOKEN: ${{ secrets.ANYA_TOKEN }}
        with:
          model: opencode/muse-spark-1.2-contributor-free
          use_github_token: true
          mentions: "/anya"
          share: false
EOF

cat > "$TMPDIR/yuki.yml" <<'EOF'
name: yuki

on:
  issue_comment:
    types: [created]
  pull_request_review_comment:
    types: [created]

jobs:
  yuki:
    if: |
      contains(github.event.comment.body, ' /yuki') ||
      startsWith(github.event.comment.body, '/yuki')
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: write
      pull-requests: write
      issues: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6
        with:
          persist-credentials: false

      - name: Run Yuki
        uses: anomalyco/opencode/github@latest
        env:
          OPENCODE_API_KEY: ${{ secrets.OPENCODE_API_KEY_YUKI }}
          GITHUB_TOKEN: ${{ secrets.YUKI_TOKEN }}
        with:
          model: opencode/muse-spark-1.2-contributor-free
          use_github_token: true
          mentions: "/yuki"
          share: false
EOF

put_file ".github/workflows/anya.yml" "$DEFAULT_BRANCH"
put_file ".github/workflows/yuki.yml" "$DEFAULT_BRANCH"

echo ""
echo "==> Done for $REPO. Remaining manual steps:"
echo "    1. Both bots accept their collaborator invites"
echo "    2. Test: comment '/anya explain' and '/yuki explain' on any issue"
