# Live agent probes

`live_agent_probe.py` launches real `gpt-6-astra` sessions at the effort routed by Franta:
`max` for ordinary workers, proof-writers, discovery-sprint lanes, and the fresh sprint summarizer,
and `ultra` for the main agent, trimmer, and verifiers used here. It uses the production materializer, access
policies, memory broker, prompts, output schemas, skill staging, permission profile, Codex
transport, and—where the workflow itself is under test—the production runtime and scheduler. It
does not change the operator's Franta project.

Run all probes:

```sh
PYTHONPATH=src python3 evals/live_agent_probe.py
```

Run selected probes and retain their complete workspaces and audit trails at a chosen path:

```sh
PYTHONPATH=src python3 evals/live_agent_probe.py \
  --probe main --probe research \
  --output-dir /private/tmp/franta-live-main-research
```

The eight probe names are:

- `main`: gives the ordinary portfolio and task-summary context without a probe-only answer,
  checks the audited summary read before any full-record fetch, and requires one resumed assignment
  serialized by exactly one successful `task-writing` call;
- `research`: requires audited abstract search before the needed full-memory fetch and exactly
  one final `record-progress` call;
- `isolated`: gives a live brainstorm worker no memory broker or `internal-search` skill and
  still requires its final `record-progress` call. It also materializes and audits the full
  isolation matrix: ordinary brainstorm and multi-discipline workers, all four sealed
  discovery-sprint lanes (including computation and associate), and the fresh sprint
  summarizer's frozen-input-only workspace; and
- `trimmer`: presents an uncued category containing two materially distinct mechanisms, then
  checks whether the returned split-and-portfolio proposal commits through the real category
  store;
- `discovery-sprint`: presents repeated variants of one mechanism with an unchanged obstacle and
  checks a real trimmer's audited four-lane sprint staging;
- `human-guidance`: presents six already-separated credible directions after all blind sprint
  lanes returned to one bottleneck, then checks the advisory decision and a real constrained
  Tectonic compilation;
- `cas`: gives a computation worker a finite Sage task and checks the audited computation record,
  final `record-progress`, and their exact linkage; and
- `repair-stop`: creates a disposable Franta project, first publishes its cited supporting
  lemma through the scheduler's exact verification path, then seeds a deliberately flawed first
  version of a root fact and uses a real verifier to reject it. A real associate worker receives the
  exact revision supplement and must stage a higher version through final `record-progress`.
  Containment synthesis is accepted deterministically so the probe isolates the repair loop; a
  second real verifier must accept and publish the repaired fact. Finally, a real main agent must
  make the terminal decision, either declining further work or assigning exactly one
  proof-writer. The probe checks exact verifier envelopes and complete predecessor records,
  lineage and event order, receipts, model/effort audits, fact-only verifier access, and that no
  post-resolution research launches.

The script prints and saves `observations.json`. A probe passes only when all of its mechanical
checks pass. The JSON also retains the structured response, staged artifacts, skill receipts,
broker audit order, tool activity, and paths to the full transport logs. A nonzero exit status
means at least one probe failed or could not complete.

The discovery-sprint, human-guidance, and CAS probes exercise the live model, launch-bound broker
tools, and constrained local handlers at their component boundary. Deterministic runtime tests
separately cover trusted-receipt consumption, scheduler persistence, and recovery; a direct skill
probe is not by itself an end-to-end scheduler test.

The research observation classifies full reads as required, defensibly related, clearly
unrelated, repeated, or clearly redundant. Related reads do not fail the probe; a clearly
unrelated read does. Redundant-read evidence is reported for efficiency review and does not
create a new full-read prohibition.
