You are responsible for rolling out 5 microservices (auth, billing, search,
notify, gateway) in a way that is safe to interrupt and resume.

Use ONLY the CONTINUUM MCP tools available to you. Do not use any other tool.
Manage the process however you see fit, but design it so that if your process
were killed mid-rollout, another agent could resume at exactly the right point
and never redeploy an already-deployed service.

Concrete requirements:

- The run id is RUN_ID. Use it for every tool call.
- Make the rollout durable and resumable: if you are interrupted and a new
  session starts, it must be able to tell what is already done.
- Never record or re-attempt a side effect (a "deploy") that has already
  happened.

Decide the order and the cadence of checkpoints yourself. There is no prescribed
sequence; just make it correct and resumable. When you are finished, report a
short summary of what you deployed and the final state.
