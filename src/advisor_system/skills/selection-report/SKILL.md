---
name: selection-report
description: Persist one Advisor proposal containing exactly five ranked mathematical obligations and a human-readable selection report, then end the proposal call.
---

# Selection report

Use this skill only in the proposal half of a launch-bound Advisor round. Read the immutable
Advisor context, the complete host-authorized memory snapshot, and all prior problem assignments
before calling the tool. The memory material is evidence for research planning; retain its status
and provenance distinctions.

Propose exactly five ranked obligations that are *most worth trying*, in rank order 1 through 5. Choose the obligations according the following rules:
    - Each obligation must identify the nearest unresolved bottleneck on a high-leverage route and contain exactly one primary mathematical unknown. It *must not* package several mathematical problems together into a single obligation. 
    - Each obligation should be an unresolved important bottleneck, and if solved, it would make solving the ROOT significantly easier.
    - Penalize a lineage that has been studied actively and successively by Franta, or keeps producing new gadget, analogy, reformulation, or additional prerequisite, but still makes *no decisive progress*. However, do not penalize it if it does make truly significant progress
    - An obligation should not merely restate the original problem. 
    - Some obligations should provide an innovative and uncommon way to solve the ROOT, instead of following the most natural ways.
    - Do not reuse a previously assigned subproblem unless it remains really important and the memory snapshot contains a breakthrough that now makes it *significantly easier*; when reusing one, cite the previous assignment and the breakthrough evidence and explain the exception.
    - Penalize an obligation that is a decomposition of a previously assigned subproblem, unless it is truly important for solving the ROOT itself.

Normally exclude every previously assigned subproblem. Repeat one only when both conditions hold:
it remains exceptionally important, and specific new research is a breakthrough that makes the
subproblem significantly easier. In that case, include the prior assignment IDs, a substantive
exception justification, and the immutable evidence IDs supporting the breakthrough. Similar
wording does not evade this rule. Every cited evidence record must have been created or advanced
to a newer canonical revision after each prior assignment that the obligation names; the
launch-bound tool checks this against host-authenticated snapshot history.

After completing the mathematical analysis, read [references/payload.md](references/payload.md)
and call `selection_report` exactly once with the complete structured payload. The launch-bound
tool validates the context bindings and ranking, writes the human-facing report, and returns its
artifact identity and digest. Do not write the report by any other route.

Then return only the proposal response naming that artifact and feedback request. End the call.
Do not select an obligation, create the next problem assignment, solicit feedback inside the
Advisor session, or continue working. The scheduler alone may resume this same session after it
has durably bound human feedback.
