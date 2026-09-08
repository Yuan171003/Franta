# CAS input

Use these fields in one `execute_cas` call. `software` names a CAS configured for this launch;
never supply an executable path.

```json
{
  "software": "sage",
  "arguments": [],
  "version_arguments": ["--version"],
  "exact_input": "print(2 + 2)\n",
  "description": "The stated finite example",
  "assumptions": "List every mathematical assumption used",
  "environment_versions": {},
  "random_seed": null,
  "interpretation": "What the output establishes computationally",
  "related_ids": {
    "route": ["R-..."],
    "obligation": ["O-..."]
  },
  "fact_candidate_operation_ids": []
}
```

`related_ids` is an optional typed map. Its only keys are `fact`, `route`, `memo`, `claim`,
and `obligation`; each value is an array of canonical IDs. Omit unused groups. Do not use
variants such as `route_id`, `obligation_id`, `task_id`, or `candidate`. The task ID is supplied
by the runtime, and Fact candidate operation IDs belong in `fact_candidate_operation_ids`.

The runtime fills `software_version`, `exact_output`, `error_output`, and `exit_status`, then
stages the record. For file output, identify `output_artifact.path` inside `artifacts/`; the
runtime validates the file and fills its SHA-256.
