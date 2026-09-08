# Portfolio-search API

## Search the frozen portfolio

Call `portfolio_search` with a query and optional result limit from 1 through 10:

```json
{
  "query": "semistable degeneration isolates the boundary contribution",
  "limit": 5
}
```

Results have only portfolio-local content fields:

```json
{
  "results": [
    {
      "portfolio_item_id": "EPI-OPAQUE-1",
      "abstract": "A degeneration route for the boundary term",
      "main_content": "Degenerate the incidence correspondence and compare the normalization sequence on each component.",
      "relevance": 6.25
    }
  ]
}
```

## Fetch one authorized item

Call `portfolio_fetch` only with an opaque ID returned by this launch's portfolio:

```json
{
  "portfolio_item_id": "EPI-OPAQUE-1"
}
```

The fetched item contains `portfolio_item_id`, `abstract`, and `main_content`. It never exposes a
canonical memory ID, fact/claim/route/memo type, relation field, dependency, provenance, BM25
score used for selection, or closest/random label.

## Frozen selection

The broker builds the portfolio from the first attempt summary query
`directions_tried + main_progress`:

- Routes: one closest by BM25 over the route description and two chosen randomly without
  replacement.
- High-level memos: three closest by BM25 over main content and three chosen randomly without
  replacement.
- Other Explorer attempts in the same turn: one closest by BM25 over its summary and one random
  attempt from a different worker lineage when available. The selected attempts contribute their
  summaries and authorized scratch.

Selection, snapshot cutoffs, and opaque membership are fixed for the launch. Searching never
adds a record to the portfolio.
