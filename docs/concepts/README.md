# CONTINUUM Concepts

One page per mechanism, describing what it does to the run rather than how to
configure it. Use these when you need the model behind a verdict. For
copy-paste wiring see [docs/guides/README.md](../guides/README.md), and for the
call signatures see [docs/api/README.md](../api/README.md).

- [Constraint pinning](constraint-pinning.md) - session-start standing constraints ("never send
  without confirmation") recorded as first-class verifiable events, so briefing, compaction,
  and context truncation cannot drop them. Covers the pin, verify, and release lifecycle, the
  grace window, and strict-mode escalation.

The guide that shows pinning wired into harness hooks with hash-only storage is
[docs/guides/constraint-pinning.md](../guides/constraint-pinning.md). The scope it does and
does not catch is in [docs/threat_model.md](../threat_model.md).
