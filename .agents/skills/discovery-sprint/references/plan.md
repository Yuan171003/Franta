# Sprint plan contract

Stage either:

- `{"decision":"no_sprint","reason":"..."}`, or
- a `{"decision":"plan", ...}` object with `target_obligation_id`,
  `target_obligation_revision`, the exact
  self-contained `target_statement`, the repeated mechanism and unchanged obstacle, four
  `lanes`, why they are mathematically different, and earlier-sprint references/differences.

Each lane needs `objective`, `mode`, `main_obligation_ids` containing the target,
`selected_new_perspective` (a string only for lane B), an `assignment_portfolio` with `fact`,
`route`, `memo`, `claim`, `obligation`, and `computation` ID lists, and a reason covering supplied
material and omissions. Carry the target only in `main_obligation_ids`; do not repeat it in the
portfolio. Lane C also needs a nonempty `computation_portfolio` list of experiment instructions.
The modes and access boundaries are fixed:

| Lane | Mode | Supplied material and purpose |
|---|---|---|
| A | `brainstorm` | Target statement and strictly necessary active facts; seek an independent proof, disproof, mechanism, intermediate obligation, or obstruction. |
| B | `multi-discipline` | Same target, necessary active facts, and one untried perspective; seek a noncosmetic representation shift. |
| C | `computation` | Target and an explicit small portfolio; explore examples, boundary cases, invariants, hidden hypotheses, and counterexamples with `CAS`. |
| D | `associate` | Target and two to four other distant facts/routes/memos/claims/obligations/computations; seek a bridge obligation or explain why none is natural. |

Use empty lists for unused portfolio types. Lanes A and B receive no routes, memos, claims,
computations, task histories, or category summaries. All four lanes are sealed from project
memory and from one another; do not include material merely because it exists.

Sprint agreement is not proof. Lane results still enter the ordinary progress, verification,
and publication workflows.
