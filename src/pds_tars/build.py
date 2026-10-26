"""
build.py - Main pipeline entry point for the PDS ATM integrated archive tables.

Queries the PDS registry for a target's bundles, reads each bundle's science
sorting pairs (one instrument LID + one category label per pair) to place its
instruments in categories, and writes the category × observation-year tables
to pds_tars/tables/.

By default, years come from the instrument's science collections inside
each bundle, sorted by that bundle's pairs. With --years bundle, they come
instead from each bundle's own Time_Coordinates.

Every target gets its own subdirectory (tables/<target>/, lowercased) holding
both node variants together. Filenames are prefixed with <target> plus a node
suffix when --node isn't the default ATM - "_allnodes" for --node ALL,
"_<node>" (e.g. "_ppi") for any other node:
  tables/<target>/<target>_integrated_archive_pretty.html      - styled interactive HTML table (with backgrounds, chips, filters)
  tables/<target>/<target>_integrated_archive_basic.html       - plain drop-in HTML table (no images, colors, or JavaScript)

Diagnostic output (requires --diagnostic flag), same directory/prefix/suffix convention:
  tables/<target>/<target>_diagnostic_table.html                - plain interactive HTML table

Registry-catalog caches (context products, node index) live in
pds_tars/cache/, not tables/ - see registry.py.

Targets are individual bodies, e.g. JUPITER, PLUTO, IO, TITAN - any name
the PDS registry recognizes as a context target. There's no planet+moons
"system" grouping; a moon with its own ATM-node data (e.g. Io, Titan) is
built and shipped as its own separate target like any planet.

By default, an instrument appears on a target's table when one of its
science collections lists that target, and the collection's parent bundle is
read for the pairs whatever targets the bundle lists. With --targets-from
bundle, it appears when its bundle lists the target instead.

A bundle or collection naming no instrument is left out by default. With
--no-instrument observatory, it's listed under its observing system (or
investigation), sorted by pairs whose LID is its telescope or facility.

Run with:  .venv/bin/python pds_tars/build.py [TARGET ...] [--diagnostic] [--node NODE] [--years {bundle,collection}]
               [--targets-from {bundle,collection}] [--no-instrument {drop,observatory}] [--refresh-contexts]
"""

import argparse
import os
import re
import sys
import traceback
from collections import defaultdict, namedtuple

import pds.peppi as pep

from pds_tars.schema import (
    SECTION_ORDER, SECTION_OF_CATEGORY, OMITTED_INSTRUMENTS, INSTRUMENT_TYPE_OVERRIDES,
    DISPLAY_NAME_OVERRIDES_BY_NAME,
)
from pds_tars.rule_engine import make_attrs, abbr_mission_type, landing_page_for_mission, technique_categories
from pds_tars.registry import (
    build_context, build_instrument_name_cache, build_investigation_cache,
    instrument_components, collection_fields, bundle_fields, clamp_to_investigation_dates,
    is_science_collection, is_ancillary_purpose, abbreviate, to_initials, scalar,
    investigation_identity, latest_versions, sorting_labels, parent_bundle_lids,
    instrument_names_by_lid, normalized_lids, fetch_products, bundles_by_lid, bare_lid,
    TITLE_KEY, NODE_KEY, CURRENT_YEAR, BUNDLE_QUERY_FIELDS, COLLECTION_QUERY_FIELDS,
)
from pds_tars.interim_sorting_labels import add_interim_sorting_labels
from pds_tars.render_html import write_diagnostic_html_table, write_pretty_html_table, write_basic_html_table

# Package lives at <project_root>/src/pds_tars/ - tables/
# is a sibling of src/, two directories up from this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
OUTPUT_DIR = os.path.join(_PROJECT_ROOT, "tables")


# ── Pipeline configuration ─────────────────────────────────────────────────────

# When True, drop collections whose purpose is entirely ancillary
# (Engineering, Observation Geometry, Calibration, Navigation). Applies only
# with --years collection or --targets-from collection, the modes that read
# collections.
SCIENCE_ONLY = True

# Where the table's years come from: each bundle's own Time_Coordinates, or
# the Time_Coordinates of the instrument's collections inside each bundle.
YEARS_FROM_BUNDLE = "bundle"
YEARS_FROM_COLLECTION = "collection"

# Which targets' tables an instrument appears on: those its bundle lists, or
# those its science collections inside the bundle list.
TARGETS_FROM_BUNDLE = "bundle"
TARGETS_FROM_COLLECTION = "collection"

# What a bundle or collection naming no instrument becomes: an unsorted
# "(no instrument)" entry that adds no rows, or an entry named after its
# observing system (or investigation), sorted by pairs on its telescope or
# facility LID.
NO_INSTRUMENT_DROP = "drop"
NO_INSTRUMENT_OBSERVATORY = "observatory"


# ── Abbreviation helper ────────────────────────────────────────────────────────

def _probe_variant_tag(name, used_acronym):
    """Returns another parenthetical qualifier in name, if one exists.

    Distinguishes same-mission variants of one instrument that aren't a
    mission difference at all - e.g. Pioneer Venus's Day/Night/North probes,
    each carrying its own parenthetical qualifier alongside the acronym.

    Args:
        name (string): instrument display name.
        used_acronym (string): the acronym abbreviate() already used from
              name, so it isn't picked again here.

    Returns (string or None):
        an uppercase tag from the first other parenthetical, or None.
    """
    for paren_content in re.findall(r'\(([^)]+)\)', name):
        if paren_content.upper() != used_acronym:
            return to_initials(paren_content)[:8]
    return None


def _mission_tag(name):
    """Returns the spacecraft/mission abbreviation already written into name, if any.

    Many PDS3-era instrument names end with "... for <spacecraft>" (e.g.
    "Low Energy Charged Particle (LECP) for VG2") - this pulls that tag
    for instruments whose acronym alone collides across missions.

    Args:
        name (string): instrument display name.

    Returns (string or None):
        an uppercase tag from the text after "for ", or None.
    """
    for_idx = name.lower().rfind(" for ")
    if for_idx == -1:
        return None
    return to_initials(name[for_idx + 5:].strip())[:8]


def _leading_words_tags(name):
    """Returns progressively wider leading-word tags for name, narrowest first.

    Last-resort distinguisher when neither _probe_variant_tag() nor
    _mission_tag() applies - e.g. "RSS" collides across ~15 missions whose
    names never say "for <spacecraft>" (Viking, Magellan, Cassini, MRO...).
    A single leading word is often shared by every instrument in a mission
    family (e.g. all "Mars Exploration Rover Opportunity/Spirit ..." start
    with "Mars"), so each width is tried in turn until one is specific
    enough not to collide too.

    Args:
        name (string): instrument display name.

    Returns (list of strings):
        uppercase tags, narrowest (most likely to collide) first; empty if
        name has no words.
    """
    words = name.split()
    return [to_initials(" ".join(words[:n]))[:8] for n in range(1, min(len(words), 6) + 1)]


def _get_abbr(name, name_to_abbr, abbr_used, display_name_map=None):
    """Returns a collision-resolved abbreviation for an instrument display name.

    Assigns a new abbreviation (from abbreviate()) on first call for a name. On
    collision, appends a distinguishing tag - tried in order from
    _probe_variant_tag(), _mission_tag(), then _leading_words_tags() - rather
    than an opaque running number, so the abbreviation itself hints at what
    distinguishes the instruments (e.g. base "UVS" -> "UVS-VG1", "UVS-VG2").
    Falls back to "BASE-TAG-N" (or "BASE-N" if no tag was found at all) once
    every tag has also collided, incrementing N to guarantee uniqueness.
    Subsequent calls with the same name return the already-assigned value.
    Mutates name_to_abbr and abbr_used in place.

    Args:
        name (string): instrument display name.
        name_to_abbr (dict): running map of display name -> assigned
              abbreviation. Stays keyed by name (the raw registry name) even
              when display_name_map resolves to something else, since
              callers elsewhere look abbreviations up by that same raw name.
        abbr_used (set): all abbreviations already assigned this run.
        display_name_map (dict or None): raw name -> override display name,
              from pipeline_rules.yaml's display_name_overrides /
              instrument_type_overrides. When name has an entry here, the
              override is what actually gets passed to abbreviate() - these
              overrides exist specifically for names the registry has no
              readable display name for (e.g. "instrument.ra__phx"), and
              abbreviate() needs the readable form to produce a real
              abbreviation instead of initials of the raw LID text. None if
              the caller has no overrides to offer.

    Returns (string):
        collision-resolved abbreviation for name.
    """

    # First check if the instrument is already abbreviated, otherwise try
    # the basic abbreviation
    if name in name_to_abbr:
        return name_to_abbr[name]
    abbr_source = (display_name_map or {}).get(name, name)
    base = abbreviate(abbr_source)
    if base not in abbr_used:
        name_to_abbr[name] = base
        abbr_used.add(base)
        return base

    # If the base abbreviation is taken, try augmenting using instrument-
    # specific information
    candidates = [_probe_variant_tag(abbr_source, base), _mission_tag(abbr_source)]
    candidates += _leading_words_tags(abbr_source)
    last_tag = None
    for tag in candidates:
        if not tag:
            continue
        last_tag = tag
        attempt = f"{base}-{tag}"
        if attempt not in abbr_used:
            name_to_abbr[name] = attempt
            abbr_used.add(attempt)
            return attempt

    # If every tag collided, fall back to numeration
    prefix = f"{base}-{last_tag}" if last_tag else base
    n = 2
    candidate = f"{prefix}-{n}"
    while candidate in abbr_used:
        n += 1
        candidate = f"{prefix}-{n}"
    name_to_abbr[name] = candidate
    abbr_used.add(candidate)
    return candidate


# ── Table data builder ─────────────────────────────────────────────────────────

def _instrument_attrs(name, fields, wavelength_range=None):
    """Builds a make_attrs() dict from one bundle's or collection's raw field dict.

    Used for technique matching. Every call site building a technique-matching
    attrs dict needs the same set of fields. Routing them all
    through one builder means a new field only needs to be added here -
    separate call sites can silently drift out of sync (one gets a new field
    like domain/investigation, another doesn't, and it keeps matching
    against an empty list with no error).

    Args:
        name (string): instrument display name (becomes attrs["title"]).
        fields (dict): one bundle's or collection's field dict (a value from
              a by_instrument[name] dict).
        wavelength_range (list of strings or None): override for
              fields["wavelength_range"], e.g. an agreed-upon
              multi-instrument fallback value; falls back to
              fields.get("wavelength_range") if omitted.

    Returns (dict):
        a make_attrs() attrs dict.
    """
    return make_attrs(
        title=name,
        discipline=fields.get("discipline") or [],
        facet1=fields.get("facet1") or [],
        facet2=fields.get("facet2") or [],
        wavelength_range=(
            (fields.get("wavelength_range") or []) if wavelength_range is None else wavelength_range
        ),
        description=fields.get("description") or "",
        domain=fields.get("domain") or [],
        investigation=fields.get("investigation") or [],
    )


TableData = namedtuple("TableData", [
    "section_tables", "name_to_abbr", "abbr_to_missions", "abbr_to_fullname",
    "abbr_to_mission_type", "abbr_to_landing_page", "ground_based_abbrs",
    "abbr_to_display_name", "category_spans", "gb_section_tables",
    "gb_category_spans", "category_collection_counts", "gb_category_collection_counts",
])


def _prune_empty_sections(section_tables):
    """Removes rows with no year data, then any section left with no rows.

    Operates on a {section: {category: {year: set}}} structure - shared by
    section_tables and gb_section_tables, which both need this same cleanup
    after ground-based abbreviations are removed from one or the other.
    Mutates section_tables in place.

    Args:
        section_tables (dict): {section_name: {category: {year_str: set}}}.
    """
    for table in section_tables.values():
        empty_rows = [row for row, year_sets in list(table.items())
                      if all(not year_abbrs for year_abbrs in year_sets.values())]
        for row in empty_rows:
            del table[row]
    for section in [name for name, table in list(section_tables.items()) if not table]:
        del section_tables[section]


def _filter_categories(data, keep_categories, keep_abbr):
    """Keeps keep_categories' rows, and each row's abbrs matching keep_abbr.

    Drops any category left with no abbrs after filtering.
    """
    filtered = {
        cat: {abbr: value for abbr, value in abbrs.items() if keep_abbr(abbr)}
        for cat, abbrs in data.items()
        if cat in keep_categories
    }
    return {cat: abbrs for cat, abbrs in filtered.items() if abbrs}


def _split_by_ground_based(data, ground_based_abbrs, main_categories, gb_categories):
    """Splits a {category: {abbr: value}} dict into (main, ground_based) halves.

    Shared by category_spans and category_collection_counts, which both need
    the same partition: each half keeps only its own abbrs (by
    ground_based_abbrs membership) and its own surviving categories
    (main_categories/gb_categories), then drops any category left empty.

    Args:
        data (dict): {category: {abbr: value}}; not mutated.
        ground_based_abbrs (set): abbrs whose dominant mission_type is
              "ground_based".
        main_categories (set): categories to keep in the main half.
        gb_categories (set): categories to keep in the ground-based half.

    Returns (tuple):
        (main, ground_based), each the same shape as data.
    """
    main = _filter_categories(data, main_categories, lambda abbr: abbr not in ground_based_abbrs)
    gb = _filter_categories(data, gb_categories, lambda abbr: abbr in ground_based_abbrs)
    return main, gb


def build_table_data(by_instrument, planet=None, investigation_cache=None):
    """Builds all data structures needed by the output writers from by_instrument.

    Works entirely from the already-fetched by_instrument dict, so no second
    registry query is needed.

    Each entry's categories are its "science_sorting_labels" - the labels its
    bundle's sorting pairs give that instrument - and its year is the entry's
    own start year. An entry is a bundle, or with --years collection, one of
    the instrument's collections inside a bundle. A label that isn't a
    category in pipeline_rules.yaml is skipped with a printed warning.

    Args:
        by_instrument (dict): instrument display name -> a dict of
              {entry_title: fields_dict} as built in _process_target(), where
              each fields_dict carries a "science_sorting_labels" list. Each
              instrument LID maps to exactly one display name via
              build_instrument_name_cache(), so a given LID appears under at
              most one key. Two LIDs that resolve to the same display name
              share an entry, which is correct (same instrument).
        planet (string or None): optional target planet name (e.g.
              "Jupiter") passed through to mission type lookup so
              planet-scoped entries in pipeline_rules.yaml take precedence
              over unscoped ones.
        investigation_cache (dict or None): from build_investigation_cache().
              Used only to enforce that every instrument resolves to exactly
              one real mission (see the "more than one mission" ValueError
              raised below); pass None only in contexts (e.g. tests) that
              don't need that check.

    Raises:
        ValueError: if, after instrument_type_overrides' optional
              "investigation" overrides are applied, some instrument's
              entries (bundles or collections) still name more than one real mission (e.g. a
              collection that's a joint product built from several
              spacecrafts' data, but - like every current PDS4 collection -
              records only one investigation on itself). Add an
              "investigation" override for that instrument's exact name
              under instrument_type_overrides in pipeline_rules.yaml.

    Returns (TableData):
          section_tables (dict): section name -> category label -> year
              string -> set of abbreviations. Ground-based abbreviations are
              removed before returning; they appear only in the
              ground-based listing at the bottom of the output.
          name_to_abbr (dict): instrument display name -> its
              collision-resolved abbreviation (e.g. "Near Infrared Mapping
              Spectrometer (NIMS)" -> "NIMS").
          abbr_to_missions (dict): abbreviation -> set of PDS investigation
              names associated with that instrument. Always names for a
              single real mission (e.g. "Galileo", "GALILEO ORBITER" are both
              Galileo) - see the "more than one mission" ValueError above.
          abbr_to_fullname (dict): abbreviation -> instrument display name.
              Inverse of name_to_abbr.
          abbr_to_mission_type (dict): abbreviation -> dominant mission type
              ("orbiter", "flyby", "probe", "ground_based", etc.).
          abbr_to_landing_page (dict): abbreviation -> URL of the
              instrument's mission landing page, or None when
              pipeline_rules.yaml defines none.
          ground_based_abbrs (set): abbreviations whose dominant mission
              type is "ground_based"; excluded from section_tables and
              listed separately in the table footer.
          abbr_to_display_name (dict): abbreviation -> overridden display
              name, for instruments with a display_name entry in
              pipeline_rules.yaml.
          category_spans (dict): category label -> abbreviation ->
              {"start": int year, "end": int year or None, "ongoing":
              bool}, built from every dated entry (bundle or collection) contributing to that
              category (start year in section_tables comes from the
              earliest; "end" is the latest real stop year seen; "ongoing"
              is set instead of "end" when any contributing collection's
              stop year is later than CURRENT_YEAR - a real observation
              can't have a future stop date, so PDS's "still active, no end
              date yet" placeholder years (year 3000 for some instruments,
              other far-future years like 2030 for others - not a single
              fixed sentinel) are all caught the same way. Entries with
              no start date ("No Date" in section_tables) don't contribute
              here; entries where end == start (or is None) aren't included
              since there's nothing to draw a line to. Used by the pretty
              table's start/end-pill timeline; the basic/diagnostic tables
              ignore it.
          gb_section_tables (dict): same shape as section_tables,
              ground-based abbreviations only, pruned independently - only
              categories with real ground-based data appear here. Shares
              section_tables' year columns (both come from the same
              collect_years() call downstream) but not its row set.
          gb_category_spans (dict): same shape as category_spans,
              ground-based abbreviations only, for gb_section_tables' own
              timeline.
          category_collection_counts (dict): category label -> abbreviation
              -> count of entries (bundles, or collections with --years
              collection) contributing to that category for that
              abbreviation (dated or not) - e.g. an instrument with 10
              UV entries and 1 Atmospheric Composition entry shows
              "10" on its UV pill and "1" on its Atmospheric Composition
              pill, not its instrument-wide total. Used for the pretty
              table's chip count badge; the basic/diagnostic tables ignore
              it.
          gb_category_collection_counts (dict): same shape as
              category_collection_counts, ground-based abbreviations only,
              for gb_section_tables' own chips.

        Empty rows (all year cells empty) and sections that become entirely
        empty after ground-based removal are pruned before returning.
    """
    section_tables = {
        section: defaultdict(lambda: defaultdict(set))
        for section in SECTION_ORDER
    }

    name_to_abbr     = {}
    abbr_used        = set()
    abbr_to_missions = defaultdict(set)
    category_spans = defaultdict(dict)
    category_collection_counts = defaultdict(lambda: defaultdict(int))

    # Built before the loop below, not after, so _get_abbr() can use it to
    # resolve a name too - not just relabel one for display afterward.
    display_name_map = {
        name: entry["display_name"]
        for entry in INSTRUMENT_TYPE_OVERRIDES
        for name in entry.get("names", [])
        if "display_name" in entry
    }
    display_name_map.update(DISPLAY_NAME_OVERRIDES_BY_NAME)

    # dict: unknown label -> set of instrument names carrying it. A label not in
    # pipeline_rules.yaml's sections has no row to go in, so it's reported
    # rather than silently dropped.
    unknown_labels = defaultdict(set)

    for inst_name, entries in by_instrument.items():
        abbr = _get_abbr(inst_name, name_to_abbr, abbr_used, display_name_map)

        for fields in entries.values():
            year = "No Date" if fields["start"] is None else fields["start"][:4]

            for inv in (fields.get("investigation") or []):
                abbr_to_missions[abbr].add(inv)

            cats_hit = set()
            for label in fields.get("science_sorting_labels") or []:
                section_name = SECTION_OF_CATEGORY.get(label)
                if section_name is None:
                    unknown_labels[label].add(inst_name)
                    continue
                section_tables[section_name][label][year].add(abbr)
                cats_hit.add(label)

            # Counted per category this entry actually hit, not once per instrument
            # - a 10-UV/1-Atmospheric-Composition instrument shows "10" and "1" on their
            # own pills, not a combined total. Counted even for "No Date" rows.
            for cat in cats_hit:
                category_collection_counts[cat][abbr] += 1

            if year != "No Date" and cats_hit:
                start_year = int(year)
                stop_raw  = fields.get("stop")
                stop_year = int(stop_raw[:4]) if stop_raw else None
                ongoing_now = stop_year is not None and stop_year > CURRENT_YEAR
                for cat in cats_hit:
                    span = category_spans[cat].setdefault(
                        abbr, {"start": start_year, "end": None, "ongoing": False}
                    )
                    span["start"] = min(span["start"], start_year)
                    if ongoing_now:
                        span["ongoing"] = True
                    elif stop_year is not None:
                        span["end"] = stop_year if span["end"] is None else max(span["end"], stop_year)

    for label, inst_names in sorted(unknown_labels.items()):
        print(f"  WARNING: science sorting label {label!r} is not a category in "
              f"pipeline_rules.yaml - skipped for {', '.join(sorted(inst_names))}")

    # Drop spans with nothing to draw a line to: a single dated collection
    # (or several agreeing on one year) with no real end year past its own
    # start year, and not ongoing.
    category_spans = {
        cat: {
            abbr: span for abbr, span in abbrs.items()
            if span["ongoing"] or (span["end"] is not None and span["end"] > span["start"])
        }
        for cat, abbrs in category_spans.items()
    }
    category_spans = {cat: abbrs for cat, abbrs in category_spans.items() if abbrs}

    abbr_to_fullname = {abbr: name for name, abbr in name_to_abbr.items()}

    # instrument_type_overrides entries may carry an "investigation" that replaces
    # an instrument's whole mission set - fixes a joint-product collection wrongly
    # naming one spacecraft's mission for every instrument that shares it.
    inst_investigation_override = {name: entry["investigation"]
                                   for entry in INSTRUMENT_TYPE_OVERRIDES
                                   for name in entry.get("names", [])
                                   if "investigation" in entry}
    for name, abbr in name_to_abbr.items():
        if name in inst_investigation_override:
            abbr_to_missions[abbr] = {inst_investigation_override[name]}

    if investigation_cache is not None:
        for abbr, missions in abbr_to_missions.items():
            identities = {investigation_identity(mission, investigation_cache) for mission in missions}
            if len(identities) > 1:
                raise ValueError(
                    f"{abbr_to_fullname.get(abbr, abbr)!r} (abbr {abbr!r}) resolves to more "
                    f"than one mission: {sorted(missions)!r}. Add an \"investigation\" "
                    f"override for this instrument's exact name under "
                    f"instrument_type_overrides in pipeline_rules.yaml."
                )

    abbr_to_mission_type = {
        abbr: abbr_mission_type(missions, planet)
        for abbr, missions in abbr_to_missions.items()
    }
    inst_override = {}
    for entry in INSTRUMENT_TYPE_OVERRIDES:
        if "type" not in entry:
            continue
        for name in entry.get("names", []):
            inst_override[name] = entry["type"]

    # instrument_type_overrides entries may carry a "url" to send one specific
    # instrument to a different landing page than its investigation resolves to by
    # default - e.g. Pioneer Venus probe instruments away from the Orbiter's page.
    inst_url_override = {}
    for entry in INSTRUMENT_TYPE_OVERRIDES:
        if "url" not in entry:
            continue
        for name in entry.get("names", []):
            inst_url_override[name] = entry["url"]

    for abbr, fullname in abbr_to_fullname.items():
        if fullname in inst_override:
            abbr_to_mission_type[abbr] = inst_override[fullname]

    # display_name_map already fed abbreviate() via _get_abbr(), but abbr_to_fullname
    # still holds the raw registry name (name_to_abbr stays keyed by it - see
    # _get_abbr()'s docstring), so it needs its own pass here to fix up the text.
    abbr_to_display_name = {}
    for abbr, fullname in abbr_to_fullname.items():
        if fullname in display_name_map:
            abbr_to_display_name[abbr] = display_name_map[fullname]
            abbr_to_fullname[abbr] = display_name_map[fullname]
    abbr_to_landing_page = {
        abbr: (landing_page_for_mission(next(iter(missions))) if missions else None)
        for abbr, missions in abbr_to_missions.items()
    }
    for name, abbr in name_to_abbr.items():
        if name in inst_url_override:
            abbr_to_landing_page[abbr] = inst_url_override[name]

    # Ground-based table construction starts here.
    ground_based_abbrs = {
        abbr for abbr, mtype in abbr_to_mission_type.items()
        if mtype == "ground_based"
    }

    # Ground-based instruments get their own parallel table (gb_section_tables),
    # captured here as the mirror of what's stripped from section_tables below.
    # Each is then pruned independently - only the year columns are shared.
    gb_section_tables = {
        section: {
            cat: {yr: (abbrs & ground_based_abbrs) for yr, abbrs in year_map.items()}
            for cat, year_map in table.items()
        }
        for section, table in section_tables.items()
    }

    # Remove ground-based instruments from the main section tables, then prune
    # whatever's left empty in both tables (shared helper - same shape).
    for table in section_tables.values():
        for row_data in table.values():
            for yr_set in row_data.values():
                yr_set -= ground_based_abbrs
    _prune_empty_sections(section_tables)
    _prune_empty_sections(gb_section_tables)

    # category_spans/category_collection_counts pruned to each table's own
    # surviving categories and split into main/ground-based (shared helper -
    # same shape for both).
    surviving_categories = {cat for table in section_tables.values() for cat in table}
    gb_surviving_categories = {cat for table in gb_section_tables.values() for cat in table}
    category_spans, gb_category_spans = _split_by_ground_based(
        category_spans, ground_based_abbrs, surviving_categories, gb_surviving_categories
    )
    category_collection_counts, gb_category_collection_counts = _split_by_ground_based(
        category_collection_counts, ground_based_abbrs, surviving_categories, gb_surviving_categories
    )

    return TableData(
        section_tables=section_tables, name_to_abbr=name_to_abbr,
        abbr_to_missions=abbr_to_missions, abbr_to_fullname=abbr_to_fullname,
        abbr_to_mission_type=abbr_to_mission_type, abbr_to_landing_page=abbr_to_landing_page,
        ground_based_abbrs=ground_based_abbrs, abbr_to_display_name=abbr_to_display_name,
        category_spans=category_spans, gb_section_tables=gb_section_tables,
        gb_category_spans=gb_category_spans,
        category_collection_counts=category_collection_counts,
        gb_category_collection_counts=gb_category_collection_counts,
    )


# ── Registry client timeout ─────────────────────────────────────────────────────

_REGISTRY_REQUEST_TIMEOUT_SECS = 60


def _apply_registry_timeout(client, timeout_secs=_REGISTRY_REQUEST_TIMEOUT_SECS):
    """Forces a fixed timeout onto every HTTP request one PDSRegistryClient makes.

    peppi's generated REST client accepts a per-call _request_timeout, but
    peppi itself never sets one, so every call defaults to None - which
    urllib3 treats as no timeout at all, not a sane fallback, so a single
    stalled connection can hang forever. Patches this client instance's own
    rest_client.request, not the shared RESTClientObject class, so the fix
    stays scoped to this one client instead of every client in the process.

    Args:
        client (peppi PDSRegistryClient): patched in place.
        timeout_secs (number): seconds forced onto every request through
              this client, regardless of what's passed in.
    """
    original_request = client.api_client.rest_client.request

    def _request(method, url, headers=None, body=None, post_params=None, _request_timeout=None):
        return original_request(method, url, headers=headers, body=body,
                                post_params=post_params, _request_timeout=timeout_secs)

    client.api_client.rest_client.request = _request


# ── Bundle and collection entries ──────────────────────────────────────────────

def _observatory_entry_name(fields):
    """Returns the name a product naming no instrument is listed under.

    The product's observing system (its observatory and telescope, e.g. "NASA
    InfraRed Telescope Facility (IRTF)"), or failing that its investigation,
    followed by "(no instrument)" so the chip's tooltip says what it is.

    Args:
        fields (dict): from bundle_fields() or collection_fields().

    Returns (string):
        e.g. "NASA InfraRed Telescope Facility (IRTF) (no instrument)", or
        plain "(no instrument)" when the product names neither.
    """
    for names in (fields.get("observing_system"), fields.get("investigation")):
        if names:
            return f"{names[0]} (no instrument)"
    return "(no instrument)"


def _add_instrument_entries(by_instrument, title, fields, inst_lids, labels,
                            inst_name_cache, spacecraft_ids, facility_ids,
                            pairs_add_instruments=True, no_instrument=NO_INSTRUMENT_DROP):
    """Adds one bundle's or collection's entry under each instrument it names.

    Each instrument's copy of fields gets "science_sorting_labels": the labels
    the sorting pairs give that instrument (empty if the pairs don't name
    it). With pairs_add_instruments, an instrument named only in the pairs,
    not in the product's own instrument references, still gets an entry - the
    pairs are what sort it.

    A pair whose LID is one of the product's telescope or facility LIDs (see
    observatory_lids()) isn't an instrument. It sorts a product that names no
    instrument at all; with no_instrument=NO_INSTRUMENT_OBSERVATORY, that
    product is listed under its observing system or investigation (see
    _observatory_entry_name()) with those pairs' labels. With
    NO_INSTRUMENT_DROP it goes under "(no instrument)" with no labels, so it
    adds no rows. Mutates by_instrument in place.

    Args:
        by_instrument (dict): instrument display name -> {entry title: fields}.
        title (string): the bundle's or collection's title, used as the entry key.
        fields (dict): from bundle_fields() or collection_fields().
        inst_lids (list of strings): the product's own instrument LIDs.
        labels (dict): normalized LID -> labels, as returned by
              sorting_labels() (merged across parent bundles for a collection).
        inst_name_cache (dict): from build_instrument_name_cache().
        spacecraft_ids (set): from build_instrument_name_cache().
        facility_ids (set): from build_instrument_name_cache().
        pairs_add_instruments (bool): whether instruments named only in the
              pairs get entries too. False limits the entries to inst_lids.
        no_instrument (string): NO_INSTRUMENT_DROP or NO_INSTRUMENT_OBSERVATORY.
    """
    observatories = normalized_lids(fields.get("observatory_lids") or [], spacecraft_ids, facility_ids)
    paired_instruments = [lid for lid in labels if lid not in observatories] if pairs_add_instruments else []
    names = instrument_names_by_lid(list(inst_lids) + paired_instruments,
                                    inst_name_cache, spacecraft_ids, facility_ids)
    if not names:
        entry = dict(fields)
        if no_instrument == NO_INSTRUMENT_OBSERVATORY:
            entry["science_sorting_labels"] = []
            for lid in observatories:
                for label in labels.get(lid, []):
                    if label not in entry["science_sorting_labels"]:
                        entry["science_sorting_labels"].append(label)
            by_instrument[_observatory_entry_name(fields)][title] = entry
        else:
            entry["science_sorting_labels"] = []
            by_instrument["(no instrument)"][title] = entry
        return
    for inst_lid, inst_name in names.items():
        entry = dict(fields)
        entry["science_sorting_labels"] = list(labels.get(inst_lid, []))
        by_instrument[inst_name][title] = entry


# ── Process target ─────────────────────────────────────────────────────────────

def _process_target(target_name, context, client, node_filter, node_suffix,
                    inst_name_cache, spacecraft_ids, facility_ids, investigation_cache,
                    omitted_lower, peppi_name_overrides, diagnostic,
                    years_from=YEARS_FROM_COLLECTION, targets_from=TARGETS_FROM_COLLECTION,
                    no_instrument=NO_INSTRUMENT_DROP):
    """Queries, categorizes, and writes output for one target.

    Raises on any failure (unknown target, registry error, etc.) rather than
    handling it - main() wraps each call in try/except so one bad target in a
    multi-target run is reported and skipped instead of aborting every target
    queued after it.

    Args:
        target_name (string): upper-cased, space-normalized target as given
              on the command line (e.g. "JUPITER", "TITAN").
        context (CachedContext): build_context() result.
        client (peppi PDSRegistryClient).
        node_filter (string or None): "PDS_<NODE>" string, or None for
              --node ALL.
        node_suffix (string): output filename suffix for the active --node
              (see the module docstring's output section).
        inst_name_cache (dict): from build_instrument_name_cache().
        spacecraft_ids (set): from build_instrument_name_cache().
        facility_ids (set): from build_instrument_name_cache().
        investigation_cache (dict): from build_investigation_cache().
        omitted_lower (set): lowercased OMITTED_INSTRUMENTS names to drop.
        peppi_name_overrides (dict): target display name -> actual
              pep.Context() TARGETS attribute name, for the handful of
              bodies Peppi doesn't expose under their plain name (see
              peppi_name_overrides in main()).
        diagnostic (bool): whether to also write the diagnostic outputs.
        years_from (string): YEARS_FROM_BUNDLE to date each entry by its
              bundle's Time_Coordinates, or YEARS_FROM_COLLECTION to date it by
              each of the instrument's science collections inside the bundle.
        targets_from (string): TARGETS_FROM_BUNDLE to place an instrument on
              this target when its bundle lists the target, or
              TARGETS_FROM_COLLECTION to place it when one of its science
              collections does (the collection's parent bundle is then read
              for the pairs, whatever targets the bundle lists).
        no_instrument (string): NO_INSTRUMENT_DROP or
              NO_INSTRUMENT_OBSERVATORY - see _add_instrument_entries().
    """
    peppi_name = peppi_name_overrides.get(target_name, target_name)
    if not hasattr(context.TARGETS, peppi_name):
        raise ValueError(
            f"unknown target {target_name!r} - no attribute {peppi_name!r} found "
            f"in the PDS registry context catalog. Check the spelling, or run "
            f"with --refresh-contexts if this is a newly archived target."
        )
    body_target = getattr(context.TARGETS, peppi_name)
    category_planet_label = target_name.title()
    file_prefix = target_name.lower() + node_suffix

    # Every target's output lives in its own tables/<target>/ subdir (both
    # node variants together, e.g. tables/venus/venus_..., tables/venus/
    # venus_allnodes_...) rather than 100+ files flat in tables/.
    target_dir = os.path.join(OUTPUT_DIR, target_name.lower())
    os.makedirs(target_dir, exist_ok=True)

    # A satellite target's own label (e.g. "Titan") never matches mission_types'
    # planet-scoped entries (e.g. Cassini -> orbiter only for Saturn); PDS4 LIDs
    # encode the real parent as "satellite.<parent>.<moon>".
    lid_parts = body_target.lid.rsplit(":", 1)[-1].split(".")
    if lid_parts[0] == "satellite" and len(lid_parts) >= 2:
        mission_type_planet = lid_parts[1].title()
    else:
        mission_type_planet = category_planet_label

    target_label = category_planet_label
    print(f"{target_label} LID : {body_target.lid}")
    print(f"Node filter : {node_filter or 'ALL'}\n")

    target_query = pep.Products(client).has_target(body_target.lid)
    target_bundles = latest_versions(fetch_products(target_query.bundles(), BUNDLE_QUERY_FIELDS))

    # list of (peppi Product, fields dict): the target's science collections,
    # every node - read only when years or targets come from collections.
    all_node_collections = []
    if years_from == YEARS_FROM_COLLECTION or targets_from == TARGETS_FROM_COLLECTION:
        target_query = pep.Products(client).has_target(body_target.lid)
        for col in fetch_products(target_query.collections(), COLLECTION_QUERY_FIELDS):
            if not is_science_collection(col):
                continue
            if SCIENCE_ONLY and is_ancillary_purpose(col):
                continue
            fields = collection_fields(col, target_label)
            clamp_to_investigation_dates(fields, investigation_cache)
            all_node_collections.append((col, fields))
    target_collections = [(col, fields) for col, fields in all_node_collections
                          if not node_filter or fields["pds_node"] == node_filter]

    # Targets from collections: the bundles used are the parents of this
    # target's science collections, fetched by LID when the target's own
    # bundle query didn't return them.
    if targets_from == TARGETS_FROM_COLLECTION:
        needed = set()
        for _, fields in target_collections:
            needed |= parent_bundle_lids(fields)
        returned = {bare_lid(bundle.id) for bundle in target_bundles}
        fetched, missing = bundles_by_lid(client, needed - returned)
        target_bundles = [bundle for bundle in target_bundles if bare_lid(bundle.id) in needed] + fetched
        print(f"Parent bundles fetched by LID : {len(fetched)}")
        for lid in missing:
            print(f"  NOTE: parent bundle {lid} is not in the registry - its collections get no sorting pairs")

    interim_count = add_interim_sorting_labels(target_bundles)
    print(f"Bundles : {len(target_bundles)}  ({interim_count} given interim sorting labels)")

    by_instrument = defaultdict(dict)

    # The target's bundles (or with targets from collections, its collections)
    # cover every node regardless of --node; node_filter only decides what
    # reaches by_instrument. Tracked unconditionally so abbr_to_nodes never
    # depends on a stale separate run.
    all_nodes_by_instrument = defaultdict(set)
    if targets_from == TARGETS_FROM_COLLECTION:
        node_sources = [(col.properties, fields["pds_node"]) for col, fields in all_node_collections]
    else:
        node_sources = [(bundle.properties, scalar(bundle.properties.get(NODE_KEY))) for bundle in target_bundles]
    for props, pds_node in node_sources:
        if pds_node:
            for inst_name in instrument_components(props, inst_name_cache, spacecraft_ids, facility_ids):
                all_nodes_by_instrument[inst_name].add(pds_node)

    # dict: bundle LID -> instrument LIDs named by its science collections on
    # this target - with targets from collections, the only instruments a
    # bundle entry is made for.
    collection_instruments = defaultdict(set)
    for _, fields in target_collections:
        for bundle_lid in parent_bundle_lids(fields):
            collection_instruments[bundle_lid].update(fields["instrument_lid"] or [])

    # dict: bundle LID -> {normalized LID: labels}, and bundle LID -> fields
    # dict, for every bundle that passes node_filter - read again below when
    # dating by collection.
    labels_by_bundle = {}
    fields_by_bundle = {}

    for bundle in target_bundles:
        if node_filter and scalar(bundle.properties.get(NODE_KEY)) != node_filter:
            continue

        fields = bundle_fields(bundle, target_label)
        clamp_to_investigation_dates(fields, investigation_cache)
        labels = sorting_labels(bundle.properties, spacecraft_ids, facility_ids)
        labels_by_bundle[fields["bundle_lid"]] = labels
        fields_by_bundle[fields["bundle_lid"]] = fields

        if years_from == YEARS_FROM_BUNDLE:
            title = scalar(bundle.properties.get(TITLE_KEY)) or bundle.id
            if targets_from == TARGETS_FROM_COLLECTION:
                _add_instrument_entries(by_instrument, title, fields,
                                        sorted(collection_instruments[fields["bundle_lid"]]), labels,
                                        inst_name_cache, spacecraft_ids, facility_ids,
                                        pairs_add_instruments=False, no_instrument=no_instrument)
            else:
                _add_instrument_entries(by_instrument, title, fields, fields["instrument_lid"] or [], labels,
                                        inst_name_cache, spacecraft_ids, facility_ids,
                                        no_instrument=no_instrument)

    # Dating by collection: each of the instrument's science collections is its
    # own entry, sorted by the pairs on the bundle (or bundles) it belongs to.
    # Only the collection's own instruments get entries, so one instrument's
    # collection doesn't date another instrument named in the same bundle.
    if years_from == YEARS_FROM_COLLECTION:
        for col, fields in target_collections:
            # dict: normalized LID -> labels, merged across every parent
            # bundle read above
            labels = defaultdict(list)
            for bundle_lid in parent_bundle_lids(fields):
                for inst_lid, inst_labels in labels_by_bundle.get(bundle_lid, {}).items():
                    for label in inst_labels:
                        if label not in labels[inst_lid]:
                            labels[inst_lid].append(label)

            # A collection naming no instrument and no observatory of its own
            # is described by its parent bundle's observatory.
            entry_fields = dict(fields)
            if not entry_fields["instrument_lid"] and not entry_fields["observatory_lids"]:
                for bundle_lid in parent_bundle_lids(fields):
                    bundle = fields_by_bundle.get(bundle_lid)
                    if bundle and bundle["observatory_lids"]:
                        entry_fields["observatory_lids"] = bundle["observatory_lids"]
                        entry_fields["observing_system"] = (entry_fields["observing_system"]
                                                            or bundle["observing_system"])
                        break

            title = scalar(col.properties.get(TITLE_KEY)) or col.id
            _add_instrument_entries(by_instrument, title, entry_fields, fields["instrument_lid"] or [],
                                    dict(labels), inst_name_cache, spacecraft_ids, facility_ids,
                                    pairs_add_instruments=False, no_instrument=no_instrument)

    for inst_name in [name for name in by_instrument if name.lower() in omitted_lower]:
        del by_instrument[inst_name]

    total_entries = sum(len(entries) for entries in by_instrument.values())
    print(f"Instruments : {len(by_instrument)}")
    print(f"Entries : {total_entries} {years_from}s  (a {years_from} naming several "
          f"instruments counts once per instrument)\n")
    if not by_instrument:
        print(f"  WARNING: zero instruments found for {target_label} with node filter "
              f"'{node_filter or 'ALL'}'. This is expected for a body with no ATM-node "
              f"data (e.g. Pluto), but also exactly what a typo'd --node value produces "
              f"- double-check the spelling before treating this output as final.\n")

    table_data = build_table_data(by_instrument, planet=mission_type_planet, investigation_cache=investigation_cache)
    section_tables = table_data.section_tables
    name_to_abbr = table_data.name_to_abbr
    abbr_to_missions = table_data.abbr_to_missions
    abbr_to_fullname = table_data.abbr_to_fullname
    abbr_to_mission_type = table_data.abbr_to_mission_type
    abbr_to_landing_page = table_data.abbr_to_landing_page
    ground_based_abbrs = table_data.ground_based_abbrs
    abbr_to_display_name = table_data.abbr_to_display_name
    category_spans = table_data.category_spans
    gb_section_tables = table_data.gb_section_tables
    gb_category_spans = table_data.gb_category_spans
    category_collection_counts = table_data.category_collection_counts
    gb_category_collection_counts = table_data.gb_category_collection_counts

    # Node info: all_nodes_by_instrument was populated above from every node's
    # collections regardless of --node, so it's already this target's
    # complete node picture - no cache, no separate --node ALL run needed.
    abbr_to_nodes = {
        abbr: all_nodes_by_instrument.get(inst_name, set())
        for inst_name, abbr in name_to_abbr.items()
    }

    # Technique tags: same rule engine as the category rows, but against
    # pipeline_rules.yaml's `techniques` block (Imaging, Spectroscopy). No table
    # rows - stored on chips so the pretty table can filter without a rebuild.
    abbr_to_techniques        = {}
    abbr_to_technique_counts  = {}
    for inst_name, cols in by_instrument.items():
        abbr = name_to_abbr.get(inst_name)
        if not abbr:
            continue
        techs = set()
        tech_counts = {}
        for fields in cols.values():
            col_attrs = _instrument_attrs(inst_name, fields)
            col_techs = technique_categories(col_attrs)
            techs.update(col_techs)
            for tech in col_techs:
                tech_counts[tech] = tech_counts.get(tech, 0) + 1
        if techs:
            abbr_to_techniques[abbr] = techs
            abbr_to_technique_counts[abbr] = tech_counts

    # Archive status for chip decorations: flags instruments with any non-archived
    # bundle (or collection) so staff can spot in-progress work at a glance - flat
    # per-abbreviation, unlike the category-scoped count badge.
    abbr_nonarchived = set()
    for inst_name, cols in by_instrument.items():
        abbr = name_to_abbr.get(inst_name)
        if not abbr:
            continue
        if any((fields.get("archive_status") or "").lower() not in ("archived", "")
               for fields in cols.values()):
            abbr_nonarchived.add(abbr)

    pretty_path = os.path.join(target_dir,
                               f"{file_prefix}_integrated_archive_pretty.html")
    write_pretty_html_table(
        section_tables, name_to_abbr, abbr_to_missions, abbr_to_fullname,
        abbr_to_mission_type, abbr_to_landing_page, ground_based_abbrs,
        abbr_to_nodes, abbr_to_techniques,
        category_collection_counts, abbr_nonarchived,
        pretty_path, target_label,
        abbr_to_display_name=abbr_to_display_name,
        investigation_cache=investigation_cache,
        abbr_to_technique_counts=abbr_to_technique_counts,
        category_spans=category_spans,
        gb_section_tables=gb_section_tables,
        gb_category_spans=gb_category_spans,
        gb_category_collection_counts=gb_category_collection_counts,
        count_unit=years_from,
    )
    basic_path = os.path.join(target_dir,
                               f"{file_prefix}_integrated_archive_basic.html")
    write_basic_html_table(
        section_tables, name_to_abbr, abbr_to_missions, abbr_to_fullname,
        abbr_to_landing_page, ground_based_abbrs,
        basic_path, target_label,
        abbr_to_display_name=abbr_to_display_name,
    )

    if diagnostic:
        abbr_to_collections = {}
        for inst_name, cols in by_instrument.items():
            abbr = name_to_abbr.get(inst_name)
            if not abbr:
                continue
            inst_lid = ""
            for fields in cols.values():
                lids = fields.get("instrument_lid") or []
                if lids:
                    inst_lid = lids[0]
                    break
            abbr_to_collections[abbr] = {
                "name": inst_name,
                "lid":  inst_lid,
                "collections": [
                    {
                        "title":          title,
                        "lid":            fields.get("collection_lid") or fields.get("bundle_lid") or "",
                        "pds_node":       fields.get("pds_node") or "",
                        "mission":        ", ".join(fields.get("investigation") or []),
                        "start":          fields.get("start") or "",
                        "stop":           fields.get("stop") or "",
                        "discipline":     ", ".join(fields.get("discipline") or []),
                        "facet1":         ", ".join(fields.get("facet1") or []),
                        "processing":     ", ".join(fields.get("processing_level") or []),
                        "archive_status": fields.get("archive_status") or "",
                        "description":    fields.get("description") or "",
                    }
                    for title, fields in sorted(cols.items())
                ],
            }
        write_diagnostic_html_table(
            section_tables, name_to_abbr, abbr_to_missions, abbr_to_fullname,
            ground_based_abbrs, abbr_to_nodes,
            os.path.join(target_dir, f"{file_prefix}_diagnostic_table.html"),
            target_label,
            abbr_to_collections=abbr_to_collections,
            abbr_to_display_name=abbr_to_display_name,
        )


# ── Pipeline orchestration ──────────────────────────────────────────────────────

def run_pipeline(targets, diagnostic=False, node="ATM", refresh_contexts=False,
                 years_from=YEARS_FROM_COLLECTION, targets_from=TARGETS_FROM_COLLECTION,
                 no_instrument=NO_INSTRUMENT_DROP):
    """Runs the full pipeline for one or more targets.

    Holds every step main() used to run inline, taking plain arguments
    instead of an argparse Namespace so it can be called directly from
    Python (see setup/example_build_usage.py) without going through
    sys.argv. Never calls sys.exit() - a per-target failure is reported
    by returning it in failed_targets, leaving it to the caller (main(),
    or a script calling this directly) to decide what a failure means for
    its own process.

    For each target:
      1. Queries the PDS registry for the target's bundles (and, with
         years_from or targets_from set to collection, its science
         collections too, plus any of their parent bundles the target's
         bundle query didn't return).
      2. Groups them by instrument and reads each bundle's science sorting
         pairs for the categories.
      3. Builds category × year tables and writes the pretty/basic HTML output
         (plus the diagnostic HTML table, with diagnostic=True).

    Args:
        targets (list of strings): targets to process, e.g. ["JUPITER",
              "VENUS"] - any name the PDS registry recognizes as a context
              target.
        diagnostic (bool): also write a plain interactive HTML diagnostic
              table.
        node (string): PDS node short name to restrict collections to, e.g.
              "ATM", "PPI", "GEO", "NAIF" (default: "ATM"). Pass "ALL" to
              include every node. Case-insensitive.
        refresh_contexts (bool): force a live re-fetch of the context-product
              catalog (instruments, spacecraft, etc.) instead of using the
              on-disk cache in pds_tars/cache/context_cache.json.
              This re-downloads the full catalog and can take several minutes.
        years_from (string): YEARS_FROM_COLLECTION (default) or
              YEARS_FROM_BUNDLE - see _process_target().
        targets_from (string): TARGETS_FROM_COLLECTION (default) or
              TARGETS_FROM_BUNDLE - see _process_target().
        no_instrument (string): NO_INSTRUMENT_DROP (default) or
              NO_INSTRUMENT_OBSERVATORY - see _add_instrument_entries().

    Returns (list of strings):
        target names (from targets) that failed, in the order queued;
        empty if every target succeeded.
    """
    node_filter = None if node.upper() == "ALL" else f"PDS_{node.upper()}"

    # Output file prefix suffix based on node: ATM (default) -> no suffix,
    # ALL -> "_allnodes", other nodes -> "_<node>" (e.g. "_ppi").
    if node_filter is None:
        node_suffix = "_allnodes"
    elif node_filter == "PDS_ATM":
        node_suffix = ""
    else:
        node_suffix = "_" + node.lower()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    client  = pep.PDSRegistryClient()
    _apply_registry_timeout(client)
    context = build_context(force_refresh=refresh_contexts)

    print("Building instrument name cache…")
    inst_name_cache, spacecraft_ids, facility_ids = build_instrument_name_cache(client, force_refresh=refresh_contexts)
    print(f"Cached {len(inst_name_cache)} instrument LIDs")

    print("Building investigation cache…")
    investigation_cache = build_investigation_cache(client, force_refresh=refresh_contexts)
    print(f"Cached {len(investigation_cache)} investigations\n")

    omitted_lower = {name.lower() for name in OMITTED_INSTRUMENTS}

    # Some bodies have non-plain Peppi attribute names.
    peppi_name_overrides = {
        "PLUTO": "(134340)_PLUTO",
    }

    failed_targets = []
    for target_name in targets:
        try:
            _process_target(
                target_name, context, client, node_filter, node_suffix,
                inst_name_cache, spacecraft_ids, facility_ids, investigation_cache,
                omitted_lower, peppi_name_overrides,
                diagnostic=diagnostic, years_from=years_from,
                targets_from=targets_from, no_instrument=no_instrument,
            )
        except Exception as exc:
            print(f"[SKIPPED] {target_name}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            print(file=sys.stderr)
            failed_targets.append(target_name)
            continue

    return failed_targets


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    """Parses command-line arguments, then hands off to run_pipeline().

    A non-zero exit matters even though run_pipeline() already isolates one
    target's failure from the rest - a calling script checking $? should see
    the failure, not assume success just because main() itself didn't raise.
    """
    parser = argparse.ArgumentParser(
        description="Build PDS ATM integrated archive tables.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="TARGET",
        help="Targets to process (e.g. JUPITER VENUS IO TITAN) - any name the "
             "PDS registry recognizes as a context target. Defaults to JUPITER.",
    )
    parser.add_argument(
        "--diagnostic",
        action="store_true",
        help="Also write a plain interactive HTML diagnostic table.",
    )
    parser.add_argument(
        "--node",
        default="ATM",
        help='PDS node short name to restrict collections to, e.g. ATM, PPI, GEO, NAIF '
             '(default: ATM). Pass ALL to include every node.',
    )
    parser.add_argument(
        "--years",
        choices=[YEARS_FROM_BUNDLE, YEARS_FROM_COLLECTION],
        default=YEARS_FROM_COLLECTION,
        help="Where the table's years come from: the time ranges of each "
             "instrument's science collections inside the bundle (collection, "
             "the default), or each bundle's own time range (bundle). "
             "Categories come from the bundle's science sorting pairs either way.",
    )
    parser.add_argument(
        "--targets-from",
        choices=[TARGETS_FROM_BUNDLE, TARGETS_FROM_COLLECTION],
        default=TARGETS_FROM_COLLECTION,
        help="Which targets' tables an instrument appears on: those its "
             "science collections inside the bundle list (collection, the "
             "default), or those its bundle lists (bundle). With collection, "
             "each collection's parent bundle is read for the sorting pairs "
             "even when that bundle doesn't list the target.",
    )
    parser.add_argument(
        "--no-instrument",
        choices=[NO_INSTRUMENT_DROP, NO_INSTRUMENT_OBSERVATORY],
        default=NO_INSTRUMENT_DROP,
        help="What a bundle or collection naming no instrument becomes: left "
             "out of the table (drop, the default), or listed under its "
             "observing system (observatory/telescope), or its investigation "
             "if it names none, sorted by sorting pairs on its telescope or "
             "facility LID (observatory).",
    )
    parser.add_argument(
        "--refresh-contexts",
        action="store_true",
        help="Force a live re-fetch of the context-product catalog (instruments, "
             "spacecraft, etc.) instead of using the on-disk cache in "
             "pds_tars/cache/context_cache.json. This re-downloads the full "
             "catalog and can take several minutes.",
    )
    args = parser.parse_args()
    targets = [target.upper().replace(" ", "_") for target in args.targets] if args.targets else ["JUPITER"]

    failed_targets = run_pipeline(
        targets, diagnostic=args.diagnostic, node=args.node, refresh_contexts=args.refresh_contexts,
        years_from=args.years, targets_from=args.targets_from, no_instrument=args.no_instrument,
    )

    if failed_targets:
        print(f"Failed: {', '.join(failed_targets)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
