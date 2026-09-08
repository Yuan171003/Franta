# Local research dashboard

`dashboard_system` is an independent, copyable program alongside the research
systems. It imports none of Franta, Explorer, or Advisor. A host implements the
three protocols in `interfaces.py`:

- `DashboardReadPort` supplies current status, effective main-memory records and
  relations, trusted Explorer scratch/summary pages, and a monitor snapshot.
- `OperatorCommandPort` accepts explicit human submissions. Commands belong to
  the host; the dashboard never changes research databases or schedules workers.
- `MonitorPort` summarizes a copied snapshot into at most five directions and
  five research-quality assessments, each citing real snapshot record IDs.
  Each assessment contains an integer score from 1 to 10 and a brief English
  reason. The dimensions are creativity, synthesis, sustained work on critical
  obstacles, major breakthrough potential, and proof closure potential. Its
  result is display-only.

`DashboardServer` binds only `127.0.0.1`, starts at port 1113, and increments the
port only when it is occupied. Its three routes are `/`, `/main-memory`, and
`/explorer-memory`. JavaScript, CSS, math rendering and fonts are served locally.
HTTP reads use the read port; submissions require the local session token and
same-origin checks. Paging, filtering and graph expansion do not invoke a model.

`ResearchMonitor` runs independently with at most one model call in flight. A
manual refresh requests a new summary; concurrent clicks join that request.
Automatic refresh is every 5400 seconds while research is active or awaiting
human feedback. Stopped/completed projects still support manual refresh. Failed
refreshes preserve the previous result and show an error. Monitor records and
cache files live in the supplied dashboard cache directory, outside research
memory, and model costs are displayed separately.

The Franta Monitor keeps one persistent model conversation per Explorer/Franta
cycle. Refreshes and dashboard restarts resume that exact conversation through
Explorer admission, drain, sorting, and the Franta turn. Only the next cycle
starts a new conversation. Projects without alternation keep one conversation.
Each refresh supplies a fresh copied snapshot with cycle context and a list of
added, updated, and removed records. Per-cycle session state and locks live in
`private/dashboard/monitor-sessions/`; per-call snapshots, recoverable CLI events,
and usage remain in `private/dashboard/monitor-runs/`. A failed resume reports
an error without silently creating a replacement conversation.

The homepage displays all five quality scores with reasons and evidence links.
The assessment's cycle and timestamp identify older results while a new refresh
is pending or has failed. Existing direction-only caches remain readable until
a fresh assessment is available. New results must contain all five assessments;
invalid scores or nonexistent citations preserve the previous cached result.

The Franta adapter is `franta/dashboard_adapter.py`; the read adapter is
`franta/dashboard_read.py`. It uses short read-only SQLite transactions and the
existing pure record decoder, without constructing a writable runtime or
repository. Normal SQLite reader lock coordination remains enabled for WAL
correctness. Neither a dashboard nor a monitor failure stops research.

The dashboard survives a research pause. Advisor feedback is first queued in
`private/dashboard/commands/`. The current runner consumes it at its own boundary.
If that runner has stopped, a continuation process first acquires the project
lock, validates and records commands, and resumes only accepted feedback for
the current waiting Advisor report. A historical or rejected command cannot
restart unrelated research. Human guidance uses the existing immutable Markdown
inbox and does not by itself resume a stopped project.

Token totals come from reported transport usage, including separate physical
retries, with cached input shown as a subset of input. Missing reports remain
unknown. Active time is the union of observed call intervals, avoiding double
counting parallel workers; elapsed time spans the observed run history. The
currently displayed Explorer direction is the most recent trusted report from
that attempt; an unreported direction is explicitly marked as such.
