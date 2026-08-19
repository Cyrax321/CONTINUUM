# CLI

The `continuum` command is the command-line surface, also usable in scripts. Exit
codes are a safety contract: only a verified-safe run exits `0`, so
`continuum resume "$RUN" && ./start-agent.sh` cannot launch onto stale state.

```bash
continuum --db continuum.db <command> [args]
continuum --json <command>      # machine-readable output
```

## Commands

| Command | Purpose |
|---------|---------|
| `init` | Initialize storage for a new database. |
| `runs` | List runs and their status. |
| `inspect <run_id>` | Show a run's goal, progress, and metadata. |
| `history <run_id>` | List checkpoints for a run. |
| `events <run_id>` | Dump the raw event log for a run. |
| `diff <run_id>` | Show the environment/state diff since the last checkpoint. |
| `validate <run_id>` | Check state against the current environment. |
| `resume [run_id]` | Assess and describe how the run may resume. Omit `run_id` to resume the most recently active run. Prints the run's `goal` so you know what to continue. |
| `confirm <run_id>` | Confirm a human-approved recovery step. |
| `checkpoint <run_id>` | Force a state checkpoint. |
| `verify <run_id>` | Re-audit the event chain for tampering. |
| `actions <run_id>` | List recorded side effects and flag uncertain outcomes. |
| `show-contract <run_id>` | Print the recovery contract for the run. |
| `replay <run_id>` | Replay events and verify the stored state version. |
| `benchmark` | Run the CONTINUUM-Bench harness. |
| `attest-keygen` | Generate an Ed25519 signer key pair (PEM files). |
| `attest <run_id>` | Sign a run's event chain into an attestation document. |
| `attest-verify <run_id> --attest <file>` | Verify a signed attestation against the live chain. |
| `serve` | Run the Tier 0 newline-delimited JSON sidecar (no MCP dependency). |

## Examples

```bash
# Is it safe to continue?
continuum resume run_42

# What changed since the last checkpoint?
continuum diff run_42

# Prove the chain is untampered, then capture a signed attestation
continuum verify run_42
continuum attest run_42 --key signer.pem --out run_42.attest.json
continuum attest-verify run_42 --attest run_42.attest.json
```

Most commands accept `--db` (storage path or URL) and `--json`. Colour is
TTY-aware and respects `NO_COLOR`; piped output is byte-identical to uncoloured
output.
