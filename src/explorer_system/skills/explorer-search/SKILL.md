---
name: explorer-search
description: Search authorized host memory and provisional Explorer scratch together, then fetch selected full records through an audited read-only broker.
---

# Explorer search

Use this skill only when it is materialized for the current Explorer call. Under the staged
access policy, it is the full-memory interface for attempt 3; attempts 1 and 2 receive narrower
skills and must not bypass their absence through the filesystem or shell. A full-memory attempt
may search the six authorized host-memory types together with provisional scratch and Explorer
summaries. A recovery-only legacy call may also receive this skill under its frozen v1 policy.

Call `explorer_search` with a mathematical query, a nonempty list of `record_types`, and a limit
of at most 10. Read returned abstracts, provenance, and status labels first. Call
`explorer_fetch` only for a result whose full record is needed. Use the precise interface in
[references/api.md](references/api.md).

Among all returned material, only active canonical facts and the frozen project foundations are
established premises. Routes, memos, claims, obligations, computations, and every Explorer record
remain exploratory; both `scratch` and `summary` are provisional even when they contain a
purported proof. Treat computations as evidence for idea generation, not as proof.

The broker fixes the visible canonical revision and scratch cutoff at launch, enforces the
Explorer lineage and phase scope, and audits search and fetch. This skill is read-only: it cannot
edit, withdraw, promote, verify, or publish any record. Never open canonical or scratch storage
directly. `scratch` and `summary` are Explorer record types, not additional canonical host-memory
types.
