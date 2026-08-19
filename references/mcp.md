# MCP server: status, verification, and open questions

**Crash recovery at startup is fixed and tested.** A server process killed with
`SIGKILL` leaves orphaned `<db>-wal` / `<db>-shm` sidecars that previously made
the next launch fail with `sqlite3.OperationalError: disk I/O error`. The server
opens its store through `_open_server_storage`, which recovers from that error in
stages, least destructive first, because the two sidecars are not equally
expendable:

1. `<db>-shm` is a shared-memory index and genuinely is reconstructable, so it is
   removed and the open retried. When a stale `-shm` was the blocker this is
   lossless — SQLite replays the `-wal` and every committed transaction survives.
2. `<db>-wal` is *not* reconstructable: it holds transactions committed but not
   yet checkpointed into the main database. On a write-heavy run that can be the
   whole history. It is therefore moved aside rather than unlinked, and restored
   if the retry still fails. The server comes up, the committed bytes stay on
   disk, and a warning naming the quarantine path goes to stderr.

An earlier version deleted both sidecars, justified by the claim that they are
reconstructable from the main database. That is true of `-shm` and false of
`-wal`; the audit in [../test.md](../test.md) measured the blast radius on a real
database (4 KB main file, 420 KB WAL holding all 16 events) and found the loss was
both total and silent, since an emptied database still verifies as an intact
chain. Six regression tests in `tests/test_mcp_server.py` now cover staged
recovery, `-shm`-only sufficiency, the log never being unlinked, quarantine not
clobbering an earlier crash's evidence, restoration when quarantining does not
help, and the re-raise when there is nothing to clear. Recorded in CHANGELOG.md
under Fixed.

**The server is verified usable through Claude Code.** Registered as an MCP
server, it reports `✔ Connected`, exposes all ten tools with the correct
read-only/mutating split, and the full `continuum_record_progress` to
`continuum_checkpoint` to `continuum_intercept_action` to
`continuum_complete_action` to `continuum_resume` cycle returns correct,
durable JSON. Authorization denies by default. That claim is proven end to end
over the real stdio protocol, and the unit suite covers every tool.

**The surface has been audited adversarially.** Beyond confirming the documented
behaviour, the audit tested the dangerous inverse of each claim — not only that
duplicate side effects are suppressed, but that genuinely new work is not — and
verified every result against the SQLite store instead of trusting tool output.
It found that `env` supplied to `continuum_checkpoint` was recorded as a snapshot
only, so environment drift was rendered in `environment_changes` while the verdict
stayed `safe`; the tool now declares each pinned resource as a
`DEPENDENCY_DECLARED` event so the diff has something to invalidate. Full method
and per-claim results: [../test.md](../test.md).
