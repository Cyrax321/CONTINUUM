#!/bin/bash
# CONTINUUM - one-command demo. Run:  ./try-it.sh
cd "$(dirname "$0")" || exit 1

# macOS re-hides uv's .pth files, which makes the venv's Python skip them.
# A symlink into site-packages is immune to that. The venv's layout names its
# Python version, so every site-packages is handled rather than one pinned
# version (#840: 3.14 was hardcoded while the support matrix is 3.11-3.13).
for SP in .venv/lib/python3*/site-packages; do
  [ -d "$SP" ] || continue
  [ -e "$SP/continuum" ] || ln -sfn "$PWD/src/continuum" "$SP/continuum"
  chflags nohidden "$SP"/*.pth 2>/dev/null
done

export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="$PWD/src"

case "${1:-demo}" in
  demo)  python examples/crash_recovery_agent.py ;;
  test)  python -m pytest ;;
  cli)   shift; continuum "$@" ;;
  shell) echo "PATH and PYTHONPATH set. Try: continuum --help"; exec "$SHELL" ;;
  *)     echo "usage: ./try-it.sh [demo|test|cli ...|shell]" ;;
esac
