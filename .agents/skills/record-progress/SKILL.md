---
name: record-progress
description: Stage immutable worker progress, proposals, challenges, and the attempt-final summary.
---

# Record progress

Stage durable worker output whenever you prove a claim or make significant progress, not for
routine notes. Call exactly once with `is_final: true` before an attempt ends normally. Sequence
numbers start at 1, increase by one within the attempt, and no call follows the final record.

Each call includes the progress since the previous call and may include:

- `operations`: memory proposals with unique `operation_id` values;
- `computation_operation_ids` naming staged CAS results used by this progress (never inline
  computation bodies); and
- `fact_challenges`, each with a unique `challenge_id`, fact ID, precise alleged proof failure,
  and optional evidence.

Use the compact field contract in [references/payload.md](references/payload.md) when staging an
operation, challenge, computation link, or final record.

The nine operation kinds are `fact`, `route_add`, `route_update`, `memo`, `claim_add`,
`claim_remove`, `obligation_add`, `obligation_update`, and `obligation_remove`. Add proposals use
a temporary `proposal_id`. For worker submissions, the fact ID is `candidate_id`: use a fresh
`candidate_id`, fresh `proposal_id`, and `candidate_version: 1` for every fact operation. Never
reuse a `candidate_id` in another operation or progress file, including for a corrected proof,
self-revision, or verifier-requested repair. If a staged fact needs correction, submit the
corrected fact under entirely fresh IDs and leave the earlier candidate for the scheduler and
verifier to resolve. Updates declare the target, expected base revision, an incremental
`set`/`append`/`add_ids`/`remove_ids` patch, an explanation, and supporting IDs. Missing patch
fields mean “unchanged.” A typed non-fact relationship may use an unpublished proposal's
temporary ID across progress records. Put it only in that relationship field, never in prose,
mathematical content, or another untyped field. The scheduler publishes the source with the link
pending and inactive, then atomically replaces it with the canonical ID when the target publishes
or deduplicates. A rejected target must be corrected or explicitly abandoned; there is no timeout.

For every Obligation relation, `premise_memory_ids` must be a nonempty, exhaustive list of all
actual Fact or Obligation premises on the left-hand side. The enclosing Obligation is not an
implicit premise. If that Obligation is itself mathematically a premise, include it explicitly:
use its canonical ID if it already exists, or, in an `obligation_add` operation, use that
operation's own temporary `proposal_id`.

Only active facts and the frozen project foundations are established premises. Routes, memos,
claims, obligations, and computations are exploratory. Small proved results normally become
claims. Propose a fact only for an important or reusable advance, with a precise statement,
complete proof, explicit fact dependencies, and precise external references. Cite each
predecessor fact ID where used; do not cite a claim, route, memo, obligation, or computation as a
proved premise; reproduce any needed argument in the proof. Refer only to materialized IDs or IDs
obtained through your authorized search/fetch. Declare `root_resolution` only for a proof or
disproof of `ROOT`.

Before proposing a new route or obligation, use `internal-search` when available. Read abstracts
first and fetch a plausible same record only when needed. If it already represents the work,
update it only for genuinely new progress or perspective; otherwise submit nothing. Isolated
modes must not search and may submit directly.

A final call sets `outcome_status` to `finished` (objective achieved), `progress` (objective not
achieved but significant progress made), or `failed` (normal exit without either); `interrupted`
belongs to the scheduler. Set top-level `completion_evidence_ids` to the operations required to
justify completion. Also supply `attempt_summary` with `work_mode`, `task`, matching
`proposed_outcome`, cumulative important progress, the same duplicate-free string IDs as
`completion_evidence_operation_ids` (order does not matter), and most promising next steps.

Call the launch-bound `record_progress` tool with the record as its `payload`.

For the special `main-sort` role only, every operation also carries exactly
`explorer_provenance: {"sort_run_id": "...", "source_record_ids": ["ES-...", ...]}`.
To promote a computation already executed by Explorer, use
`explorer_computation_promotions` entries containing only `evidence_id` (`XCAS-*`) and
`source_record_ids`. The scheduler reconstructs the computation from its private trusted CAS
receipt. This field is forbidden to ordinary workers, and textual computation scratch is not
eligible for canonical computation memory.

This writes only to the task outbox. The scheduler validates proposals, verifies fact candidates,
resolves temporary IDs, and is the sole canonical-memory writer.

## Immutable artifacts

Treat each `record-progress` call as a publication boundary: the runtime may snapshot every current file under `artifacts/`, not only files named in the payload. After calling it, never modify, replace, delete, or recreate an existing artifact path. Keep drafts in `tmp/` and publish revisions under fresh versioned paths. Never manually edit `outbox/` or CAS/`record-progress` outputs.
