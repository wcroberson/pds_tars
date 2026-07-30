# PDS Atmospheres Node — Integrated Archive Tables

Builds the per-body "integrated archive" pages for the PDS Atmospheres Node
website (Mercury, Venus, Mars, Jupiter, Io, Europa, Ganymede, Callisto,
Saturn, Titan, Uranus, Neptune, Pluto) directly from the PDS registry,
instead of hand-maintaining them. Each target gets a standardized table of
instruments × observation years, organized into the node's locked
category vocabulary (Electromagnetic Spectrum, Particles & Fields,
Atmosphere, Surface).

This directory is self-contained — everything needed to install, run, and
test the pipeline lives here. All commands below assume you're standing
inside it.

## Requirements

- Python 3.13+
- A dedicated environment is recommended (`pds.peppi` pulls in a
  fair number of transitive dependencies). Either a conda env or a plain
  venv works — dependencies and versions are declared once, in
  `pyproject.toml`.

## Installation

Conda (env spec in `setup/environment.yml`):

```bash
conda env create -f setup/environment.yml
conda activate pds-atm-pipeline
pip install -e .
```

Or a plain venv:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

Add `[dev]` (`pip install -e ".[dev]"`) to also pull in `pytest` for running the tests.

## Quick start

```bash
# Build Jupiter's table (default target if none given)
pipeline-build

# Build specific targets
pipeline-build VENUS MARS JUPITER

# Any target the PDS registry recognizes works, including moons with their
# own ATM-node data
pipeline-build TITAN IO
```

`pipeline-build` is the console script installed by `pip install -e .`
(see `pyproject.toml`); it's `build.py`'s `main()`, runnable from anywhere
once installed. `setup/example_build_usage.py` shows the equivalent called
directly from Python instead of the command line.

Each run writes into `tables/<target>/`:

- `<target>_integrated_archive_pretty.html` — styled interactive table
  (background image, mission chips, node/technique/text filters,
  collapsible section banners).
- `<target>_integrated_archive_basic.html` — plain drop-in HTML table, no
  images/colors/JavaScript.

## Options

| Flag | Effect |
|---|---|
| `--node NODE` | Restrict to one PDS node (`ATM`, `PPI`, `GEO`, `NAIF`, ...). Default is `ATM`. Pass `ALL` to include every node — output filenames get an `_allnodes` (or `_<node>`) suffix so they never collide with the default run. |
| `--diagnostic` | Also write a diagnostic HTML table: a plain interactive category × year table with checkboxes, per-instrument notes, and a full collection detail view per instrument. Not part of the shipped product — useful for auditing what a target's data actually looks like. |
| `--refresh-contexts` | Force a live re-fetch of the registry's context-product catalog (instruments, spacecraft, investigations, targets) instead of using the on-disk cache. Can take several minutes; the catalog rarely changes, so this is rarely needed. |

Example:

```bash
# Full picture across every PDS node, plus diagnostic output, for one target
pipeline-build SATURN --node ALL --diagnostic
```

## How categorization works

Every instrument's collections get matched against rules in
`pipeline_rules.yaml` — the single file that controls which rows
exist, rules for sorting instruments into each, mission type/landing-page
metadata, and instrument name overrides. Most rules match on PDS4 fields
the registry already provides (`wavelength_range`, `discipline`,
`facet1`/`facet2`, free-text `description`); a documented
`hardcoded_instruments` escape hatch exists for the case
where an instrument can't be sorted by its PDS metadata alone.

To add or change a category or sorting rule, edit `pipeline_rules.yaml` directly — no code
changes needed. See the comments at the top of that file for the rule
schema.

## Layout

```
pyproject.toml         package metadata, dependencies, pipeline-build entry point
src/pds_tars/
    build.py            entry point — CLI, registry queries, orchestration
    registry.py          PDS registry querying + on-disk caching
    rule_engine.py        category matching against pipeline_rules.yaml
    schema.py            pipeline_rules.yaml loader + row-ordering helpers
    render_html.py        HTML table generation (pretty/basic/diagnostic)
    pipeline_rules.yaml    category vocabulary, detection rules, mission metadata
background_images/    per-target background images for the pretty table
cache/                on-disk registry-catalog caches (see below)
tables/                output, one subdirectory per target
tests/                pytest suite for the pure/testable functions above
setup/                environment.yml (conda env spec) + example_build_usage.py
```

## Caching

`cache/` holds registry data that changes slowly enough to be worth
persisting between runs:

- `context_cache.json`, `context_targets_cache.json`,
  `context_investigations_cache.json` — the registry's context-product
  catalog (instruments, spacecraft, targets, investigations). Refreshed
  automatically after 7 days, or immediately with `--refresh-contexts`.

## Tests

```bash
.venv/bin/python -m pytest tests/
# or, after `conda activate pds-atm-pipeline`:
pytest tests/
```

All tests are self-contained (no network calls, no file I/O) — they exercise
the pure category-matching and table-building functions directly.

## Integrating into an existing website

Each output file (`_pretty.html`, `_basic.html`) is a complete, self-contained
HTML document — the simplest integration is linking to it directly or
embedding it with `<iframe src="...">`, which needs no code changes on either
side.

If the target site instead wants to pull the table into its own page layout
(a CMS content block, a template include, etc.) rather than iframe it,
`render_html.py`'s templates are split into a page shell and a content block
for exactly that case:

- `_PRETTY_TABLE_HEAD` / `_PRETTY_TABLE_CONTENT` / `_PRETTY_TABLE_FOOT`
- `_BASIC_TABLE_HEAD` / `_BASIC_TABLE_CONTENT` / `_BASIC_TABLE_FOOT`

`write_pretty_html_table()` and `write_basic_html_table()` currently render
all three concatenated (`_HEAD + _CONTENT + _FOOT`), producing the same
standalone documents as before — a future content-only entry point can
render `_CONTENT` alone against a target site's own page shell instead,
without touching the data-preparation code.

**Before pasting `_CONTENT` into an existing page**, note that both
templates' CSS uses page-level selectors — `_PRETTY_TABLE_BASE_CSS` starts
with `*{box-sizing:border-box}` and `body{...}`; the basic table's CSS has
similar bare `body`/`h1`/`a` rules (its own comment already flags it as
"override or remove entirely when integrating"). Those are harmless inside
this template's own `<html>` (the iframe/standalone case) but would restyle
the *host* page's `body`/`h1`/etc. if the content block is pasted directly
into a page that has other content on it.

## A note on live-data behavior

This pipeline queries the live PDS registry on every run (aside from the
caches above) — output can change as new data gets archived or migrated
into PDS4, and a fresh run is the only way to pick that up. There is
currently no scheduled/automated rebuild; someone needs to run
`pipeline-build` and redeploy the output when a refresh is wanted.

Developed with the use of Claude AI