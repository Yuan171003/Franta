# Franta 0.1.0 release validation

Validated on 2026-09-08 on macOS 26.5.2. These results describe the tested
release source; they are not a guarantee of the absence of every possible bug.

## Automated regressions

| Environment | Command | Result |
| --- | --- | --- |
| Python 3.14.6, freshly extracted source ZIP, real CAS/confinement enabled | `FRANTA_RUN_CONFINEMENT_INTEGRATION=1 PYTHONPATH=src python3 -m unittest discover -s tests -v` | 738 tests: 737 passed, 1 skipped; 110.931 seconds |
| Python 3.12.14, default offline suite | `PYTHONPATH=src python3 -m unittest discover -s tests -v` | 738 tests: 733 passed, 5 skipped; 83.581 seconds |
| Read-only synthetic evaluator | `PYTHONPATH=src python3 -m franta.evaluation --snapshot evals/fixtures/complete_observation.json` | All 9 scenarios passed |

The single skip in the Python 3.14 run is the real Linux/bubblewrap test; this
machine runs macOS. The Python 3.12 run additionally skips four opt-in real
CAS/compiler/confinement tests, all of which passed in the Python 3.14 run.
The real tests exercised SageMath 10.9 batch execution and exception reporting,
Macaulay2 1.26.06 execution, Tectonic 0.16.9 PDF compilation, rejection of prohibited file
reads, and network confinement. They did not substitute fake CAS results.

The suite also covers scheduling, persistence, recovery, reference resolution,
permission boundaries, subprocess cleanup, dashboard HTTP behavior, Explorer,
Advisor, skill receipts, and installed-resource materialization.

## Release-specific fixes and test maintenance

- Renamed the Python package, console command, configuration fields, environment
  variables, persisted identifiers, skills, documentation, and dashboard text
  consistently to Franta.
- Replaced personal executable locations with optional portable configuration;
  verified PATH lookup, manifest-relative paths, home expansion, and spaces.
- Added Linux bubblewrap confinement and platform-aware Tectonic cache lookup.
- Fixed long and multibyte project paths by using the existing authenticated,
  private file spool when a Unix-domain socket address exceeds 103 encoded
  bytes. Other unexpected socket errors remain visible.
- Included all runtime skill documents and dashboard/vendor assets in wheels
  and source distributions; retained third-party license files.
- Added allowlisted source export and checks for private runtime directories,
  known credential patterns, personal home paths, retired branding, oversized
  files, and symlinks escaping the source tree.
- Updated stale golden values after confirming the supplied prompt builders
  retained their research semantics apart from the requested branding change.
  Existing prompt-size guards now include the supplied goal and verification
  instructions without trimming those instructions.
- Corrected two outdated integration fixtures to submit genuine, validated
  task-writing receipts and legal assignment responses; retained recovery,
  promotion, session, and drain assertions.
- Replaced the personal executable path in one replay fixture with an explicitly
  documented example and refreshed that fixture's digest. Mathematical input,
  output, and event ordering are unchanged.

## Installation and publication checks

The release process builds an sdist from a clean exported source tree, rebuilds
its wheel from that sdist, and installs the wheel in a new virtual environment
outside the checkout with source imports disabled. The distribution auditor
checks 49 skill/static resources, materializes all 13 skills, reads dashboard
assets and licenses, and invokes `franta --help`.

The README's `init`, `status`, and `resume --max-cycles 0 --no-dashboard`
instructions were also exercised from a freshly installed wheel in a default
macOS temporary directory whose broker path is 147 bytes long. The commands
succeeded without source imports, copied credentials, or live model calls;
a Codex sentinel confirmed zero launches. The source ZIP is scanned using the same
publication rules. A temporary Git index successfully staged all 263 exported
files (including the export marker) and produced a valid tree; the largest file
is 456,810 bytes. No repository metadata was inserted into the source ZIP. Use `scripts/check_distribution.py` and
`scripts/export_release.py` to repeat the package/export checks.

## Limits of this validation

- No live Codex model call or long-running mathematical research evaluation was
  launched for this release. Authentication, model entitlement, service limits,
  and network access must be verified in the deployment environment.
- Linux confinement was reviewed and tested through command/error contracts;
  real Linux execution and the Python 3.11/3.13 matrix remain for GitHub Actions
  to exercise. The workflow includes a separate real bubblewrap job.
- Native Windows is not supported; use a suitable Linux environment.
- This is a fresh-project release. Previously generated internal research
  directories were preserved outside the publication set and were not migrated.
- No GitHub repository or remote upload is created by validation or export.
