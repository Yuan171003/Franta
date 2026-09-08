---
name: task-search
description: Read authorized Franta task summaries and fetch selected full task artifacts through the audited broker.
---

# Task search

Use this skill when an earlier task may help a main-agent or trimmer decision.

1. Call `task_summary` with the canonical task ID.
2. Read the returned summary and artifact descriptors first.
3. Call `task_artifact_fetch` only for an artifact whose full content is needed.

Do not fetch every artifact by default. Summary-first reading is guidance, not a prohibition on
opening a necessary full artifact. Every fetch is audited and limited by the launch-bound policy.

Never open the task archive or scheduler-private paths from the shell. This skill is unavailable
to workers, verifiers, synthesizers, sprint summarizers, and isolated lanes. Task artifacts are
intermediate evidence, not established mathematical premises or additional memory types.
