# Franta operator guide

Start with the [README](../README.md) for installation and a first project.
`PROJECT` below means the generated research directory, not the source checkout.
Replace IDs in command examples with values returned by `franta status`.

## Bootstrap configuration

The [example manifest](../examples/bootstrap.toml) is the reference starting
point. All project/input paths are relative to the manifest directory.

| Table | Purpose |
| --- | --- |
| `[project]` | Required name, output directory, root problem, and foundation policy |
| `[models.default]` | Persisted main, trimmer, verifier, challenge-verifier, and closure-review model route |
| `[models.synthesizer]` | Persisted synthesizer route |
| `[tools]`, `[tools.extra_cas]` | Executable configuration |
| `[retries]` | Positive retry counts for the supported call kinds |
| `[timeouts]` | Optional `agent_call_seconds` watchdog |
| `[limits]` | Concurrency, search, and fixed workflow limits |
| `[context_budgets]` | Positive token budgets for materialized contexts |
| `[agents]` | `native_web_search` must remain `true` |
| `[explorer]` | Optional alternation and exploration limits |
| `[advisor]` | Optional human-selected next-cycle planning; requires Explorer |
| `[initial]` | Structured seed obligations, routes, memos, claims, and seed theorems |

For the problem and foundation, supply exactly one inline value or file:
`root_problem` / `root_problem_file` and `foundation_policy` /
`foundation_policy_file`. Do not include both forms of the same input.

`max_non_verifier_workers` is 1–4. Some other limits encode fixed workflow
invariants and cannot be tuned freely; the parser reports invalid values.
Explorer's worker limit cannot exceed the non-verifier worker limit.

The runtime saves configuration in `private/runtime-config.json` and a copy of
the input manifest in `bootstrap-manifest.toml`. Reopening reads persisted
configuration. A different manifest cannot silently alter an existing project.
There is no general project-relocation or configuration-migration command;
create a fresh project when changing environment-dependent settings, and keep
the original project intact for inspection/recovery.

## Tools and confinement

Install optional tools using their upstream instructions:
[SageMath](https://doc.sagemath.org/html/en/installation/),
[Macaulay2](https://macaulay2.com/), and
[Tectonic](https://tectonic-typesetting.github.io/).
Franta does not install these tools and does not enable an unconfigured CAS.

```toml
[tools]
codex = "codex"
sage = "sage"
macaulay2 = "M2"
tectonic = "tectonic"

# Optional additional trusted CAS executable, chosen by the operator:
# [tools.extra_cas]
# singular = "Singular"
```

A bare name is found on `PATH`; a path containing a directory component is
resolved relative to the manifest, with `~` expanded. Absolute paths are
accepted. Each value names one executable; shell command strings, arguments,
pipelines, and environment assignments are not parsed here.

For a Conda installation, activate the environment before running Franta and
use its actual `sage` executable. Executable wrappers may be used, but they
must work inside confinement: their runtime dependencies must be in allowed
system locations or the wrapper's installation prefix. A wrapper that reaches
into an unrelated private installation can be denied.

Check enabled tools from the same shell before initializing a project:

```sh
command -v codex
sage --version
M2 --version
tectonic --version
```

CAS execution uses a process owned by one agent call, captures outputs and
versions, and stages nonauthoritative evidence. Cancellation or timeout also
terminates owned subprocesses. Computation results do not bypass verification.

On macOS, confinement uses `/usr/bin/sandbox-exec`. On Linux, install your
distribution's `bubblewrap` package and ensure `bwrap` can create user
namespaces under the host's security policy. Both restrict file access and
network access. If confinement is unavailable, Franta refuses the CAS/report
operation instead of launching it unconfined. A basic Linux smoke check is:

```sh
bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev /bin/true
```

Tectonic reports are compiled with `--only-cached --untrusted`; they cannot
download missing TeX files. Before enabling report compilation, compile a
small document online in an ordinary terminal using the same installation
and user account. For example, create a temporary `warmup.tex` with:

```tex
\documentclass{article}
\usepackage{amsmath,amssymb}
\begin{document}
Franta report cache preparation: $1+1=2$.
\end{document}
```

Then run `tectonic warmup.tex`. Preload any additional packages your reports
will use too. The runtime reads the platform's Tectonic cache, including Linux
`XDG_CACHE_HOME` when set. Set `TECTONIC_CACHE_DIR` to use an explicit cache
directory. A missing cached file causes compilation to fail;
the human-guidance workflow requires a successfully compiled report.

## Models and runtime compatibility

These settings describe the shipped source, rather than model availability:

| Route | Default model | Default reasoning |
| --- | --- | --- |
| Main, trimmer, verifier, challenge verifier, closure review | `gpt-6-astra` | `ultra` |
| Synthesizer | `gpt-6-astra` | `xhigh` |
| Workers, proof writer, discovery-sprint lanes and fresh sprint summarizer | `gpt-6-astra` | `max` |
| Explorer | `gpt-6-astra` | `max` |
| Main sorter | `gpt-6-astra` | `ultra` |
| Advisor | `gpt-6-astra` | `ultra` |
| Dashboard monitor | Persisted default model | `medium` |

`[models.default]` and `[models.synthesizer]` configure their persisted routes.
Worker and Explorer routes remain fixed in their source modules. Advisor
settings also expose its model and reasoning fields. Changing only
`[models.default]` therefore does not change every model call. The transport
maps persisted `gpt-5.6-sol` settings to `gpt-6-astra`, retaining the reasoning
effort. Research launches set `model_context_window = 872000` and
`model_auto_compact_token_limit = 780000`.

The CLI must support the flags and permission configuration used in
`src/franta/execution_gateway/transport.py`, including clean configuration,
structured output, session resume, and `agents.enabled=false`. The transport
checks the rendered instructions and rejects launches that still expose
unscheduled subagents. CLI version `0.148.0` was the original compatibility
baseline; this is not a guarantee that every later CLI is compatible.

Use the [official Codex documentation](https://learn.chatgpt.com/docs/codex/cli)
for CLI installation. Authentication and file credential storage are documented
by [OpenAI](https://learn.chatgpt.com/docs/auth). Franta copies host `auth.json`
into its private Codex home once and retains refreshed credentials/session
state there; host configuration and global skills/plugins are excluded.

The configured account must support all routes used by your workflow. An
offline test pass cannot validate model entitlement, account limits, network
access, or the live CLI's permission behavior.

## Running, stopping, and recovery

```sh
franta init /path/to/bootstrap.toml
franta start /path/to/bootstrap.toml --no-dashboard
franta status PROJECT
franta resume PROJECT
franta evaluate PROJECT
```

Use `Ctrl-C` to interrupt the foreground runner, then `resume` to continue.
Recovery fences lost leases, rejects stale outputs, preserves already staged
progress, and relaunches persisted work. Do not start two writable runners for
one project: the project lock coordinates the scheduler and dashboard resumes.

`--max-cycles N` on `start`/`resume` bounds the scheduler event loop. It is not a
wall-clock deadline or token limit, and one cycle can launch a long model call.
`--max-cycles 0 --no-dashboard` provides an initialization/recovery check without
running the event loop. Worker calls have a four-hour watchdog; the optional
`[timeouts].agent_call_seconds` controls other supported call deadlines. CAS
also has its own bounded execution deadline.

The generated directory contains:

| Path | Contents |
| --- | --- |
| `scheduler.sqlite3` | Authoritative scheduler/memory state |
| `canonical/`, `indexes/`, `categories/`, `portfolios/` | Readable generated memory views |
| `private/` | Runtime configuration, sessions, credentials, receipts, Explorer store, and internal control state |
| `workspaces/` | Scoped per-call input, artifacts, and outputs |
| `audit/`, `task-archive/` | Audit and completed task records |
| `root-problem.md`, `foundation-v1.md` | Frozen research problem and foundation |
| `advisor-reports/` | Archived Advisor reports and assignments when enabled |

For a backup, stop the runner and its dashboard processes before copying the
whole project, including private state and any SQLite sidecar files. Copying
only `canonical/` is not sufficient to resume. Active projects can contain
absolute paths and persisted process/session context; moving an active project
to another machine is not a supported migration workflow.

## Explorer and Advisor

With `[explorer] enabled = true`, Franta's manifest defaults allow a two-hour
Explorer admission window, three attempts of up to three hours per lineage,
and then an eight-hour Franta admission window. Already admitted work drains
after admission closes, so a full turn can take longer than its admission
window. The first Explorer clock begins after stable bootstrap, not at `init`.

Explorer scratch and summaries are append-only and provisional. Only the
task-bound sorter receives the frozen turn and may propose selected material
for Franta integration. Adding `[advisor] enabled = true` introduces a human
selection gate after each Franta turn.

Advisor proposes exactly five obligations and accepts one or two choices.
The CLI accepts inline JSON or a file:

```sh
franta advisor-feedback PROJECT REQUEST_ID @/path/to/feedback.json
franta resume PROJECT
```

Example file:

```json
{
  "choices": [
    {"kind": "listed", "obligation_id": "OBLIGATION_ID"},
    {"kind": "custom", "statement": "State the complete custom subproblem here."}
  ],
  "instructions": "Additional context for these two choices."
}
```

`choices` is the binding decision; `instructions` cannot replace or contradict
it. The next cycle receives the original problem and the selected subproblems.
The original ROOT remains immutable.

## Human guidance and dashboard

`suggest` queues an unsolicited research suggestion in the immutable inbox:

```sh
franta suggest PROJECT "Investigate the relative case first."
franta suggest PROJECT @/path/to/guidance.md
```

The next eligible Explorer attempt 1 or Franta Main call receives it once.
Explorer must follow it; Main must assign its first worker to follow it.
Advisor leaves the suggestion pending. Inputs of already started calls and
recovered retries remain frozen. `status` shows delivery state.

To answer or cancel a trimmer's existing human-guidance request:

```sh
franta guidance PROJECT REQUEST_ID "My advisory response"
franta cancel-guidance PROJECT REQUEST_ID
franta resume PROJECT
```

This request/response path differs from `suggest` and requires the trimmer to
have produced its compiled report. Guidance is an instruction, not a fact.

`franta dashboard PROJECT` starts or reuses the independent local dashboard.
It shows overview/usage, canonical memory relations, and newest-first Explorer
records. Ordinary page refreshes do not invoke a model; monitor refreshes do.
The monitor runs every 5400 seconds while research is active or awaiting human
feedback and also supports manual refresh when stopped. Monitor failures keep
the previous summary and do not stop research.

The webpage remains available during an Advisor pause. Accepted current-round
Advisor feedback is queued and processed under the project lock; it can resume
a stopped project. A research suggestion alone does not resume one.

## Additional operator commands

```sh
franta rebuild-projections PROJECT
franta cancel-sprint PROJECT SPRINT_ID --reason "Operator requested cancellation"
franta refs PROJECT --state pending
franta ref-status PROJECT TMP-ID
franta resolve-ref PROJECT TMP-ID CANONICAL-ID \
  --resolution operator_correction --operation-id OPERATOR-REF-1
franta abandon-ref PROJECT TMP-ID \
  --reason "Withdrawn target" --operation-id OPERATOR-REF-2
```

Reference resolution and abandonment are explicit audited decisions. A fact
candidate's `predecessor_fact_ids` is the structured dependency authority;
publication requires resolved active predecessors and successful verification.
Other schema-declared temporary relationships are soft and do not block task
closure. An unpublished soft target retains an annotated
`TMP-ID(unpublished)` value instead of an invented canonical replacement.

Use `franta COMMAND --help` for syntax. The read-only evaluator reports `pass`,
`fail`, or `inconclusive`; missing observations are not assumed to pass. See
[evaluation documentation](../evals/README.md) for interpreting reports.
