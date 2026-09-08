---
name: CAS
description: Run a configured local CAS reproducibly and stage its nonauthoritative computation record.
---

# CAS

Use this skill for a reproducible calculation with a CAS configured for this call. It is also
available to a verifier as a checking aid. Never supply or invoke an executable path.

Prepare the compact input described in [references/input.md](references/input.md), then call the
launch-bound `execute_cas` tool with those fields.

The audited handler selects the trusted executable, runs without a shell or network, and captures
the exact executable, input, arguments, software/version, output, errors, and exit status. Supply
the mathematical object, assumptions, environment details, seed when relevant, related canonical
IDs, and a short interpretation. A failed run may also be retained when useful.

The staged computation is nonauthoritative evidence. Any theorem inferred from it needs a
separate rigorous proof and the ordinary fact-verification pipeline.
