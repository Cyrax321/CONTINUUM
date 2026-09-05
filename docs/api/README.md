# CONTINUUM API Reference

This is the programmatic API reference for CONTINUUM. The `docs/` root holds the
marketing site; this folder documents the library and CLI that you integrate
with. Everything here is generated from the public surface of the `continuum`
package.

CONTINUUM gives a long-running agent a durable, tamper-evident recovery layer:
it checkpoints semantic state, records side effects in an idempotent ledger,
validates a run against its environment, and decides how (and whether) the agent
may resume. The integration points, in order of most to least common, are:

- **Adapters** (`continuum.adapters`) wrap your agent loop or framework so
  checkpointing, interception, and resume happen automatically.
- **Action ledger** (`continuum.actions.ledger`) records side effects so a
  restarted agent never repeats an effect it already performed.
- **Checkpoints** (`continuum.checkpoint`) persist state on a policy.
- **Recovery engine** (`continuum.recovery`) decides the safe resume mode.
- **Validator** (`continuum.state.validator`) checks state against the live
  environment.
- **MCP server** (`continuum.mcp`) exposes the same operations to any MCP client
  (for example Claude Code) over stdio, SSE, or HTTP.
- **Security** (`continuum.security`, `continuum.mcp.authz`) signs chain
  attestations and authenticates callers.
- **CLI** (`continuum`) is the command line surface, also usable in scripts.

## Modules

- [Adapters](adapters.md) - GenericAgentAdapter, LangGraph, OpenAI, LangChain
- [Action ledger](ledger.md) - ActionLedger, ActionOutcome
- [Checkpoints](checkpoints.md) - CheckpointManager, RestoredRun
- [Recovery](recovery.md) - RecoveryEngine, RecoveryDecision
- [Validation](validation.md) - StateValidator, validate_state, ValidationOutcome
- [MCP server](mcp.md) - continuum-mcp, the twelve tools, build_server
- [Security](security.md) - attestation and caller authentication
- [CLI](cli.md) - the `continuum` command reference

## Install

```bash
pip install continuum-agent          # core, no optional dependencies
pip install continuum-agent[attest]  # adds cryptographic attestation
pip install continuum-agent[mcp]     # adds the MCP server
pip install continuum-agent[langgraph]  # adds the LangGraph adapter
pip install continuum-agent[openai]  # adds the OpenAI Agents SDK adapter
```

All imports below assume `import continuum` and the relevant submodule.
