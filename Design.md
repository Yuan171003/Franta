# Clarified design for a long-running mathematical research agent

## 1. Design intent and interpretation

The system should place as few restrictions as possible on mathematical exploration while still being able to run coherently, safely, and for a long time. Mathematical choices that benefit from judgment, diversity, or randomness belong in agent prompts. Mechanical decisions that must be consistent—ID allocation, access boundaries, portfolio materialization, dependency tracking, validation, persistence, retries, and concurrency limits—belong to the Python scheduler.

Prompts should be concise and should primarily state an agent's goal, available capabilities, available evidence, and hard boundaries. They should not prescribe a detailed mathematical procedure unless the role is verification, where a stricter procedure is intentional. Within its task and access boundary, an agent may use any mathematical approach that is not expressly prohibited. The design of the prompt (except to the verifier) should follow the principles: short, concise, focusing on what they can do instead of what they should do

The words below have fixed meanings:

- **MUST** is mandatory. The scheduler enforces it when it is mechanically checkable; otherwise it is a required agent-output or role contract.
- **SHOULD** is guidance given to an agent. The agent may depart from it when it has a mathematical reason.
- **MAY** grants discretion.
- **Active fact** means a verified, published fact that has not been revoked.
- **Candidate** means agent output waiting for validation, verification, or publication. A candidate is not yet a memory.
- **Publish** means that the scheduler atomically adds a validated record to canonical memory.

Nothing in a route, memo, claim, obligation, task, category, portfolio, or computation is mathematically authoritative merely because it is stored. Among memory records, active facts are the sole source of established mathematical truth. Proofs may additionally use the versioned project foundations—axioms, definitions, and the root problem's hypotheses—which are not research conclusions.

## 2. Components and model configuration

The system has three components:

1. **Memory:** seven and only seven canonical memory types: fact, route, memo, claim, obligation, task, and computation.
2. **Scheduler:** a deterministic Python program that owns persistent state and enforces hard boundaries. It does not use an LLM and therefore consumes no model tokens.
3. **Agents:** main agent, trimmer, workers, and synthesizer. A verifier is a scheduler-invoked worker mode, not a fifth agent type. A discovery-sprint summarizer is a fresh worker session, not the synthesizer.

Model routing is role-specific:

- the main agent, trimmer, verifier, challenge-verifier, and closure reviewer use the persisted default model settings, initially `gpt-6-astra` with reasoning effort `ultra`;
- the synthesizer uses its persisted model settings, initially `gpt-6-astra` with reasoning effort `xhigh`;
- ordinary workers, proof-writers, all four discovery-sprint lanes, and the fresh discovery-sprint summarizer use `gpt-6-astra` with reasoning effort `max`; these worker routes do not inherit the manifest's default model settings.

The project begins from a non-memory bootstrap manifest containing:

- a self-contained root problem;
- self-contained statements for any additional initial target obligations;
- candidate material for any initial routes, memos, or claims, plus nonauthoritative suggestions for seed theorems that workers may later formalize;
- model settings;
- configured paths for Sage, Macaulay2, and any other allowed computer-algebra systems;
- scheduler limits and timeouts described in this specification;
- context or portfolio token budgets used for materialization and progressive disclosure;
- a project foundation policy

The bootstrap transaction in Section 9.1 allocates canonical IDs and creates the root obligation. A mathematical theorem suggested as seed material is not part of the foundation policy and is not published during bootstrap; after bootstrap it must be assigned to an ordinary worker, whose task ID supplies provenance, and then follow ordinary fact verification. Every published fact records the foundation-policy version used by its proof.

All durable reasoning state must be recoverable from files owned by the scheduler. No agent's conversational context is authoritative.

## 3. IDs, references, and write authority

Every canonical record and every category has a globally unique, immutable, type-prefixed ID. A valid scheme is `F-…`, `R-…`, `M-…`, `CL-…`, `O-…`, `T-…`, `C-…`, and `CAT-…`. The exact suffix format is an implementation choice, but the scheduler alone allocates IDs and never reuses them.

Whenever one stored object refers to another memory, it MUST use the target's ID rather than copying the target's full statement. A task card may additionally embed a statement when an isolated worker cannot access the referenced memory.

The scheduler is the sole writer of canonical memory, category files, portfolio snapshots, active indexes, and audit records. Agents and skills write only proposed or intermediate artifacts. The scheduler validates those artifacts and then applies accepted changes atomically. This extends the original rule that fact files can only be written through the scheduler and prevents concurrent agents from corrupting cross-links.

The scheduler MUST validate all referenced IDs and maintain consistent reciprocal indexes where records expose the same current relationship—for example, current route–fact use, route–memo attachment, route–claim attachment, and route–obligation attachment. Historical relationships are retained in audit or status overlays rather than being mistaken for current use. A missing, mistyped, inactive-when-required, or access-forbidden canonical ID causes rejection of the proposed operation; it is never silently ignored. Schema-declared relationship fields may instead use a task-owned typed temporary proposal ID under Section 8.3. The source memory may publish immediately. The unresolved value remains durable and visible in that field but is inactive: it creates no canonical reciprocal link, fact premise, or other semantic index until it resolves.

## 4. Canonical memory

### 4.1 Fact

A fact is a node in the fact dependency graph. Its immutable core contains:

- its unique ID;
- a self-contained mathematical statement;
- a complete proof, relative only to its listed predecessor facts, the project foundation policy, and any precisely identified external results checked by the verifier;
- the IDs of every predecessor fact used logically by the proof;
- the task ID whose output produced the accepted proof;
- every notation item introduced by the fact, with a precise mathematical definition;
- structured external references;
- an optional verifier-confirmed `root_resolution` naming `ROOT` with outcome `proved` or `disproved`.

A structured external reference contains source type, authors, title, stable identifier or URL, precise theorem/section/page locator, the exact result used, and its role in the proof. A bare link is not sufficient. A worker may use such an external result directly in a candidate proof, but the verifier must check both the cited result and its applicability before the containing fact can be published. When the result already exists in project memory, the proof uses its active fact as a predecessor instead. A frequently reused external result SHOULD be imported as its own verified fact so that later dependencies and revocation are explicit. The structured `predecessor_fact_ids` list is the sole machine authority for internal fact dependencies. The scheduler and verifier do not infer dependencies by scanning the statement or proof, do not require a predecessor ID to occur literally there, and do not rewrite either field when an ID resolves. The verifier instead checks the mathematics relative to the complete active fact records declared by that list. Each introduced-notation record contains the symbol or phrase, its precise definition, and its scope.

The fact also has mutable attached metadata containing:

- a concise abstract;
- searchable keywords;
- IDs of related routes.

For a published fact, the ID, statement, proof, predecessor IDs, originating task ID, foundation-policy version, introduced notation, external references, and `root_resolution` MUST never change. Only attached metadata may change, and only through the scheduler. “Published fact” and “accepted fact” mean the same thing in this specification.

The dependency graph MUST be acyclic. A candidate fact may depend only on active facts that already exist when the candidate is published. The scheduler rejects self-dependencies, cycles, inactive predecessors, or IDs declared in `predecessor_fact_ids` that are not available to the verifier. ID-like text elsewhere in the candidate does not create a dependency. An exact occurrence in the statement or proof of a temporary Fact proposal ID already known to the task is nevertheless a publication gate: it leaves the mathematical text unchanged and does not add a dependency edge, but the containing Fact cannot publish unless that proposal resolves. If it is terminally rejected, the containing unpublished Fact is rejected.

If a published fact is later found unreliable, the scheduler performs revocation:

1. Append an audit event containing the fact ID, reason, evidence or human instruction, time, and initiating actor.
2. Mark the fact inactive in the graph index without altering or deleting its immutable file.
3. Transitively mark every active fact that depends on it inactive, recording the causal root and dependency path for each affected fact.
4. Prevent every revoked fact from being supplied as an established premise.
5. Give obligations whose predecessor lists contain a revoked fact the derived status `unsupported`. They cannot be assigned with the revoked premise presented as truth, but they may be assigned specifically to reprove the missing premise, reformulate the obligation, or attack the self-contained statement without that premise. Remove the fact from every route's current-support list while retaining the old relationship and its effect in audit/status overlays; route, memo, and claim contents remain historical material.

A corrected statement or proof is submitted through the normal verification pipeline and receives a new fact ID. Revocation never silently rewrites or reactivates the old historical record. Facts that depended on the old ID must be re-established against active predecessors before returning to the active graph.

### 4.2 Route

A route is a concrete strategy for attacking the root problem or an important obligation. It is stored as a Markdown record containing:

- unique ID;
- current revision number;
- concise, mathematically clear abstract;
- mathematical description of the strategy;
- qualitative value assessment;
- progress obtained so far;
- IDs of related obligations;
- most promising next steps;
- main obstacles;
- IDs of active verified facts used by the route;
- IDs of relevant memos.
- IDs of relevant claims.

The scheduler maintains a derived history of task IDs that have tried each route. The main agent and trimmer judge whether a route has been “actively tried” from its complete task and progress history, not merely from recent recency or a fixed status flag. A rigorous refutation or completion is recorded in progress and obstacles with supporting fact IDs.

The value assessment consists of five short qualitative descriptions, not numerical scores:

- **confidence:** how plausible it is that the route can make significant progress;
- **success_gain:** how important the result would be if the route succeeds;
- **failure_gain:** how much useful knowledge a rigorous failure, obstruction, or counterexample would yield;
- **relevance:** whether the route addresses the central difficulty or only a peripheral case;
- **novelty:** whether the central mechanism is genuinely new relative to stored routes or is a refinement of a familiar mechanism.

Progress recorded in a route is a research summary, not a source of truth. Any statement presented there as established MUST point to an active fact ID; everything else must be clearly described as tentative, computational, heuristic, or obstructed.

The route ID and mathematical strategy description do not change under the defined route-update operation. Every accepted update increments the revision. The abstract, value, progress, obligation links, next steps, obstacles, fact links, memo links, and claim links may be refined through an explicit incremental update. A change that requires a different central mathematical mechanism or target is a new route rather than an update. The agent proposing the operation makes the initial decision whether the change is an update or a new route; when it proposes a new route, the synthesizer performs the duplicate/add/update review in Section 9.5.

### 4.3 Memo

A memo records an idea, thought, intuition, small discovery, example, counterexample, obstacle, possible route change, or other useful immature material. It is stored as Markdown and contains:

- unique ID;
- concise, searchable abstract;
- genre, exactly one of `high-level` or `normal`;
- content;
- zero or more related route IDs.

Memo content may be vague or incomplete and is never an established premise.

The memo's ID, abstract, genre, and content are immutable after publication. Its route attachments are scheduler-maintained metadata and may change as routes are created or reorganized. A substantive correction to memo content is stored as a new memo linked from the relevant route or task rather than overwriting history.

### 4.4 Obligation

An obligation is a rigorous, precise mathematical statement not currently established in the active fact graph, whose proof or disproof would have serious impact on the project. It contains:

- unique ID;
- current revision number;
- concise, searchable abstract;
- self-contained mathematical statement;
- qualitative importance;
- IDs of predecessor facts that were active when the obligation was published and on which its formulation relies; current activity is derived from the fact index;
- current partial progress;
- related route IDs;
- typed relations with other obligations.

An obligation relation stores a relation type, a list of premise memory IDs (obligations and, when needed, facts), a conclusion that is either an obligation ID or the reserved target `ROOT`, and a short explanation. Examples include `implies`, `derived_from`, and `jointly_implies`. A relation is only established when it cites supporting active fact IDs; without such support it is explicitly conjectural and cannot be used as a proved implication.

The obligation ID, statement, and predecessor fact IDs are immutable. The abstract, importance, partial progress, related routes, and relations may be updated incrementally, and every accepted update increments the revision. If the statement or its required premises change, create a new obligation and remove the old one from the active obligation set.

Removing an obligation means removing it from the active obligation index and from every related route, not erasing its history. If an obligation is proved, it must be removed and be replaced by a fact. The scheduler records a tombstone with the reason and any resolving or refuting fact IDs. A proved obligation normally yields a new fact candidate first and is removed only after that fact is published.

### 4.5 Task

A task is the durable record of one worker assignment. It contains:

- unique task ID;
- `assign_record`, which is the scheduler-accepted assignment report with the task ID attached;
- final status;
- concise final summary;
- a reference to its progress files, verification reports, and computation IDs produced under the task.

The four final statuses have exclusive meanings:

- `finished`: the task objective was achieved;
- `progress`: the objective was not achieved, but the task made mathematically significant progress;
- `failed`: the worker exited normally but neither completed the objective nor made significant progress;
- `interrupted`: after the configured retry policy, the task closed without a recoverable normal outcome because of infrastructure failure, loss of connection, token/process termination, or missing required attempt summary.

Runtime states belong to the scheduler rather than the task's four-valued final outcome. The durable lifecycle is `queued → launching → running → attempt_ended → postprocessing → closed`, with explicit branches through `revision_pending`, `retry_pending`, `stopping`, or `needs_attention`. Every transition is audited.

The assignment report, task card, progress files, verification reports, and worker summaries are intermediate artifacts rather than separate memories. The canonical task memory stores immutable references and hashes to them; the scheduler retains the raw artifacts in a separate task archive and does not embed them into the task record. Their contents do not automatically become facts, routes, memos, claims, or obligations; only the ingestion workflow in Section 9 can publish those records.

Once closed, a task record is append-only except for scheduler-added audit links. A restarted process uses a new attempt number under the same task ID if it is still pursuing the same task card. A materially different objective, mode, or portfolio receives a new task ID.

### 4.6 Computation

A computation stores a reproducible result produced by Sage, Macaulay2, or another configured computer-algebra system. It contains:

- unique computation ID and task ID;
- mathematical description of the computed object and assumptions;
- exact input, script, or command;
- software name and version;
- relevant environment/package versions;
- random seed when randomness is used;
- exact output or a stable reference to the raw output artifact;
- exit status and any error output;
- the worker's short interpretation;
- canonical IDs of related routes, memos, claims, obligations, and facts; fact activity is always derived from the current fact index/status overlay;
- provenance operation IDs for related fact candidates, whose later duplicate/publication/rejection resolutions live in the task audit rather than masquerading as memory IDs.

A computation is evidence for human checking and idea generation; it is not a fact and cannot be used as an established premise. A mathematical conclusion inferred from it must be submitted separately as a fact with a rigorous proof and pass verification.

### 4.7 Claim

A claim records a small claim proved by the worker, but not verified. It is stored as Markdown and contains:

- unique ID;
- concise, searchable abstract;
- content;
- zero or more related route IDs.

Claim content is never an established premise.

The claim's ID, abstract, and content are immutable after publication. Its route attachments are scheduler-maintained metadata and may change as routes are created or reorganized. A substantive correction to claim content is stored as a new claim linked from the relevant route or task rather than overwriting history.

The scheduler keeps an active-claim index. `claim_remove` atomically marks a claim withdrawn in a tombstone/status overlay, removes its current route links, and hides it from default search without deleting or editing it. Any replacement claim or fact must resolve before withdrawal and is recorded in the tombstone.

### 4.8 Abstracts

Every fact, route, memo, claim, and obligation abstract must identify the principal mathematical objects, hypotheses or target, and central method or conclusion in the minimum clear prose needed for search and triage. It should naturally contain discriminating mathematical keywords and must avoid placeholders such as “an idea about the problem.” Its purpose is to let another agent to have an understanding of the record and decide whether to open the full record without reading the full record.

## 5. Organizational and intermediate artifacts

Categories, portfolio snapshots, assignment reports, task cards, progress files, verification reports, summaries, and audit events are not additional memory types.

### 5.1 Categories

A category is a mutable organizational view over facts, routes, memos, claims, and obligations that belong to the same broad research direction, such as “use rational Hodge structures.” Tasks and computations may inform a category summary but are not primary category members. A memory may belong to multiple categories or none.

Each category is a Markdown file with:

- stable category ID and mutable name;
- revision number;
- concise description of the direction and its attached memories, focusing on routes and obligations and briefly mentioning important facts, claims, memos, and especially high-level memos;
- main progress;
- current obstacles;
- typed IDs of all member facts, routes, memos, claims, and obligations.

Categories carry no mathematical authority. A category's reference to a revoked fact remains visible as historical context but is marked inactive and is never supplied as verified support.

### 5.2 The two kinds of portfolio

The design uses two different objects:

1. A **category portfolio** is the set of category IDs and exact category revisions selected by the trimmer to guide the main agent. It remains active until a later trim replaces it.
2. An **assignment portfolio** is the explicit, typed set of fact, route, memo, claim, obligation, and computation IDs selected by the main agent for one worker task.

A portfolio never owns or deletes memory. “Delete a route from the portfolio” means omit that route from one selected view; it never means delete the canonical route.

The trimmer or main agent chooses portfolio content using mathematical judgment. The scheduler then validates every ID, records the exact snapshot, materializes the readable files, and enforces the relevant access policy. This separates semantic choice from mechanical enforcement.

For the main agent, the category portfolio is attentional guidance rather than an access boundary: the main agent may use internal search or direct indexed access to other memories when needed. For an ordinary search-enabled worker, the assignment portfolio is a starting set and internal search may find other memories. The scheduler may give the main agent, trimmer, and ordinary search-enabled workers a filesystem path that is enforced read-only and exposes committed canonical memory together with current status and index overlays. This path grants no write authority and excludes proposal staging, raw task archives, scheduler-private state, and other agents' workspaces. Its availability does not waive progressive disclosure, active-fact checks, or full-record-read auditing.

Brainstorm and multi-discipline workers receive no readable path to the canonical memory store. Discovery-sprint workers retain the stricter lane-specific access rules in Section 8.6: any lane whose contract denies direct project-memory access receives only its materialized sealed input and no canonical-store path. These boundaries are enforced at the filesystem and tool levels, not merely stated in prompts.

### 5.3 Progressive disclosure

Whenever an agent reads project memory, it SHOULD minimize tokens by using this order:

1. read category descriptions or task summaries;
2. read the abstracts of relevant memories;
3. read complete memory records or raw task artifacts only when needed for a mathematical or scheduling decision.

Whenever the main agent or trimmer reads task memories, it SHOULD minimize tokens by using this order:
1. Read the summary;
2. Read other memories  only when needed for a mathematical or scheduling decision.

The scheduler and search index must make all three layers available. Progressive disclosure is guidance, not a prohibition on reading a complete record when necessary.

### 5.4 Agent network access

The scheduler may expose an audited native web-search capability to agents, including an isolated worker when its task permits outside literature; this does not grant access to project memory. Agent-controlled shells, subprocesses, downloaded programs, and CAS commands do not receive unrestricted outbound network access. Model transport and scheduler-controlled infrastructure traffic are separate from network access initiated by an agent. Web material is nonauthoritative: any external result used by a fact must still be recorded as a structured external reference and checked by the verifier as required by Section 4.1.

## 6. Scheduler

The scheduler is the control plane. It MUST:

- start the main agent, trimmer, synthesizer, and workers with the role-routed models described in Section 2;
- persist and restore the root problem, root-resolution state, assignment gate, active category portfolio, category revisions, task and attempt states, trim state, human-guidance wait state, and pending post-processing;
- allocate IDs;
- validate and atomically write canonical records;
- maintain dependency edges, reverse links, indexes, and audit records;
- maintain reverse indexes from external references and foundation-policy versions to facts that use them;
- validate, snapshot, and deliver category and assignment portfolios;
- enforce worker-mode memory access;
- enforce concurrency limits;
- invoke the synthesizer for every new fact, route, or obligation proposal as defined in Section 9.5, invoke verifiers for fact candidates that the synthesizer does not resolve to existing memory, and invoke a fresh summarizer worker for `discovery-sprint` processing as defined in Section 9.2.1;
- distinguish infrastructure interruption from a mathematical rejection;
- make every ingestion step idempotent, so restart or duplicate delivery cannot publish the same operation twice.

The scheduler MUST NOT decide mathematical truth, route identity, obligation equivalence, category meaning, or research priority. Mathematical truth remains with the verifier. The synthesizer makes the semantic judgments defined in Section 9.5 about whether a proposed fact, route, or obligation is already represented in memory and whether a duplicate route or obligation contains new progress or perspective. Outside that review, route identity and obligation equivalence remain with the agent proposing the operation; category meaning remains with the trimmer; and research priority remains with the trimmer and main agent.

At most four non-verifier workers may run at once. Research, brainstorm, associate, multi-discipline, reformulate, computation, and proof-writer workers all count toward four. Verifiers do not count toward that limit, but a separately configured positive `max_parallel_verifiers` prevents unbounded verifier launch. If a proposed main-agent batch exceeds the free non-verifier slots, the scheduler launches none of that batch and returns it to the main agent for revision. Discovery-sprint batches follow the atomic four-slot rule in Sections 8.6 and 9.2.1.

Infrastructure retry limits are configuration values, not mathematical heuristics. At minimum, the manifest must set positive limits for worker interruption retries, verifier transport retries, synthesizer transport retries, and summarizer transport retries. Exhausting a transport retry limit creates an explicit `needs_attention` scheduler event; it never creates an `incorrect` mathematical verdict.

### 6.1 Full-process recovery

Before launching any agent call or applying any memory operation, the scheduler persists its call ID, input hash, attempt/lease epoch, retry counter, and event cursors so that the action can be repeated or fenced safely. This applies to main-agent, trimmer, worker, verifier, synthesizer, and summarizer calls. After the whole program stops and is restarted from the terminal, it MUST:

1. load the last committed state and audit journal;
2. finish or roll back any incomplete atomic write;
3. avoid reapplying progress operations and verifier/synthesizer/summarizer results already committed;
4. reconcile process liveness, fence every lost call's old lease epoch before relaunch, and reject late output from a superseded epoch while preserving progress already committed;
5. mark lost worker processes as interrupted attempts and retry eligible tasks under their existing task IDs and new attempt numbers, preserving retry counts across restart;
6. relaunch lost trimmer, synthesizer, summarizer, and verifier calls from their exact persisted inputs; when the gate permits an `open` or `resolution_pending` main-agent call, restart it with its last portfolio, new task summaries, and running-task manifest;
7. restore `reviewing_trim`, `trimming`, `waiting_for_human`, `resolution_pending`, or `completed` instead of incorrectly reopening assignment or planning while the gate is closed.

The scheduler provides a documented terminal entry point for both a fresh start and a resume. A restart never relies on reconstructing state from an old chat transcript.

## 7. Agent roles and worker modes

### 7.1 Main agent

The main agent attacks the provided obligations strategically and coordinates worker assignments. It first reads the current category descriptions, then selected memory abstracts, and reads full records only as needed. It also reads most recent task summaries first and inspects raw progress or assignment artifacts only when needed.

The current category portfolio guides the main agent, but the main agent may access other memories. Human guidance is advisory input. The main agent should consider human guidance seriously as an instruction, but should not view it as an overriding mathematical judgment.

Before proposing a batch of assignments, the main agent MUST inspect all running task cards and SHOULD balance the batch across objectives, modes, routes, perspectives, and portfolios. Different workers may receive different memory combinations, and the main agent may deliberately remove some active ideas from the portfolio to encourage a worker to generate independent ideas. It records the reason for each choice. 

The main agent follows these mode-selection rules:

- Use **research** when a concrete route deserves sustained attack. The main agent may include a primary obligation related to route. Include the related facts, memos, claims, obligations, computations, plus other related routes it finds helpful. It may deliberately include different routes or routes that are not actively tried to increase creativity.
- Use **brainstorm** for exactly one obligation on which the project is stuck and independent, minimally primed thought is valuable. The only things in the portfolio are that obligation and the facts that are necessary for attacking it. It SHOULD contain no routes.
- Use **associate** to seek progress from a deliberately varied combination of memories, including underused memories. The portfolio could include deliberately flexible selection of routes, and related facts, memo, claims, obligations, and computations
- Use **multi-discipline** to attack one or more obligations from a genuinely different mathematical perspective that has not been actively tried. The only things in the portfolio are those obligations, the facts that are necessary for attacking them, and a suggested initial perspective for attacking them. It SHOULD contain no routes. The perspective may be broad or vague, but the main agent must have a concrete reason to regard it as genuinely different from actively tried approaches and potentially illuminating. Examples include viewing a moduli problem through Shimura varieties or giving a topological invariant a categorical interpretation.
- Use **reformulate** to reinterpret selected memories in other mathematical languages and seek concepts or deductions from the reinterpretation. The portfolio could include deliberately flexible selection of routes, and related facts, memo, claims, obligations, and computations.
- Use **computation** to investigate examples and derive possible counterexamples, phenomena, invariants, or conjectures. It receives a portfolio containing routes and obligations, and some related facts, memos, claims, and computations.
- Use **proof-writer** only when the root problem could be proved or disproved by a fact or the combination of several facts.

After a verified root resolution, the main agent stops research assignments and may issue at most one proof-writer assignment before acknowledging completion.

Portfolio assignment should be focused on routes or obligations, and only include necessary facts, memos, claims, and computations. When selecting memos, it should pay attention to high-level ones since they have the potential to provide inspiration. The size of the portfolio should not be too large in order to save tokens.

The main agent MUST detect when workers are producing only small variations in one direction without a significant breakthrough. Significant progress should push the agent significantly closer to the root problem, or solve a significant obstacle. Producing verified facts and solving obligations are not significant progress by themselves. Rephrasing or case-by-case discussion that do not alter an obstacle count as small progress. This remains a judgment by the main agent and trimmer, not a fixed scheduler score. In this case, the main agent should reassign other routes or modes to the workers to avoid stagnation.

If the workers have been assigned several different directions of routes, obligations and modes, but still make few progress, or are still keep producing improvements on few directions without significant progress, the main agent should  enter a ’stuck’ mode. In this mode, it should write a brief summary on the current progress and stucking points, and the scheduler would send it to the trimmer.

When a worker finishes, the main agent reads its summary and the canonical changes that resulted. It may immediately propose another task if the next objective is clear, or leave the slot idle until other results arrive. If a worker has made genuinely important progress (such as a real breakthrough or innovative idea; small progress and facts are not enough) and the main agent wants it to continue with a same or similar task, the main agent may put the finished task's ID in the new assignment's `if_resume` field. Otherwise, `if_resume` is `null` and a fresh worker session is the default. If a worker is interrupted, the scheduler first retries the same task. Once the configured retry limit is exhausted, the main agent may reschedule the goal, mode, or worker in a new task.

If necessary, the main agent may reason mathematically while planning, but it has no direct canonical-memory write path. If it develops a potentially important mathematical result, it assigns a worker to formulate and record it through the ordinary progress and verification pipeline.

The main agent decides the entire next assignment batch before invoking `task-writing`. It then invokes `task-writing` exactly once for each assignment in that finalized batch. It must not invoke the skill before it finishing an assignment.

Whether to resume previous session: the main agent should have a fresh restart if the category portfolio changes; otherwise, it should resume the previous session.

### 7.2 Worker freedom common to all modes

A worker's mode is its current suggested way of approaching its task, not a permanent identity and not a rigid mathematical procedure. Except for hard access and truth boundaries, a worker may try another method when useful.

All non-verifier workers:

- work on an assigned task in service of the root problem;
- may treat active facts and the project foundation policy as established internal premises, and may propose the use of a precisely cited external result subject to verifier checking;
- may use routes, memos, claims, obligations, tasks, and computations as ideas or targets, never as proved claims;
- are explicitly encouraged to refute a route or rigorously identify a serious obstacle when that is the true outcome;
- may freely continue attacking the root problem after completing the assigned objective, under the same mode and access policy, and record that work under the same task;
- use `record-progress` after significant progress and once with `final=true` before each attempt ends normally;
- receive the configured Sage and Macaulay2 capabilities through `CAS`.

Finishing the assigned objective does not lift a sandbox. A brainstorm, multi-discipline, or discovery-sprint computation worker needs a new assignment to gain broader memory access.

### 7.3 Research mode

Research mode attacks the assigned task primarily through one specified route. It requires a main route ID and a concrete task objective; it may also specify a main obligation. The worker may pursue intermediate claims when they serve the task and root problem. It may use `internal-search`.

### 7.4 Verifier mode

Verifier is a worker mode that only the scheduler may invoke. The main agent and other workers cannot launch it directly; submitting a fact candidate is the request for verification.

The verifier receives the exact candidate statement and proof, the project foundation policy, and any prior verification report for a revision. It checks the exact submission, not the plausibility of the intended idea. Its procedure must check at least:

- whether exactly the same fact is already proved in the fact graph (if it is the case, there’s no need to verify and add the fact)
- statement/proof correspondence and quantifiers;
- every logical step and edge case;
- hidden assumptions and circular reasoning;
- correctness and active status of dependencies;
- whether `predecessor_fact_ids` declares a complete active predecessor basis and the proof is valid relative to those records, without requiring literal ID tokens in the statement or proof;
- accurate use of external references and project foundations.
- whether a declared `root_resolution` really proves or disproves `ROOT`.

It performs a constructive pass that reconstructs the proof and an adversarial pass that actively searches for a counterexample, missing case, unjustified equivalence, quantifier reversal, circular dependency, or reliance on the desired conclusion. It does not repair a proof charitably and then approve the repaired version; only the submitted version is judged.

Its report contains `verdict: correct` or `verdict: incorrect`, the candidate ID and version, submission operation ID, and verifier-attempt ID. For either verdict it echoes the submitted predecessor list exactly; a correct verdict confirms that the declared basis is complete and sufficient, together with every notation item introduced with precise mathematical definition, the structured external references, and any `root_resolution`. If the verdict is incorrect, it includes detailed errors with locations. Any nontrivial gap, unsupported premise, unresolved external citation, or unproved root resolution yields `incorrect`. A crashed or disconnected verifier yields no report and is retried as an infrastructure failure.

The verifier never edits a proof or canonical memory. It may explain how a gap could be repaired, but the originating worker performs the revision.

The verifier may use the configured `CAS` tools as a checking aid, but its only required output is the verification report; a computation cannot replace a proof check.

### 7.5 Brainstorm mode

Brainstorm mode receives exactly one self-contained obligation statement and may receive selected active facts that the main agent considers necessary. It receives no routes, memos, claims, or other obligations. The scheduler materializes the statement and allowed fact records into a sealed workspace.

The worker has no `internal-search` capability and no direct access to the project memory store. It develops independent ideas to prove or disprove the obligation. This stricter definition resolves the original conflict between the main-agent and worker descriptions in favor of the expressly requested strong sandbox.

### 7.6 Associate mode

Associate mode receives a deliberately flexible selection of routes, facts, memos, claims, and optionally obligations. The worker seeks new ideas or progress by combining these material. It may use `internal-search`.

### 7.7 Multi-discipline mode

Multi-discipline mode receives one or more self-contained target obligations, a selected assignment portfolio, and an initial perspective chosen by the main agent.
The scheduler exposes only the explicitly materialized task card and portfolio. `internal-search` and direct project-memory access are unavailable. The worker should attacks the targets from a genuinely different angle; it may use the supplied angle, or may refine, replace, or combine it with another perspective. This preserves agent freedom without weakening the isolation that produces independent ideas.

### 7.8 Reformulate mode

Reformulate mode receives selected routes, facts, memos, claims, and optionally obligations. It actively translates and reinterprets them in mathematical languages from other areas, with the aim of producing concepts, deductions, or innovative routes rather than a merely cosmetic restatement. It may use `internal-search`.

### 7.9 Computation mode

Computation mode receives a collection of routes and obligations, and related facts, memos, claims, and computations. Outside a discovery sprint, it may use `internal-search` and freely call `CAS`. A discovery-sprint computation worker instead receives the special prompt and sealed environment specified in Section 8.6. Its goal is to do computation to explore relevant examples, and derive counterexamples, new phenomena, possible invariants, or conjectures. Every completed computation is recorded reproducibly, and any claimed theorem still requires a separate rigorous proof and verification.

### 7.10 Proof-writer mode

Proof-writer is an expository worker mode. It’s goal is to write a publishable mathematical paper explaining the proof or disproof of the root problem, using the facts in the memory.

### 7.11 Synthesizer

The synthesizer is invoked only by the scheduler and cannot be assigned by the main agent. It reviews every proposed new fact, route, or obligation to decide whether the same mathematical content is already represented in canonical memory. For a proposed route or obligation that is already represented, it also decides whether the proposal contributes genuinely new progress or perspective and, when it does, formulates a valid `route_update` or `obligation_update` patch instead of adding a duplicate record. It reads search results and abstracts first and opens full records only as needed. It never verifies a new proof, decides mathematical truth, or writes canonical memory; it returns the structured review described in Section 9.5 for scheduler validation and application.

## 8. Skill and artifact contracts

The names below describe required capabilities. Their internal implementation may vary, but their inputs, outputs, and access behavior must satisfy these contracts.

### 8.1 `task-writing`

After the main agent has finalized an entire assignment batch, it calls `task-writing` once per assignment. For a discovery sprint, the scheduler calls `task-writing` once for each of the trimmer's four finalized lane blueprints under one sprint batch ID. task-writing serializes a finalized assignment decision. It MUST NOT revise the mathematical objective, mode, selected memories, continuation decision, target, route, target obligation, or perspective.
The skill receives the main-agent assignment decision or sprint lane blueprint and emits an `assign_report` containing:

- scheduler-issued batch ID and unique assignment-report ID;
- task objective;
- `if_resume`: the previous task ID whose worker session should be resumed, or `null` for the default fresh start;
- work mode;
- main route ID(s), or `null` when the mode has none;
- main obligation ID(s), or `null` when the mode has none;
- selected new perspective, or `null` when the mode has none;
- assignment portfolio grouped into fact, route, memo, claim, obligation, and computation IDs;
- a brief reason for the assignment, including the reason for deliberate omissions or unusual memory combinations.

Mode validation is mechanical: research requires a main route, maybe a main obligation; brainstorm requires exactly one main obligation and permits only optional facts; multi-discipline requires at least one main obligation and a nonempty seed perspective; computation requires routes or obligations; proof-writer requires the active `root_solution_fact_id`. Associate and reformulate need no distinguished route or obligation unless the main agent chooses one. There should be a hard scheduler limit (my experimental limit is 20) on the maximal number of memories in a portfolio.

The scheduler validates the report, allocates the task ID, attaches that ID to the accepted report to form the task's `assign_record`, and creates one immutable task card containing:

- task ID;
- `if_resume`: the validated previous task ID, or `null`;
- root problem;
- work mode;
- self-contained task content/objective;
- optional main route ID;
- optional main obligation ID(s) and, for an isolated mode, the corresponding embedded statement(s);
- optional selected perspective;
- typed portfolio IDs;
- the exact memory snapshot or revisions supplied;
- mode-specific prompt;
- enforced access policy.

Task-ID allocation, slot reservation, accepted-report storage, and launch intent are committed together under the batch/report idempotency keys. The scheduler then sends the task card and portfolio for the worker. Task cards are not edited after launch. Changing the objective, mode, or portfolio creates a new task; an infrastructure or revision attempt reuses the original card and receives a separate immutable attempt supplement explaining the retry or verifier feedback. When `if_resume` contains a task ID, the scheduler resumes the worker session associated with that task and gives it the new task card. The scheduler tracks each worker-session lineage and permits at most six resume launches in that lineage. If the lineage has already been resumed six times, the scheduler rejects another resume request with `fresh_start_required` and requires the new task card to use `if_resume: null`; it never launches a seventh resume. A fresh launch starts a new lineage and resets this count.

Every 8 main-agent assign_records the scheduler receives, it should send them together to the trimmer.

### 8.2 `internal-search`

The caller supplies:

- a mathematical description of what it seeks;
- a nonempty subset of `fact`, `route`, `memo`, `claim`, `obligation`, and `computation`, or all six;
- optionally a result limit (my experimental hard limit is no more than 10) and whether inactive facts or withdrawn claims should be shown.

The skill filters results by the caller's access policy before ranking. It uses BM25 as the primary ranking method and may add candidates found through semantic or synonym expansion so that different terminology does not hide a relevant memory. Each result contains ID, memory type, a noncanonical display title generated from the statement or abstract, the abstract, active/inactive status when applicable, and a relevance score or rank. For a computation, the search index supplies a concise derived summary of its mathematical object, assumptions, software, outcome, and worker interpretation; this summary is an index artifact rather than a new memory type or a change to the computation record. An agent should reads these summaries first, and opens a complete record only as needed.

Facts are active-only and withdrawn claims are hidden by default. Historical results, when requested, are visibly marked and never returned as valid premises. A worker's full-memory fetch is logged in its task archive; a main-agent, trimmer, or synthesizer fetch is logged in that agent run's audit record.

`internal-search` is unavailable at the tool and filesystem level in brainstorm and multi-discipline mode and for discovery-sprint computation workers. This is enforced by the scheduler and not left to prompt compliance.

### 8.2.1 `task-search`

`task-search` is a read-only, audited progressive-disclosure skill available only to the main agent and trimmer. It is separate from canonical-memory search. The caller first requests an authorized task summary and artifact descriptors, then may fetch selected complete task artifacts only when the summary is insufficient for a mathematical, scheduling, or trimming decision. Summary-first reading is guidance, not a hard fetch-order gate, and there is no requirement to fetch every artifact.

The scheduler filters every request by the launch-bound role and project policy and records every complete-artifact fetch in the caller's audit. The skill never exposes a direct task-archive filesystem path, proposal staging, scheduler-private state, or another live agent's workspace. It is unavailable to workers, verifiers, synthesizers, discovery-sprint summarizers, and isolated lanes. Its use is not an exit condition; the only skill-timing rules mechanically enforced by the scheduler remain `task-writing` and the attempt-final `record-progress` call.

### 8.3 `record-progress`

Each call emits a new immutable progress file containing:

- globally unique progress ID;
- task ID and attempt number;
- strictly increasing per-attempt sequence number;
- `is_final`;
- current outcome status;
- a concise account of progress since the previous call;
- zero or more memory-operation proposals;
- related computation IDs;
- zero or more `fact_challenge` control reports, each with a unique challenge ID, fact ID, precise alleged proof failure, and optional evidence.

There remain seven memory kinds. The following are eight **memory-operation proposal kinds**, not eight memory kinds:

1. `fact`
2. `route_update`
3. `route_add`
4. `memo`
5. `claim_add`
6. `claim_remove`
7. `obligation_add`
8. `obligation_update`
9. `obligation_remove`

Every submission has a unique operation ID so replay is idempotent. Every worker-submitted `fact` operation also uses a fresh candidate ID, a fresh proposal ID, and `candidate_version: 1`, including a correction or verifier-requested repair. Scheduler-owned normalization may create immutable internal successor versions for that exact proposal; it never authorizes a worker to reuse either ID. A `fact` operation contains the precise statement, complete proof, authoritative `predecessor_fact_ids`, and proposed attached metadata required by Section 4.1, and declares `root_resolution` when it claims to prove or disprove `ROOT`. If the proof uses another verified fact, that fact must appear in `predecessor_fact_ids`. If the proof uses a claim, it must reproduce and verify the needed argument instead of treating the claim as a proved premise. The proof may contain newly proved intermediate lemmas, but it may not merely assume them. A dependency on another uncommitted fact proposal initially uses that proposal's temporary ID in `predecessor_fact_ids`; occurrences or omissions of that ID in the statement or proof do not alter the dependency graph.

Remember that a proved claim SHOULD be written as a claim by default. A proved claim should be recorded as a fact if it makes significant progress on a route, provides genuinely valuable perspective, proves or disproves an important intermediate question, or would be used for several times in later proof.

The scheduler looks only at `predecessor_fact_ids` when determining fact dependencies. Only after each listed predecessor publishes or deduplicates to an active canonical fact does the scheduler replace the temporary ID in that list, create a new immutable normalized candidate version, and send that version to synthesis and verification. The statement and proof are copied unchanged. Known temporary Fact IDs occurring there are separate publication gates as described in Section 4.1. Terminal rejection of a temporary Fact proposal recursively rejects every still-unpublished Fact gated by or dependent on it, and then every Fact gated by or dependent on those rejected proposals. A later repair uses fresh IDs and does not revive the rejected proposal or its dependent chain.

More generally, Fact metadata and non-Fact memory may refer to a not-yet-committed Fact, route, memo, claim, or obligation proposal in a schema-declared relationship field whose type admits that target. The proposal need not appear in the same progress file. The scheduler validates and publishes the source memory while recording the unresolved relationship in a durable pending-reference overlay. Its raw temporary ID remains visible in the original field, but it is inactive and is not an established canonical link or mathematical premise. Temporary IDs remain forbidden in untyped non-Fact prose, abstracts, immutable content, and control-identity fields.

When the target later publishes, deduplicates, or merges through an update, the scheduler atomically substitutes its formal canonical ID in every waiting relationship, activates the applicable reciprocal or typed indexes, increments the source revision or metadata version as appropriate, and records immutable mappings. If that exact temporary proposal is terminally rejected or is still unpublished when its task finishes, the durable field value instead becomes `TMP-ID(unpublished)`. The annotation remains data in the original list, scalar endpoint, or nested relation position, but never enters a canonical dependency or reciprocal index. Soft references do not reopen, block, or put the source task into attention. Legitimate soft-reference cycles are therefore harmless; only the mathematical Fact dependency graph must be acyclic.

For every published or deduplicated add proposal, the scheduler records an immutable proposal-to-canonical-ID mapping in the task audit. Rejected proposals record no canonical target.

A `route_add` supplies the route schema from Section 4.2 except for the canonical ID and scheduler-managed revision, using its proposal ID until commit. A `memo` similarly supplies the memo schema from Section 4.3, including related route IDs; a `claim_add` similarly supplies the claim schema from Section 4.7, including related route IDs; and an `obligation_add` supplies the obligation schema except for canonical ID and revision. Their canonical IDs are scheduler-assigned and their revisions initialized by the scheduler only when committed.

An obligation SHOULD be an unverified claim whose proof or disproof would have serious impact on the route or root problem. An ordinary conjecture should not be proposed as an obligation

Before it starts a route_add or obligation_add, a worker that has `internal-search` should call it to find whether the proposed route or obligation already exists. If it is the case, read the full route or obligation memory. If the worker produces essentially new progress or perspective, use route_update or obligation_update instead to update the memory; if it does not produce essentially new progress or perspective, do not add it. A worker whose mode forbids `internal-search` remains isolated and may submit the proposal without this preliminary search; the scheduler does not weaken its access boundary. Regardless of whether the worker could search, every proposed new fact, route, or obligation goes through the synthesizer review in Section 9.5.

`route_update`, `obligation_update`use a machine-readable incremental patch with:

- operation ID;
- target ID;
- expected base revision;
- `set` for whitelisted scalar fields;
- `append` for new narrative entries;
- `add_ids` and `remove_ids` for typed relationship lists;
- an explanation and supporting memory IDs.

An absent field means “leave unchanged”, never “clear.” For a route, whitelisted changes cover the abstract, value descriptions, progress, obligations, next steps, obstacles, fact IDs, memo IDs, and claim IDs; they cannot alter the strategy description. For an obligation, they cover the abstract, importance, partial progress, related routes, and typed relations; they cannot alter the statement or predecessor facts. The scheduler applies a patch only against its declared base revision. It may rebase nonconflicting append/add operations, but any conflicting replacement returns to the agent proposing the operation for semantic resolution rather than overwriting newer work.

When a `route_update` or `obligation_update` changes an abstract, the agent SHOULD keep the abstract concise and control its total length.

`obligation_remove` contains the obligation ID, and optional resolving/refuting fact IDs or replacement obligation ID. It performs archival removal from the active set as defined in Section 4.4.

`claim_remove` contains the claim ID and optional replacement claim or fact ID. The scheduler resolves any replacement first, then applies the withdrawal defined in Section 4.7.

An intermediate call normally reports `progress`. `is_final=true` means final for the current attempt. At most one such progress record is allowed per attempt, and a normally ending attempt must submit exactly one. On an `is_final=true` call, the worker proposes `finished`, `progress`, or `failed` using Section 4.5 and lists the operation IDs required as evidence for completion. `interrupted` is normally assigned by the scheduler. The scheduler cannot make a mathematical significance judgment: it enforces infrastructure and verifier outcomes, and requests a main-agent closure review when the worker's proposed outcome is inconsistent or ambiguous.

With `is_final=true`, the skill also creates an attempt summary containing the work mode, task, proposed outcome, cumulative important progress for the whole task, completion-evidence operation IDs, and most promising next steps. If an attempt ends without a valid attempt summary, the scheduler treats that attempt as interrupted. Valid progress files already committed from an interrupted attempt are still processed and preserved. When the task closes, the scheduler designates the closing attempt's cumulative summary, together with mechanical downstream results, as the task's final summary.

### 8.4 `CAS`

`CAS` exposes the configured Sage, Macaulay2, and optional additional local mathematical software. It captures the exact executable, version, input, output, errors, environment, artifacts, and seed needed for reproduction. At the end of each coherent computation, it stages the record specified in Section 4.6 except for the canonical computation ID; the scheduler assigns that ID and publishes the computation memory. Failed computations may also be stored when the failure is mathematically or diagnostically useful.

### 8.5 `human-guidance`

The trimmer may call `human-guidance` when it judges that several promising directions exceed the current workers' ability to cover them adequately, or when it needs human guidance on whether to continue a route.

The skill writes and successfully compiles a LaTeX PDF describing every currently active direction. For each main research direction, write down its rigorous math description, current progress, current obstacle, next obligations and most promising next steps. Also includes what the agents have down in the past 5 tasks, and includes which question it wants human to decide. The report must be understandable to a PhD student in algebraic geometry who may not know the project's terminology or techniques.

The report and a request ID are persisted. The scheduler then enters `waiting_for_human`: it blocks new main-agent assignments but lets already accepted workers, verification, and ingestion continue. It waits indefinitely unless the human explicitly cancels the request. When guidance arrives, the trimmer first reads results produced since the report snapshot, then finishes its work using the human response as advisory input. If the request is cancelled, the scheduler returns to `trimming` and the trimmer selects or retains a portfolio without human guidance. The trimmer should consider human guidance as an important advisory input, but mot as the sole factor in its decision. In either case, the scheduler commits the result, stores any guidance received, and only then reopens assignment.


### 8.6 `discovery-sprint` 

This is a trimmer-only skill. It is used when recent tasks and trims keep producing small variations of the same central mechanism without materially changing the main obstacle. It creates one exploration sprint around exactly one active target obligation.

The decision that the project is stuck remains a semantic judgment by the trimmer. The scheduler MUST NOT replace it with a fixed numerical score. The trimmer SHOULD NOT launch a sprint merely because one task failed or because a relevant running task has not yet reported.

## Plan

The skill reads the current `stuck` report when one exists, category summaries, recent task summaries, and only the memory abstracts or full records needed by progressive disclosure. It returns either `no_sprint` with a concise reason, or a sprint plan containing:

- the target obligation ID, revision, and exact self-contained statement;
- a concise account of the repeated mechanism and unchanged obstacle;
- four worker-assignment blueprints described below;
- the exact facts and other materials supplied to each lane;
- a brief reason why the four lanes are mathematically different;
- references to related earlier sprints and how this plan differs from them.

The trimmer chooses the mathematical target, perspective, portfolios, and deliberate omissions. The scheduler only validates IDs, revisions, active-fact status, access policy, concurrency, and persistence.

## Four isolated lanes

### Lane A: clean-room brainstorm

Use `brainstorm`. Give the worker exactly one target obligation and only the active facts strictly necessary to attack it. Give it no routes, memos, claims, computations, other obligations, task histories, category summaries, or `internal-search`. Its goal is to independently find a proof, disproof, new central mechanism, precise intermediate obligation, or serious obstruction.

### Lane B: representation shift

Use `multi-discipline`. Give the worker the same target obligation, only necessary active facts, and one perspective not actively tried by the project. Give it no routes, memos, claims, computations, task histories, category summaries, or `internal-search`. Its goal is to translate the target into a genuinely different mathematical language and seek deductions that are not merely cosmetic reformulations.

### Lane C: counterexample and boundary laboratory

Use `computation` with `CAS`. Give the worker the target obligation and a small explicit portfolio selected by the trimmer. Give it a sprint-specific computation prompt and the same sealed tool and filesystem environment used for brainstorm mode: `internal-search` is unavailable and there is no direct project-memory access. It investigates examples, low-dimensional or degenerate cases, extreme parameters, finite or random instances, and possible counterexamples. Its goal is to expose hidden hypotheses, new invariants, corrected statements, or a rigorous computationally motivated obstruction. Computations remain nonauthoritative evidence.

### Lane D: remote composition

Use `associate`. Give the worker the target obligation and a small bundle of two to four deliberately distant but potentially composable facts, routes, memos, claims, obligations, or computations. It has no `internal-search` or direct project-memory access. Its goal is to find a bridge obligation `B` such that the supplied materials together with `B` could imply the target, or to explain precisely why no natural bridge appears available.

## Launch and isolation

A sprint uses all four non-verifier worker slots. The scheduler waits until all four slots are free, reserves them atomically, and launches all four task cards together. If all four slots cannot be reserved, it launches none of the sprint lanes.

Each lane receives a separate sealed workspace. Before the completion barrier, no lane may read another lane's task card, progress, computation, output, or proposed memory operation. Finishing early does not enlarge its access. Every lane still uses ordinary `record-progress`, task recovery, computation recording, fact verification, and progress-ingestion rules.

The barrier opens only after all four lane tasks have closed under Section 9.6, including terminal dispositions for every operation, computation, and challenge submitted by the lanes. Missing or interrupted lanes are recorded explicitly rather than silently replaced.

## Blind synthesis

After the barrier, the scheduler invokes another independent fresh worker `summarizer` (do not use trimmer or main agent since they might have initial bias) once using the frozen sprint plan, the four final or interruption summaries, relevant computations, and canonical changes produced by the lanes. It SHOULD omit current route rankings and category-selection rationales so that the comparison is not biased toward the existing agenda.

The summarize report contains:

- one concise mechanism fingerprint for each lane: principal objects, representation, central move, required bridge, main obstacle, and evidence status;
- which lanes are genuinely different and which are renamed variants;
- shared bottlenecks and contradictions;
- possible bridges between lanes;
- the most informative negative results;
- concrete follow-up tasks and suitable worker modes;
- a clear distinction between verified facts, unverified claims, computations, and conjectural ideas.

Agreement between lanes is evidence for research priority, not a proof.

## Integration

The trimmer reads the synthesis report and the canonical changes, then performs its ordinary maintain and select modes. It SHOULD prefer mechanisms that genuinely change the central obstacle, preserve useful negative results, and avoid flooding the next portfolio with every sprint artifact.

The trimmer may recommend follow-up assignments, retain the current portfolio, choose a revised category portfolio, or call `human-guidance`. It SHOULD call `human-guidance` when several important new directions cannot be covered concurrently, when all four lanes return to the same unresolved obstacle, or when no credible machine-selected escape direction remains.

Sprint plans, lane comparisons, mechanism fingerprints, and synthesis reports are control or audit artifacts, not new memory types. The trimmer and summarizer do not write canonical memory directly. Active facts and the frozen project foundations remain the only established premises, and every mathematical result enters memory through the ordinary scheduler-controlled pipeline.

### 8.7 Fact challenges

A `fact_challenge` is a control artifact, not a memory type or memory operation. A worker may include one in a progress file; the main agent, trimmer, or human may submit the same schema directly.

The scheduler invokes verifier mode, under verifier concurrency and retry limits, with the exact immutable fact core and challenge. It returns `confirmed_invalid`, `challenge_rejected`, or `inconclusive`, with justification. `confirmed_invalid` requires a concrete fatal flaw or failed cited premise and starts Section 9.7 revocation; `challenge_rejected` closes the challenge; `inconclusive` creates `needs_attention` without changing fact status. Transport failure has no mathematical effect, and agents cannot revoke facts.

### 8.8 Baseline worker prompt

The generic prompt for every non-verifier worker is:

> You are a long-running mathematical worker in a research system. Your current work mode is `{MODE}`. The root problem is `{ROOT_PROBLEM}`.
>
> Your assignment is in `{TASK_CARD}`. You should follow the prompt in your assignment. You have access to the materialized portfolio listed there. For each memory in the portfolio, read summaries and abstracts first, then full records when useful. Active facts and declared project foundations are established internal premises; routes, memos, claims, obligations, computations, and task records are exploratory material and cannot be regarded as verified facts. You may propose using a precisely cited external result.
>
> The mode is a suggested way to solve the task, and you may use another mathematical approach when useful. If your access policy permits it, use `internal-search` to find established facts, routes, memos, claims, obligations, or computations that has discovered by the agent. For each searched memory, you should read its abstract first, and only read the full memory if you need it.
>
Refuting a route or rigorously proving that it faces a serious obstacle is also valuable progress. If the task is complete, you may freely continue attacking the root problem under the same access policy.
>
> Use `record-progress` whenever you prove a claim or make significant progress, and use it once with `final=true` before the current attempt ends normally so that useful information is not lost. You may use the configured `CAS` capability for computation.

The scheduler omits the `internal-search` capability from the prompt and supplies the sealed workspace for brainstorm and multi-discipline tasks. It applies the same isolation to a discovery-sprint computation task and supplies that task's sprint-specific computation prompt. It should explicitly require the worker to work within the given portfolio. Each mode also receives the concise mode-specific description in Section 7. The verifier receives its separate strict prompt and report schema from Section 7.4.

## 9. Runtime workflows

### 9.1 Bootstrap, coordination states, and assignment cycle

On first start, the scheduler performs one idempotent bootstrap transaction:

1. Validate the bootstrap manifest and freeze foundation-policy version 1. The foundation is not changed in place during that project's lifetime. A materially different foundation or root hypothesis starts a separate project with a separate memory namespace; this specification defines no in-place foundation migration, and no published fact's recorded version is edited.
2. Allocate an obligation ID for the root problem, store it as `root_obligation_id`, and define the reserved relation target `ROOT` as an alias for that ID.
3. Allocate and publish any additional initial obligations and stage initial routes, memos, and claims through their normal validation paths.
4. Store any suggested seed theorem outside canonical memory as possible input for the main agent's first assignments; it has no truth status.
5. After the initial route, memo, claim, and obligation proposals reach a stable result, run the initial trim and commit the first category portfolio.

The scheduler has an assignment gate with six durable states:

- `open`: the main agent may create a new assignment batch;
- `reviewing_trim`: a batch or stuck report is awaiting the trimmer's `no_trim`/`trim` decision, so no later batch may start;
- `trimming`: no new main-agent assignment is accepted;
- `waiting_for_human`: no new main-agent assignment is accepted until the guidance request is answered or explicitly cancelled;
- `resolution_pending`: no research assignment is accepted; accepted work drains and at most one proof-writer task may run;
- `completed`: the project accepts no assignments unless its resolving fact is revoked.

When a verified fact proves or disproves `ROOT`, the scheduler stores its ID and outcome, enters `resolution_pending`, and invokes one terminal main-agent call that may assign one proof-writer or decline it. After accepted work and that optional task close, it enters `completed`.

After bootstrap, an assignment cycle is triggered only when the gate is `open`, at least one non-verifier slot is free, and a relevant event has occurred: initial portfolio commit, a task completion or terminal interruption, a trim/human-guidance commit, or an explicit retry/resume action. The cycle is:

1. While the gate is `open`, the scheduler reserves a unique batch ID and invokes one main-agent planning call with the root problem and target obligations, current category portfolio, all task summaries created since the main agent's last durable checkpoint, and the current running-task manifest.
2. The main agent decides a complete batch no larger than the available non-verifier slots.
3. Only after that decision is final, it calls `task-writing` once for each proposed assignment.
4. The scheduler validates the batch as a unit, records accepted reports/cards and launch intents atomically, reserves slots, and launches workers under the stored intents. A replay of the batch or report IDs returns the previously allocated task IDs rather than creating duplicates.
5. If the main agent has not reported `stuck` and called trimmer for consecutive 8 (this is an experimental number, and could be changed later) worker assignments, the scheduler should enter `reviewing_trim` and forwards the 8 accepted assignment reports to one trimmer call. If the trimmer returns `no_trim`, the gate reopens; if it returns `trim`, the gate enters `trimming`. The decision does not retroactively cancel accepted work.
6. Worker results are ingested as they arrive. A completed task notifies the main agent through its summary and resulting canonical changes.

If the main agent thinks the research direction is stuck, it emits a `stuck` coordination report under the reserved batch ID. The scheduler enters `reviewing_trim` and sends that report to the trimmer, so the system cannot deadlock merely because there was no assignment report. If the main agent deliberately waits for already running or pending work, it emits `wait_for_results`; this is valid only when at least one identified task or downstream operation can still produce an event. The scheduler records the current event cursor and does not invoke planning again until a new relevant event arrives. Without such pending work, the main agent must emit `stuck/no_assignment` instead.

If the main agent exits due to interruption (such as internet disconnection), the scheduler should try to restart it. If it cannot be restarted for several times, the scheduler should preserve the current progress and exit, and waits human command to restart again.

Only one main planning call and one trimmer decision call may be live at a time. Wake-ups that arrive while either is live are coalesced behind durable event cursors. An idle slot is allowed. “Wait for other workers” means that the scheduler preserves current state without inventing an assignment.

### 9.2 Trimmer

The trimmer organizes research attention. It has 2 modes: select mode and maintain mode

Each time it receives an assignment batch, or receives `stuck` report, it reads that report, current category summaries, and the necessary recent task summaries and memory abstracts using progressive disclosure before returning either `no_trim` with a reason or requesting `trim`. If it receives assignment batch, it SHOULD request trim when the project is stuck, when several tasks have produced only small progress in essentially one direction. These remain semantic judgments; the scheduler does not reduce them to a numerical score.

When trim begins, the scheduler closes the assignment gate and records a consistent event-log cutoff. Already accepted workers continue, and their progress, verification, and memory ingestion continue. The main agent may not create a new batch.

When the trim begins, it should first go to maintain mode, then go to select mode. 

In the maintain mode, the goal of the trimmer is to maintain and redraw the categories. The trimmer should reads:

1. all existing category summaries;
2. Task summaries created since the previous trim;
3. additional task artifacts when the summaries are insufficient, only read them if you need them for your decision;
4. relevant memory abstracts if you need, and only read full memories if you need them.

It may append new memories to existing categories, create categories, rename or revise categories, change membership, merge redundant categories, or split a category. It SHOULD split a category when materially distinct research directions are being hidden by one broad summary or the summary can no longer remain concise enough for token-saving navigation. Memories may remain in zero or multiple categories.

In the select mode, the goal is to assign the work portfolio of the agent in the next few directions: 
The trimmer chooses a category portfolio for the main agent, which is the union of several categories. The general advice for assigning the categories are: the number of memories in the portfolio should be medium, giving the agent enough memories to develop the ideas, combines different ideas together, and to attack root problem and obligations, but should not be too large that exceeds the processing ability of main agent; when the main agent is getting stuck, the category should be chosen flexibly and diversified, and it could include underdeveloped directions, so that the main agent could have better chance to jump out of the loop; do not choose highly overlapping categories consecutively

If you think you cannot let the agent to jump out of the loop by choosing portfolios, or if the agent is still stuccoed after more than one trim modes, call $discovery-sprint. Then have the scheduler invoke a fresh summarizer worker as specified in Sections 8.6 and 9.2.1, and use its report to assign the category portfolio.

If the agent is still stuck after trimming and calling $discovery-sprint, or if you think there are several promising directions that you cannot cover them concurrently, you should call $human-guidance. When you write the report, you should list several possible research directions and ask the human to choose one or to suggest a new one. 

The trimmer emits category proposals and a category-portfolio proposal containing:

- proposal IDs for new categories, existing category IDs with expected revisions, and requested membership/content changes;
- expected revision of the current portfolio;
- flattened member IDs grouped by memory type;
- base event ID and a later `confirmed_through_event_id` after reviewing deltas;
- concise selection rationale;
- optional human-guidance reference.

Before commit, the scheduler checks that every referenced record exists and mechanically compares the proposal's event/revision bases with current state. It sends the trimmer every later event that touches a referenced memory, category, task summary, or status. The trimmer then revises the proposal or returns a new `confirmed_through_event_id`. Commit succeeds only if all referenced revisions and statuses still match through that event; unrelated later events do not block it. The scheduler allocates IDs for new categories, increments category revisions, allocates the new portfolio version, substitutes proposal IDs, and atomically stores the category revisions and portfolio snapshot. It then supplies the portfolio to the main agent and reopens the gate. That portfolio remains active for however many assignment cycles occur before the next trim; this is the meaning of “the next few rounds.”

The committed category portfolio is stored as a Markdown snapshot. Category member lists may retain revoked fact IDs for historical orientation, with visible status. The `fact` section of a worker assignment portfolio, however, may contain only active fact IDs.

If the trimmer invokes `human-guidance`, Section 8.5 controls the pause and resume behavior.

#### 9.2.1 `discovery-sprint` execution

A discovery sprint is a persisted subworkflow of `trimming`, and the assignment gate remains closed throughout it. When the trimmer invokes `discovery-sprint`, the scheduler allocates a sprint ID and stores the skill input and its returned `no_sprint` or sprint plan. For `no_sprint`, it returns the stored reason to the trimmer, which continues its ordinary trim. For a plan, the scheduler validates the target obligation ID and revision, the four required lane modes, all referenced IDs and active-fact statuses, the sealed access policies, and the four-slot requirement. These checks are mechanical; the scheduler does not judge whether the project is stuck or whether the lanes are mathematically different. Once the plan is persisted, the plan-producing trimmer call ends at a durable continuation point rather than remaining live throughout the sprint.

The scheduler then:

1. waits until all four non-verifier slots are free, calls `task-writing` once for each trimmer-authored blueprint, and atomically accepts the four sprint assignment reports, creates their immutable task cards, reserves the slots, and launches all four lanes together; if the reservation fails, it launches none and resumes the persisted wait unless the sprint is cancelled; the sprint assignment records are linked to the sprint ID and excluded from the eight-assignment trimmer-review counter;
2. enforces the separate sealed workspaces and prevents every lane from receiving any other lane's card, progress, computation, output, or proposed memory operation before the barrier, while processing each lane's progress and task lifecycle through the ordinary workflows;
3. opens the barrier only after all four lane tasks have closed under Section 9.6, including terminal dispositions for every operation, computation, and challenge submitted by the lanes, and records every missing or interrupted lane explicitly;
4. freezes a synthesis input containing the stored sprint plan, the four final or interruption summaries, the relevant computations, and the canonical changes produced by the lanes; it SHOULD omit current route rankings and category-selection rationales; it then launches one independent fresh summarizer worker session as one idempotent logical call, and every transport retry uses that exact stored input;
5. validates and stores the synthesis report as a control or audit artifact, then invokes one trimmer continuation with the report, the sprint's canonical changes, and all event deltas since the trim cutoff; that continuation resumes the ordinary maintain and select modes and commits any category or portfolio changes through Section 9.2.

For this call, the fresh summarizer compares the four lane results and produces exactly the synthesis report specified in Section 8.6: lane mechanism fingerprints, genuine differences versus renamed variants, shared bottlenecks and contradictions, possible bridges, informative negative results, and concrete follow-up tasks with suitable worker modes. It must distinguish verified facts, unverified claims, computational evidence, conjectural ideas, and missing or interrupted results. It does not treat agreement as proof, edit canonical memory, or process any individual fact, route, claim, or obligation addition or update.

If the synthesis call exhausts its transport retries or repeatedly returns an invalid report, the scheduler records `needs_attention`, retains the frozen input, and keeps the gate in `trimming`. It proceeds only after an authorized retry, explicit sprint cancellation, or human redirection; cancellation or redirection is recorded before the trimmer continuation receives the available sprint artifacts and event deltas.

### 9.3 Progress ingestion

For every progress file, the scheduler:

1. validates the task, attempt, sequence, access authorization, schema, target revisions, and all IDs;
2. records the progress ID as received before doing downstream work;
3. rejects each invalid operation with an explicit error for the worker to repair, without partially applying that operation; independent valid operations in the same progress file may continue;
4. routes each valid operation to the appropriate pipeline;
5. applies accepted changes atomically with all reverse-link and index changes;
6. marks each operation ID committed, rejected, or pending so replay is safe.

The operation routing is:

- `fact` → synthesizer containment review, then duplicate resolution or verifier, followed by revision or publication;
- `route_add` and `obligation_add` → synthesizer duplicate/progress review, then duplicate resolution, validated update, or validated addition;
- `route_update` and `obligation_update` → scheduler patch validation and atomic application;
- `memo` and `claim_add` → scheduler schema validation and publication;
- `claim_remove` → replacement resolution, then atomic withdrawal and route-link cleanup;
- `obligation_remove` → scheduler archival removal and cross-link cleanup;
- computation artifacts from `CAS` → scheduler reproducibility validation and publication;
- `fact_challenge` → verifier challenge review, then closure, `needs_attention`, or Section 9.7 revocation.

Raw progress remains in the task archive regardless of whether its proposals are accepted. A rejected proposal is never presented to another agent as canonical memory.

If an invalid operation is still relevant and the worker is live, the scheduler delivers the validation error to that attempt. If the attempt already ended and its summary marked the operation as required for completion, the scheduler opens a correction attempt under the same task and supplies the error in an immutable supplement. A rejected nonessential operation is recorded and does not by itself reopen the task.

### 9.4 Fact verification and revision

Each worker Fact proposal is immutable. Scheduler-owned predecessor normalization creates an immutable internal successor for the same exact proposal; a worker correction is a different proposal with fresh candidate and proposal IDs. After every predecessor and body publication gate is resolved, the scheduler computes a verification-bundle digest over the exact statement, proof, predecessor Fact IDs and core hashes, external references, foundation policy, and any `root_resolution` with the root-obligation hash. It supplies that exact bundle to the verifier. Changing any semantic component requires a fresh worker proposal or a scheduler-owned normalization successor and a new verification report.

If the verifier fails to return a valid report because of transport, internet, token, or process failure, the scheduler retries under the configured transport policy. No verdict exists until a syntactically valid report for the exact candidate hash is received.

If the verdict is `correct`:

1. The scheduler confirms that the complete bundle digest still matches the verification report, every predecessor remains active, and adding the node preserves acyclicity.
2. It validates the worker-supplied attached metadata and confirms that the synthesizer review from Section 9.5 still matches the relevant memory revisions and event cursor. It may additionally resolve a candidate to an active fact that appeared after that review only when the statement is textually identical after purely syntactic normalization; the scheduler itself does not judge mathematical equivalence.
3. The scheduler either records that exact-duplicate resolution or atomically publishes a new active fact, graph edges, external-reference index entries, and attached metadata.
4. If the verifier confirmed `root_resolution`, the scheduler sets `root_solution_fact_id` and `root_resolution_outcome` and starts the completion workflow in Section 9.1.

If the verdict is `incorrect`, the exact proposal ID is terminal immediately and its unpublished dependent chain is rejected. If fewer than two verifier-triggered revision requests have been issued for the repair chain, the scheduler creates a new revision attempt under the same task ID and immutable task card, supplies the full verification report in an immutable attempt supplement, increments the repair-chain count, and resumes or relaunches the originating worker. A revision attempt does not consume the infrastructure-interruption retry count. The worker must either:

- submit a genuinely modified proof with fresh operation, candidate, and proposal IDs and `candidate_version: 1`, which goes through verification again; or
- concede that it cannot repair the proof.

The scheduler refuses to spend another verifier call on an identical rejected verification bundle. A corrected dependency list, reference, foundation basis, statement, or proof under fresh IDs creates a different bundle and may be checked. The scheduler issues at most two verifier-triggered revision requests for one repair chain. If the verifier rejects the proposal submitted in response to the second request, the scheduler issues no third revision request and automatically opens a final concession attempt under the same task ID and immutable task card. It supplies the latest verification report in an immutable supplement and requires the worker to follow the concession workflow below without submitting another Fact for that chain. Verifier transport failures do not increment this count because they produce no mathematical verdict.

When the worker concedes, it submits every smaller surviving result as a claim and does not generate a fact candidate from any such result. Each claim remains nonauthoritative and unverified. It also creates a memo describing the attempted strategy, the precise failure, and any reusable insight; this memo is required whether or not a smaller result survives. The closing worker proposal or, if needed, the main-agent closure review chooses `progress` or `failed` according to the mathematical value recovered.

### 9.5 Synthesizer review

Before a proposed new fact is sent to a verifier, or a proposed new route or obligation is published, the scheduler invokes the synthesizer with the immutable proposal, its operation ID and digest, a current event cursor, and project-wide search access to the corresponding memory type. The synthesizer uses progressive disclosure: it searches and reads abstracts first, then reads complete candidate matches only as needed. For facts, only active facts may resolve a proposal as already represented. This review does not enlarge the originating worker's access and is required even when that worker already performed its own duplicate search.

The synthesizer returns exactly one of these structured resolutions with a concise mathematical explanation and the IDs and revisions on which it relied:

- `new`: the proposal is not already represented and continues through its ordinary addition pipeline;
- `duplicate`: an existing canonical record already represents the proposal and no new record is added;
- `update`: for a proposed route or obligation only, an existing record represents the same central strategy or statement but the proposal supplies genuinely new progress or perspective. The report identifies the existing record and contains a `route_update` or `obligation_update` patch restricted to the mutable fields allowed by Section 8.3.

For a `duplicate` fact resolution, the scheduler records the proposal-to-existing-active-fact mapping and does not invoke a verifier or publish a new fact, unless an unconfirmed `root_resolution` still requires verification. The synthesizer does not judge whether a genuinely new fact is true; every fact resolved as `new` must still pass exact-version verification. For a duplicate route or obligation with no new progress or perspective, the scheduler records the proposal-to-existing-record mapping and publishes nothing. For an `update` resolution, it validates the target ID, expected revision, references, and patch shape, applies the patch atomically, and maps the original add proposal to that existing record. The synthesizer must return `new`, rather than `update`, when applying the proposal would require changing an immutable strategy description, obligation statement, or predecessor basis.

Before commit, the scheduler checks that the synthesizer's input digest, relied-on revisions, statuses, and event cursor are still current. If a relevant record changed, it supplies the delta and obtains a revised or reconfirmed report; it never guesses how to rebase a semantic decision. The scheduler persists the call ID, exact input, result, retry count, and lease epoch. Transport or process failure produces no semantic resolution and follows the configured synthesizer retry policy; exhaustion leaves the operation in `needs_attention` rather than publishing, updating, rejecting, or verifying it.

### 9.6 Task completion

A worker's attempt-final summary moves the task to `attempt_ended`; it does not itself close the task. The scheduler then enters `postprocessing` and waits for every submitted operation, computation, and challenge to reach a terminal committed, rejected, or abandoned disposition. Pending soft references are not downstream operations: any target still unpublished at finalization is durably annotated `(unpublished)` and does not delay closure. A `needs_attention` item pauses closure until an authorized retry, abandonment, or redirection resolves it.

Whenever a verifier rejects a Fact proposal whose repair chain has received fewer than two verifier-triggered revision requests, the task enters `revision_pending` and opens a new numbered revision attempt with the original task card plus an immutable feedback supplement, whether or not the Fact was marked as completion evidence. The old proposal remains rejected; the response must use fresh IDs. A rejection after the second revision request instead opens the final concession attempt defined in Section 9.4. If any other operation marked as completion evidence is rejected, it likewise forces a revision attempt. A rejected nonessential non-Fact operation remains in the audit and does not force revision, and failed soft references merely retain their `(unpublished)` annotation. If a worker process is lost, the task enters `retry_pending`; an infrastructure retry uses a new attempt number and does not edit the task card. Exhausting the configured worker retry limit closes the task as `interrupted` and notifies the main agent.

Exhausting a verifier transport retry limit does not close or mathematically reject the proposal. Exhausting a synthesizer or summarizer transport retry limit leaves the corresponding containment review or discovery-sprint summary call durably pending. Either case creates `needs_attention` and notifies both the main agent and human operator. They may request another retry; a nonessential verifier proposal may be abandoned, and a task or sprint may be redirected or cancelled under its applicable workflow. Abandoning completion evidence requires a main-agent closure review rather than silently preserving `finished`.

Once downstream work has terminal dispositions, the scheduler performs only mechanical outcome checks. It assigns `interrupted` after unrecovered abnormal termination. Otherwise it accepts the closing worker's proposed `finished`, `progress`, or `failed` when that proposal is consistent with the verifier results and declared completion-evidence IDs. If significance or completion remains semantically ambiguous, the main agent reviews the objective, cumulative attempt summary, and downstream results and chooses one of those three outcomes. The scheduler then persists that reviewed status and the closing cumulative summary as the task memory, with immutable references to the one task card and all attempts and artifacts.

If an attempt is interrupted, already submitted valid operations continue through ingestion; a later successful attempt may still let the task close normally. If retry exhaustion closes the task without any valid attempt-final summary, the scheduler creates a mechanical interruption summary from the assignment, last durable progress, and interruption/retry history and does not invent mathematical conclusions. The scheduler notifies the main agent using the final concise task summary, accepted/rejected operation results, and IDs of canonical changes. The main agent reads raw artifacts only if it needs more detail.

### 9.7 Revocation

A confirmed challenge or explicit human order starts one atomic revocation transaction. With dependency edge `P → F` meaning “the proof of `F` uses predecessor `P`,” the scheduler follows outgoing edges from the challenged fact to find every descendant. In that same transaction it:

- marks the root and all active descendants revoked and records for each one the root cause, report, time, and dependency path;
- removes them from the active fact index without deleting any file or historical edge;
- fences and invalidates in-flight candidates that depend on a newly revoked fact and returns them for revision;
- marks affected obligations `unsupported` and restricts their assignment as described in Section 4.1;
- removes revoked IDs from routes' current-support indexes;
- clears `root_solution_fact_id` and `root_resolution_outcome` and reopens the assignment gate if the resolving fact or a predecessor is revoked;
- installs a guard that prevents any new assignment or publication from materializing a revoked fact as an active premise.

Search, category, portfolio, task, memo, claim, and computation displays are then refreshed through derived status overlays. Immutable or append-only historical records are not edited merely to add the word “revoked.”

Repair uses the normal new-fact pipeline and a new ID. Nothing is automatically reactivated, and no dependent fact regains active status without a newly verified proof against active premises.

If a structured external source is challenged or retracted, the scheduler uses the external-reference reverse index to open a fact challenge for every active fact that directly used that source. Revocation propagates through the ordinary fact graph only after each affected proof is confirmed invalid; a bibliographic change alone is not silently treated as a mathematical verdict.

## 10. Preservation of freedom and implementation choices

This specification intentionally does **not** impose:

- numeric scores for route value;
- a deterministic best-route policy;
- a fixed exploration quota or mathematical stagnation formula;
- a prescribed mathematical method within a worker mode;
- a fixed physical directory layout, serialization library, search library, or process manager;
- a ban on reading outside the current category portfolio for the main agent or ordinary searchable worker modes;
- a proof-revision limit other than the explicit two-request repair limit in Section 9.4.

Those are genuine implementation or agent-judgment choices, not missing semantics. Any implementation is conforming only if it preserves all of these core invariants:

1. Among memory records, active facts alone are established mathematical truth; the only non-memory premises are the frozen project axioms, definitions, root hypotheses, and verifier-checked external results recorded inside a published fact.
2. No fact is published before exact-version verification.
3. Published fact cores are immutable; revocation is historical and transitive.
4. The scheduler alone commits canonical state and enforces hard access, integrity, recovery, and concurrency rules.
5. Semantic mathematical choices stay with agents.
6. Brainstorm, multi-discipline, and discovery-sprint computation memory isolation is enforced, not merely requested.
7. The main agent remains free to look beyond its category portfolio.
8. At most four non-verifier workers run at once; verifiers use their separate configured limit.
9. Negative results, diverse portfolios, and free attack after task completion remain valued and permitted.
10. All durable work is auditable, replay-safe, and recoverable after a full stop.


## 11. Possible misunderstanding and clarification
1. Sections 8.1 - 8.6 describe 6 original Codex skills, not 6 LLMs (except 8.6 requires the scheduler to call 4 models). Section 8.2.1 additionally defines the read-only `task-search` skill for the main agent and trimmer.
2. Facts SHOULD be really important claims, not ordinary small claims. Small or unimportant claims should go to `claim`
3. The prompt of main agents and trimmers should be concise while clear. The current description is a description on its function, rather than the actual prompt.
4. Main agent should resume session until the trimmer sends a new category portfolio. Trimmer can resume session for consecutive 3 rounds, and then have a fresh start. For workers, a fresh session is the default; the main agent requests a resume by putting the previous task ID in the new task card's `if_resume` field, and the scheduler forbids a seventh resume in one worker-session lineage.
