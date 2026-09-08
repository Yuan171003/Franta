---
name: check-result
description: Check whether one strict mathematical proposition has an established matching result without browsing host memory.
---

# Check result

Use this skill only when progress requires knowing whether one precise mathematical proposition
has been proved, disproved, or computed. State exactly one complete proposition; do not submit a
topic, a list, an open-ended literature query, or a request for related methods. Calls are not
capped.

Call `check_result` with the proposition and the requested result kind. Follow the closed
interface in [references/api.md](references/api.md). The broker searches only the authorized
fact, claim, and computation snapshot and returns at most the three closest matches.

Every returned match has the single status `established`. Results deliberately omit any field
that distinguishes a fact from a claim, as well as source-memory IDs and relations. Use the
mathematical content as a known result, but do not infer anything from an empty response beyond
the absence of a sufficiently close match in this frozen snapshot.

This skill is read-only and proposition-scoped. It does not provide general memory search,
fetch, pagination, publication, verification, or mutation. Do not try to enumerate memory or
open its storage through the filesystem or shell.
