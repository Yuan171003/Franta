---
name: human-guidance
description: Let the trimmer request advisory human direction through a successfully compiled project report.
---

# Human guidance

Trimmer only. Use when important promising directions exceed concurrent coverage, guidance is
needed on whether to continue a route, all sprint lanes return to one obstacle, or no credible
machine-selected escape remains.

Write a self-contained LaTeX report understandable to an algebraic-geometry PhD student who may
not know the project's terminology. Cover every active direction and, for each, its rigorous
description, current progress, obstacle, next obligations, and promising next steps. Summarize
the past five tasks and state the precise decision or new direction requested from the human.

Save the report as a `.tex` file inside `artifacts/`. Call the launch-bound `human_guidance` tool
with its relative `latex_path` and the precise `question` for the human; `request_id` is optional.
The audited handler uses configured Tectonic without a shell or network and stages only a
successfully compiled, validated PDF.

The scheduler persists the request before pausing new main-agent assignments; accepted work and
ingestion continue. Guidance is advisory, not mathematical authority or the sole basis for a
trimmer decision. On continuation, review results produced since the report snapshot before
completing the trim.
