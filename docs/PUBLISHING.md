# Preparing and publishing Franta

Run these commands from the source directory. Preparing a release does not
create a GitHub repository or upload anything automatically.

## Validate the source

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m franta.evaluation \
  --snapshot evals/fixtures/complete_observation.json
```

Live Codex/CAS checks are separate from the offline suite. Report the exact
platforms, tools, and live checks actually exercised when publishing a release.
The fixture evaluation is synthetic evidence, not a report about a live run.

## Export a clean GitHub source tree

```sh
python3 scripts/export_release.py
```

The default outputs are `release/Franta/` and `release/Franta-source.zip`.
Use `--output-dir /path/to/output` for another destination. The exporter copies
the publishable source and supporting files while excluding local research
projects, credentials, caches, and generated build output. Inspect the exported
tree before uploading; do not upload the entire research workspace.

To regenerate an earlier export, add `--force`. Replacement is limited to an
output directory marked by the exporter; it does not replace an arbitrary
directory. After building, `python scripts/check_distribution.py dist` verifies
the archives and installation resources. CI also runs this distribution check.

To publish with Git, create an empty repository in your own GitHub account,
then run the following **inside the exported `Franta` directory**, replacing
the placeholder URL with that repository's actual URL:

```sh
git init
git add .
git status --short
git commit -m "Initial Franta release"
git branch -M main
git remote add origin YOUR_GITHUB_REPOSITORY_URL
git push -u origin main
```

Review the staged list before committing. Keep `private/`, `workspaces/`,
research databases, local bootstrap files with personal paths, authentication
files, and research transcripts out of the source repository. The generated
ZIP can also be attached as a GitHub release asset. Users can clone the
repository or extract the ZIP and run `python -m pip install .`.

## Build Python distributions

In a virtual environment with the standard build frontend installed:

```sh
python -m pip install build
python -m build
```

This creates a source distribution and a wheel in `dist/`. Build dependencies
are separate from runtime dependencies. The wheel contains the runtime skill
documents and dashboard assets; source distributions also include the source
documentation and examples selected by the packaging manifest.

Verify a wheel in a fresh environment, outside the checkout so imports cannot
accidentally use source files:

```sh
python3 -m venv /tmp/franta-wheel-check
/tmp/franta-wheel-check/bin/python -m pip install /absolute/path/to/dist/franta_research-0.1.0-py3-none-any.whl
cd /tmp
/tmp/franta-wheel-check/bin/franta --help
```

Replace the wheel filename when the version changes. Also extract the source
ZIP to a new directory, install it there, and run the README's `init` and
`status` example. Publishing to PyPI is optional and separate from GitHub;
these instructions do not assume the package name is registered there.

## License and third-party files

The project currently retains its **Proprietary** metadata. Publishing source
does not change that choice. The author must explicitly select and grant a
license before describing a release as open source. Keep the bundled
third-party asset licenses and notices intact.
