# Scratch payload contract

Call `record_scratch` with one object as `payload`:

```json
{
  "operation_id": "OP-EXPLORER-SCRATCH-1",
  "record_kind": "idea",
  "abstract": "A degeneration of the incidence correspondence may isolate the missing codimension-one term.",
  "content": "Write the complete idea, derivation, example, obstruction, or tentative proof here, including hypotheses and unresolved gaps.",
  "related_memory_ids": [],
  "cas_operation_ids": []
}
```

`record_kind`, `abstract`, and `content` are required. The other fields are optional.

- `operation_id`, when supplied, is a nonempty caller-chosen staging ID. Omit it to let the
  runtime generate one. Never reuse an operation ID within a workspace; duplicate staging is
  rejected.
- `record_kind` is one of `idea`, `thought`, `intuition`, `claim`, `proof`, `route`,
  `obligation`, `computation`, `discovery`, `example`, `counterexample`, `obstacle`, `progress`,
  or `other`. It is a provisional search hint, not a publication decision.
- `abstract` is concise searchable prose naming the main objects, target, and central move or
  conclusion. It must not be a placeholder such as “an idea about the problem.”
- `content` is the self-contained main record. Preserve uncertainty and gaps rather than claiming
  verification that did not occur.
- `related_memory_ids` is a duplicate-free list of canonical fact, route, memo, claim, obligation,
  or computation IDs, or provisional Explorer scratch IDs (`ES-*`), actually used or discussed.
- `cas_operation_ids` is a duplicate-free list of staged CAS operation IDs supporting this
  scratch. The computation remains provisional.

There are no direction metadata fields and no dedicated `direction` kind. A useful direction or
pivot may be recorded as an ordinary scratch of the most fitting kind; the attempt-final summary's
`directions_tried` field is the authoritative account of directions explored during that attempt.
The caller must not supply a record ID, verification status, worker identity, attempt number,
turn, timestamp, or sequence. The service returns the allocated `ES-*` `record_id`. The stored
record remains `provisional` even when `record_kind` is `claim` or `proof`.

For a root proof or disproof, put the full argument in `content`, use `record_kind: "proof"`, and
wait for a successful trusted receipt before citing the returned `ES-*` ID as
`root_candidate_scratch_id` in the final response. Do not place a root-resolution signal in this
payload or in `record-summary`.
