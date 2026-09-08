# Check-result API

Call `check_result` with exactly these fields:

```json
{
  "kind": "proved",
  "statement": "Every smooth projective curve of genus zero over an algebraically closed field is isomorphic to the projective line."
}
```

- `kind` is `proved`, `disproved`, or `computed`.
- `statement` is one complete, definite, strict mathematical proposition. It is not phrased as
  a question and must include the hypotheses needed to fix its meaning.

The response contains zero, one, two, or three closest matches in this uniform shape:

```json
{
  "results": [
    {
      "result_id": "CR-OPAQUE-1",
      "status": "established",
      "abstract": "Genus-zero curves over algebraically closed fields",
      "main_content": "A smooth projective genus-zero curve over an algebraically closed field has a rational point and is isomorphic to the projective line.",
      "relevance": 8.75
    }
  ]
}
```

`result_id` is opaque and cannot be used for a general memory fetch. Every item is marked
`established`; no returned field identifies whether its source was a fact, claim, or computation.
There is no next-page token or request for more results.
