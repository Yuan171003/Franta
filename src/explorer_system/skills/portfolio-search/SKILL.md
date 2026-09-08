---
name: portfolio-search
description: Search and fetch only the immutable, launch-bound research portfolio supplied to an Explorer second attempt.
---

# Portfolio search

Use this skill during an Explorer second attempt to search the launch-bound portfolio without
accessing full host or Explorer memory. Call `portfolio_search` with a focused mathematical query.
Use `portfolio_fetch` only for an opaque result selected from that same portfolio. See the exact
interfaces in [references/api.md](references/api.md).

The portfolio is frozen before launch. Its relevance query is the first attempt summary's
`directions_tried` joined with `main_progress`; it does not use new direction or retrieval fields.
The host selects a mix of close and random routes and high-level memos, plus material from up to
two other Explorer attempts. Candidate shortages simply produce a smaller portfolio.

Visible items contain only an opaque portfolio ID, abstract, and main content. In particular,
route and memo items omit canonical IDs, types, relation and dependency fields, provenance,
selection reasons, and scores used to build the portfolio. Treat all peer Explorer summaries and
scratch as provisional.

The broker enforces immutable membership and audits both operations. Do not guess item IDs, open
portfolio storage directly, or use another memory search or fetch. Use `check-result` separately
when progress depends on the known status of one strict mathematical proposition.
