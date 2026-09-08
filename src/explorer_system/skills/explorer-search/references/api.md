# Explorer search API

## Search abstracts

Call `explorer_search` with:

```json
{
  "query": "degeneration of the incidence correspondence and boundary terms",
  "record_types": ["fact", "route", "memo", "claim", "obligation", "computation", "scratch", "summary"],
  "limit": 10,
  "include_inactive": false,
  "include_withdrawn": false
}
```

- `query` must be nonempty mathematical text.
- `record_types` must be a nonempty subset of `fact`, `route`, `memo`, `claim`, `obligation`,
  `computation`, `scratch`, and `summary`. The last two are provisional Explorer record spaces,
  not additional canonical memory types.
- `limit` is an integer from 1 through 10.
- `include_inactive` and `include_withdrawn` default to `false`; when authorized historical
  records are requested, their nonactive status remains visible and they are never premises.

The result contains ranked abstract-first records with their IDs, record spaces or types, status,
and provenance. Canonical records retain their canonical IDs. Scratch uses `ES-*`, summaries use
`ESUM-*`, and both have `record_space: "explorer"` and `status: "provisional"`. Only an active
canonical fact may be treated as established.

## Fetch one selected record

After reading the abstracts, fetch one selected result with:

```json
{
  "record_id": "ES-EXPLORER-1"
}
```

`explorer_fetch` accepts only an ID authorized by the current launch-bound capability and visible
inside its frozen read scope. It returns the full immutable content plus record status and
provenance. For a scratch that cites `cas_operation_ids`, the response also contains a
server-derived `cas_evidence` list with each trusted `XCAS-*` evidence ID, success flag, and
artifact hash. A main-sort call uses that evidence ID for an authenticated computation promotion;
it never supplies a computation body. A fetch never changes the record or its search status.

Under the staged access policy only attempt 3 receives `explorer_search` and `explorer_fetch`.
Attempt 1 receives `check-result`; attempt 2 receives `check-result` and `portfolio-search`.
These are mechanical capability boundaries rather than prompt-only requests. Recovery-only
legacy calls retain the tools fixed by their original v1 capability.
