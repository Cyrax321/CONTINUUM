# Reduce Per Session Token Floor

Every session pays for the system prompt plus the schemas of all twelve MCP tools, regardless of how little work is done. For a short resume check this dominates cost.

## Current cost

The MCP server exposes twelve tools via `tools/list`. Each schema is sent as part of the model context. The system prompt in `CLAUDE.md` plus twelve schemas is the floor before any user message.

## Proposal

- **Slim resume check.** Add a lightweight `continuum_resume_check` tool that exposes only `resume` and `validate` semantics with a minimal schema, or allow the client to request a filtered `tools/list` with a `subset=resume` query. The server already splits read only vs mutating tools by `read_only_hint`, so a resume only subset is a natural filter.

- **Lazy tool exposure.** Do not send tool schemas until the first `continuum_resume` call has been made. The hook in `src/continuum/hooks.py:1` can run `continuum resume` out of band and inject a pre rendered prompt, so the model never pays for the full schema when the run is already safe to resume.

- **System prompt trim.** Move the long recovery walkthrough out of the system prompt and into a referenced doc, so the per session prompt is a one line pointer plus the resume banner.

No implementation is done here. This is a design note for issue 88, with no external claims.

Reproduce the floor by inspecting `mcp/server.py:1` `tools/list` and by measuring context tokens before the first user message.

## Alternatives

- Keep the full schema always. Simple but keeps the floor high.
- Dedicated lightweight server binary. More to maintain than a filtered list.
