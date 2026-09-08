# Franta implementation decisions

This file records choices that `Design.md` intentionally leaves to the
implementation and the clarifications approved on 2026-08-18. It is not a new
mathematical policy.

## Mechanical implementation choices

- Runtime prompts state each role's goal, evidence and access boundaries, required skill timing,
  and control response in compact form. Each `SKILL.md` keeps decision rules and staging guidance
  close at hand; complex payload details use small linked references copied with the skill.
  Mechanical validation remains in the runtime and scheduler rather than being repeated as
  prompt prose.
- Python 3.11 or newer and only the standard library are required at runtime.
- A TOML bootstrap manifest supplies paths, retry limits, model settings, tool
  paths, and context budgets. Main, trimmer, verifier, challenge-verifier, and closure-review calls
  use the persisted default model settings; synthesizer calls use the persisted synthesizer
  settings. Ordinary workers, proof-writers, discovery-sprint lanes, and the fresh sprint
  summarizer instead use the fixed `gpt-6-astra`/`max` route. This needs no new manifest field, so
  existing persisted runtime configurations remain compatible. The complete manifest is
  digest-bound at bootstrap, so its persisted model settings cannot be changed in place.
  Legacy `gpt-5.6-sol` settings map to `gpt-6-astra` at launch with the same reasoning effort;
  the persisted configuration is unchanged. All Franta, Explorer, and Advisor Codex launches
  set `model_context_window = 872000` and `model_auto_compact_token_limit = 780000`.
- One SQLite database in WAL mode is the durable control plane and canonical
  data source. Deterministic Markdown files are committed projections for agent
  reading and human inspection. A projection outbox makes a crash between a
  database commit and `os.replace` recoverable.
- JSON values stored in SQLite are canonicalized before hashing. IDs use their
  required type prefix plus a monotonically allocated, zero-padded integer.
  IDs are never reused, including after rollback, withdrawal, or revocation.
- Optimistic revisions protect mutable routes, obligations, categories, and
  portfolio snapshots. Scheduler control state uses compare-and-swap revisions.
- Search uses SQLite FTS/BM25 when available and a deterministic token BM25
  fallback otherwise. Search returns summaries or abstracts. Complete fetches
  are separate audited actions.
- Agent-readable workspaces contain only task cards, permitted portfolio
  snapshots, summaries/abstracts, and proposal staging. They never contain the
  scheduler database, task archives, other workspaces, or the canonical store.
- Project-wide memory access is provided through the audited `internal-search`
  gateway rather than a canonical-memory filesystem mount. The same skill owns
  both abstract search and access-checked full-record fetch.
- Codex JSONL events provide durable session IDs and tool-call audit data.
  Every logical call is recorded before launch with its exact input digest and
  lease epoch. Resumption uses the persisted session ID only when the role's
  session policy allows it.
- Every invocation uses a private Franta `CODEX_HOME` and a fresh process
  environment. A worker with `if_resume: null` starts a new session lineage;
  a validated `if_resume: T-*` continues that task's persisted session.
  Session IDs remain scheduler-private.
- Franta disables Codex's own sub-agent surface with `agents.enabled=false` in addition to the
  legacy multi-agent feature flags. Before a production launch, the transport renders the
  model-visible prompt in the private environment and fails closed if collaboration instructions
  remain. Worker delegation therefore stays exclusively with the scheduler.
- Main and trimmer use the separate audited `task-search` gateway for closed
  task summaries and, only when needed, content-addressed archived artifacts.
  Every fetched artifact is hash-checked.
- Codex permission profiles deny scheduler-private and canonical paths, deny
  command/subprocess networking, and make only the assigned workspace writable.
  Native Codex web search remains enabled for every main-agent, trimmer, and
  worker call. Model transport and scheduler infrastructure traffic are not
  agent-controlled network access.
- Codex output is streamed into a durable per-call log while the process runs.
  Worker calls have a fixed four-hour watchdog. Optional
  `timeouts.agent_call_seconds` supplies the default watchdog for other Codex calls and
  confined CAS/Tectonic subprocesses. Cancellation records its reason before terminating
  the process group.
- Configured Sage, Macaulay2, and additional CAS names are exposed only through a launch-bound
  audited tool; executable paths stay scheduler-private. On macOS each run uses a shell-free,
  default-deny Seatbelt child profile with no network, workspace-bounded writes, and narrowly
  allowed runtime reads. Platforms without both filesystem and network confinement fail closed.
  Inputs, versions, environment metadata, outputs, errors, exit status, artifacts, and random
  seed are staged for scheduler validation.
- Tectonic uses the same constrained runner for human-guidance reports. A request is accepted
  only after the source compiles to a validated PDF, and the PDF is copied into the private
  content-addressed task archive before the workspace can disappear.
- Every scheduler-consumed staged skill artifact is authenticated by a scheduler-private broker
  receipt binding the call, launch capability, skill, operation, exact path, bytes, payload, and
  result. Agent-writable workspace receipts are diagnostic only. A broker-completed record remains
  recoverable if the Codex process disconnects before emitting its terminal tool event.
- Task closure freezes a durable close intent, publishes the canonical task memory first, then
  transitions to `closed` and applies an idempotent post-close tail. Recovery resumes each phase,
  including migration of legacy closed tasks whose canonical task publication was missing.
- Explorer alternation is enabled only by an explicit `[explorer]` manifest table. Its scratch,
  summaries, directions, trusted-receipt visibility, CAS evidence, frozen turns, and promotion
  ledger use a separate append-only database under `private/`; `ES-*`, `ESUM-*`, and `XCAS-*`
  never enter the canonical memory-type or ID contracts. The first attempt of each lineage has a
  mechanically clean-room policy. Later attempts receive a high-water-bound joint search over
  canonical summaries and provisional Explorer records.
- Explorer itself is a standalone leaf package at `src/explorer_system/`. That copy boundary owns
  worker/main-sort prompt templates, the fixed max model route, response schemas, settings,
  skill assets and payload contracts, the append-only record/CAS/frozen-turn repository, joint
  search ports, lineage reducers, alternation reducers, the worker-wave program, and the frozen
  handoff DTO. It imports no Franta module and can be copied and imported with no Franta package
  present. `src/franta/explorer_adapter.py` is the named integration seam: it binds Franta IDs,
  read policy, model DTOs, workspaces, calls, trusted receipts, and the sort-task handoff. Old
  `franta.explorer*` and control paths are compatibility re-exports only; the three workspace-level
  skill paths are symlinks to the block's single packaged asset copy.
- The portable repository accepts collaborator ID patterns at construction and uses neutral
  handoff/export APIs. The Franta repository adapter alone supplies canonical ID rules and the
  compatibility names `sort`, `promotion`, and canonical computation publication. Likewise,
  Explorer's main-sort template receives collaborator tool names explicitly; Franta supplies
  `internal-search`, `record-progress`, and its provenance envelope through the adapter.
- The 2-hour Explorer and 8-hour Franta durations are admission windows. The initial Explorer
  clock begins only after the foreground run reaches stable bootstrap, so initialization delay
  consumes no admission time. A lineage
  admitted before closure keeps its reserved slot through exactly three attempts of at most three
  hours; after Explorer drain, a one-shot task-bound `main-sort` call receives every trusted
  summary and scratch in a version-fixed, read-only filesystem snapshot and selectively promotes
  useful material. It may inspect that snapshot with local read-only commands or helper scripts,
  but receives no live Explorer search/fetch capability. The sorter uses a cycle-scoped session;
  its ordinary planning continuation uses the persistent `main:project` session, starting it
  on the first Main call and resuming it thereafter. The persisted `post_sort_call_id` binds
  that planning call to the completed sorter. Franta deadline
  closure mechanically closes work that had not launched, prevents new assignments and retries,
  and lets already-running attempts plus their downstream publication work drain.
- Every selected main-sort operation carries server-validated Explorer provenance. Cross-call
  computation promotion accepts only successful trusted `XCAS-*` evidence cited by a frozen
  source record; the scheduler reconstructs and normalizes the computation body from private
  receipts. Textual calculation scratch cannot become canonical `C-*` memory. Unselected records
  remain provisional and are not converted to fallback memos.

## Approved clarifications

- The scheduler mechanically requires a hash-matched `task-writing` artifact for every
  main-agent assignment (including a proof-writer assignment) and for every discovery-sprint
  lane before accepting the corresponding control result. It requires a hash-matched final
  `record-progress` artifact before a worker attempt may end normally. An interrupted process
  remains an interruption. No other skill-timing rule is made mandatory by the scheduler.
- Fact metadata and non-fact memory may contain a task-owned typed relationship to an unpublished
  Fact, route, memo, claim, or obligation proposal, including from another progress record. The
  scheduler publishes the source with the raw temporary ID visible but inactive in its original
  field. Publication, deduplication, or update-merging atomically replaces it with the canonical
  ID; terminal failure or task-final nonpublication instead renders `TMP-ID(unpublished)` in the
  same list, scalar endpoint, or nested relation position. Neither state creates an active index,
  and soft references never block task closure or schedule a correction.
- A temporary fact predecessor is different: `predecessor_fact_ids` is the sole authority for
  fact dependencies, and the dependent fact is not sent to synthesis or verification until every
  listed predecessor resolves. Resolution replaces only entries in that list in a new immutable
  normalized candidate version; statement and proof text are neither scanned nor rewritten and
  need not contain literal predecessor IDs. Terminal rejection recursively rejects every still-
  unpublished direct or transitive dependent. An exact occurrence of a known temporary Fact ID
  in the statement or proof is a separate publication gate, not a dependency edge; success leaves
  the text unchanged and terminal failure rejects the containing Fact and its dependent chain.
- A verifier receives the exact candidate identity and semantic bundle,
  complete declared predecessor records, foundation policy, external references,
  and the prior verifier report on a revision. An unchanged rejected semantic
  bundle returns to correction without another verifier call.
- A fact duplicating active mathematical content may nevertheless be published
  as a new immutable fact when a newly verified `root_resolution` requires it.
- The first committed verified root resolution controls completion. Later
  same-outcome resolutions are alternate proofs. An opposite outcome creates
  `needs_attention` and freezes completion for human review.
- The root obligation keeps its original ID and receives a derived `resolved`
  status. Revocation of the resolving fact or a predecessor restores it to
  active rather than manufacturing a new root ID.
- Root resolution supersedes unlaunched trim, guidance, and sprint control work.
  Already launched tasks and their ingestion drain, but no new sprint summary
  or trimmer continuation is launched.
- Prepared, running, and completed main/trimmer control calls are recovered
  from their persisted exact inputs. Completed results commit idempotently;
  lost calls are fenced and relaunched under their persisted retry policy.
- Main and trimmer transport retries are positive manifest settings, defaulting
  to three. Exhaustion preserves state, records `needs_attention`, and returns
  control to the operator.
- One trimmer round is one top-level review/trim including its sprint or human
  continuation. One session handles three rounds total; round four is fresh.
- When an atomic main-agent batch crosses the eight-assignment boundary, the
  trimmer receives every assignment report accumulated since its previous
  review, then the counter resets.
- The six-resume lineage cap counts explicit cross-task `if_resume` launches.
  Same-task interruption retries and verifier-revision attempts do not consume
  it.
- Revoking a published fact does not force a repair assignment. The main agent
  receives the event and retains responsibility for the next assignment.
- Removed obligations leave historical relation and category entries visible
  through inactive overlays. Current route links and active indexes are cleaned.
- A corrected computation is a new `C-*` record; the original remains history.
- Each worker Fact submission uses fresh candidate and proposal IDs with version 1. Temporary Fact
  predecessors resolve in `predecessor_fact_ids` before a scheduler-owned immutable normalized
  successor is reviewed by the synthesizer and verifier; normalization never changes the
  statement or proof. A repair uses fresh worker IDs and cannot revive the rejected proposal.
- The nine memory-operation kinds enumerated in Sections 8.3 and 9.3 are
  implemented; the word “eight” is treated as an editorial counting error.
- A hash-authenticated final `record-progress` file whose process disconnects
  before its matching final response keeps its operations but cannot close the
  attempt normally; the attempt follows the interruption retry path.
- The top-level and attempt-summary completion-evidence lists are duplicate-free string-ID sets
  containing the same IDs. The skill boundary and scheduler independently validate this; list
  order has no semantic meaning.
- Temporary-reference inspection and exceptional explicit resolution remain audited operator
  commands with caller-supplied operation IDs. Normal source-task completion deterministically
  annotates any still-unpublished soft target instead of waiting indefinitely.
- Agent response schemas are recursively closed. Dynamic category revisions
  use closed entry arrays, and verifier, closure, and sprint subrecords retain
  every Design field.

## Approved access interpretation

- Every worker receives the self-contained root problem, including isolated
  workers.
- Main agent and trimmer have project-wide audited search/fetch access.
- Research, associate, reformulate, and ordinary computation workers have the
  same project-wide access. A verifier can search and fetch facts only.
- Brainstorm and multi-discipline workers have no project-memory search. The
  stricter discovery-sprint rules also remove search from its computation and
  associate lanes. These workers receive only sealed materialized input.
- A verifier initially receives the submitted statement and proof, dependency
  IDs, foundation policy, and external references. It sees fact abstracts
  through `internal-search` and fetches complete fact records only as needed.
- A proof-writer receives the root-resolution fact and audited on-demand access
  to its transitive active-fact closure. Closure materialization does not count
  against the assignment portfolio's twenty-memory limit.
- A discovery-sprint summarizer receives only the frozen sealed synthesis input
  and no project-memory access.
- Native web search is available to all workers, main agents, and trimmers,
  including memory-isolated workers. It never grants project-memory access.
