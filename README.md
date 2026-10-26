# PDS Atmospheres Node — Integrated Archive Tables

Builds the per-body "integrated archive" pages for the PDS Atmospheres Node
website (Mercury, Venus, Mars, Jupiter, Io, Europa, Ganymede, Callisto,
Saturn, Titan, Uranus, Neptune, Pluto) directly from the PDS registry,
instead of hand-maintaining them. Each target gets a standardized table of
instruments × observation years, organized into sections and categories
(e.g. Electromagnetic Spectrum > IR, Atmosphere > Clouds) that come from
science sorting labels on the PDS bundle labels themselves.

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
| `--years {collection,bundle}` | Where each row's years come from: the instrument's science collections inside each bundle (`collection`, the default), or each bundle's own time range (`bundle`). Many bundle labels carry no time range, so `bundle` puts their data under "No Date". |
| `--targets-from {collection,bundle}` | Which targets' tables an instrument appears on: those its science collections list (`collection`, the default), or those its bundle lists (`bundle`). With `collection`, a collection's parent bundle is read for the sorting labels even if the bundle doesn't list that target. |
| `--no-instrument {drop,observatory}` | What happens to a bundle or collection that names no instrument (e.g. ground-based photometry that names only an observatory and telescope): left out (`drop`, the default), or listed under its observing system's name, e.g. "NASA InfraRed Telescope Facility (IRTF) (no instrument)", sorted by labels paired with its telescope or facility LID (`observatory`). |
| `--refresh-contexts` | Force a live re-fetch of the registry's context-product catalog (instruments, spacecraft, investigations, targets) instead of using the on-disk cache. Can take several minutes; the catalog rarely changes, and a target missing from the saved catalog triggers a refresh on its own, so this is rarely needed. |

Example:

```bash
# Full picture across every PDS node, plus diagnostic output, for one target
pipeline-build SATURN --node ALL --diagnostic
```

## How categorization works

Instruments are sorted by **science sorting labels** carried on PDS4 bundle
labels (a sorting dictionary with the namespace `science_category_sorting`).
Each bundle lists pairs of one `instrument_lid` and one
`science_sorting_label`, and each label is written `Section:Category`:

- `Atmosphere:Clouds`, `Electromagnetic Spectrum:IR`, ... — the instrument
  gets a row in that section of the table. Sections and categories are
  whatever the labels say; the sorting dictionary is their master list.
- `Technique:Imaging`, `Technique:Spectroscopy`, ... — no row; these become
  the instrument's technique tags, used by the pretty table's technique
  filter.

An instrument with several labels on a bundle has one pair per label. The
pairs apply to that bundle only, so an instrument can be sorted differently
in different bundles.

`pipeline_rules.yaml` no longer decides categories. It holds only:

- `display_order` — the order sections and rows appear in. Anything the
  labels use that isn't listed still appears, after the listed ones,
  alphabetically.
- mission types and landing pages (`mission_type_display`, `mission_types`,
  `mission_landing_pages`), keyed by PDS investigation name;
- per-instrument fixes (`omitted_instruments`, `instrument_type_overrides`,
  `display_name_overrides`, `collection_date_overrides`).

**Temporary:** until bundle labels in the registry carry the sorting pairs,
`src/pds_tars/interim_sorting_labels.py` fills them in from
`interim_sorting_labels.csv` (one row per pair), in the exact form the
registry will return them. The `bundle-sorting-registry-labels` branch
removes it.

## Layout

```
pyproject.toml         package metadata, dependencies, pipeline-build entry point
src/pds_tars/
    build.py            entry point — CLI, registry queries, orchestration
    registry.py          PDS registry querying, saved results + on-disk caching
    rule_engine.py        mission type / landing page lookups from pipeline_rules.yaml
    schema.py            pipeline_rules.yaml loader, "Section:Category" parsing, ordering
    render_html.py        HTML table generation (pretty/basic/diagnostic)
    pipeline_rules.yaml    display order, mission metadata, per-instrument overrides
    interim_sorting_labels.py/.csv   TEMPORARY stand-in for registry sorting labels
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
- `registry_results/` — the last successful result of each per-target
  query (a target's bundles and collections, and parent bundles fetched by
  LID). Overwritten on every successful run; used only as a fallback.

Every registry query asks only for the fields the pipeline reads (peppi's
`.fields()`), which keeps requests small.

## When the registry is down

The pipeline is meant to run unattended, so it falls back rather than
failing wherever it can, and says so in one line:

- A failing registry request is retried twice (after 10 s, then 20 s).
- If it still fails, the saved copy is used: the context caches, or the
  query's saved result in `cache/registry_results/`, with a `NOTE:` line
  giving the date it was saved.
- Only when there is no saved copy at all does a target fail, with the
  reason (registry not responding) and no traceback; other targets still
  run. A run with no context caches and no registry stops before any
  target, saying why.
- A single product the registry returns in an unexpected shape, or a
  bundle with mismatched sorting pairs, is skipped with a `WARNING:` line;
  the rest of the target still builds.
- An unknown target name triggers one refresh of the target catalog (in
  case it's newly archived) before it's reported as unknown.

The exit code is non-zero if any target failed. An unexpected error is
reported as a likely bug, with a traceback for reporting it.

## Tests

```bash
.venv/bin/python -m pytest tests/
# or, after `conda activate pds-atm-pipeline`:
pytest tests/
```

All tests are self-contained (no network calls; the few that touch files use
pytest's temporary directories) — they exercise the label parsing,
table-building, caching and fallback functions directly.

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

This pipeline queries the live PDS registry on every run (falling back to the
saved copies above only when it doesn't respond) — output can change as new data gets archived or migrated
into PDS4, and a fresh run is the only way to pick that up. There is
currently no scheduled/automated rebuild; someone needs to run
`pipeline-build` and redeploy the output when a refresh is wanted.

Developed with the use of Claude AI