"""
schema.py - Loads pipeline_rules.yaml and parses science sorting labels.

Sections and categories come from the science sorting pairs on bundle labels,
as "Section:Category" values - the sorting dictionary is their master list.
pipeline_rules.yaml only gives their display order (display_order), plus
mission types, landing pages and per-instrument overrides.
"""

import os
import yaml

_RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_rules.yaml")

# The section whose labels are technique tags (e.g. "Technique:Imaging"),
# stored on an instrument's chips for filtering instead of making table rows.
TECHNIQUE_SECTION = "Technique"

# Section for a label that names no section (no ":"), so it still shows.
UNSECTIONED = "Other"


def _load():
    """Parses pipeline_rules.yaml and returns its top-level data structures.

    Returns (tuple):
        display_order (dict): section name -> row labels, in display order
        mission_types (list): investigation name -> type mappings
        mission_landing_pages (list): investigation name -> URL mappings
        mission_type_display (list): type vocabulary with display metadata
        omitted_instruments (list): instrument display names to exclude
        instrument_type_overrides (list): exact instrument name -> type overrides
        display_name_overrides (list): exact instrument name -> display name
        collection_date_overrides (list): exact collection LID -> corrected start/stop
    """
    with open(_RULES_FILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return (data.get("display_order") or {},
            data["mission_types"],
            data["mission_landing_pages"],
            data["mission_type_display"],
            data.get("omitted_instruments", []),
            data.get("instrument_type_overrides", []),
            data.get("display_name_overrides", []),
            data.get("collection_date_overrides", []))


(DISPLAY_ORDER,
 MISSION_TYPES, MISSION_LANDING_PAGES, MISSION_TYPE_DISPLAY,
 OMITTED_INSTRUMENTS, INSTRUMENT_TYPE_OVERRIDES,
 _DISPLAY_NAME_OVERRIDES,
 _COLLECTION_DATE_OVERRIDES) = _load()

# Rekeyed once here (each entry's own lookup key is already known when the
# YAML list is parsed) so every caller gets an O(1) dict instead of each
# repeating the same rekey at its own call site.
DISPLAY_NAME_OVERRIDES_BY_NAME = {
    entry["name"]: entry["display_name"] for entry in _DISPLAY_NAME_OVERRIDES
}

COLLECTION_DATE_OVERRIDES_BY_LID = {
    entry["collection_lid"]: entry for entry in _COLLECTION_DATE_OVERRIDES
}


def split_label(label):
    """Splits one science sorting label into its section and category.

    Args:
        label (string): e.g. "Atmosphere:Clouds" or "Technique:Imaging".
              Whitespace around either part is ignored.

    Returns (tuple):
        (section, category). A label with no ":" has no section; it comes
        back as (UNSECTIONED, label) so it still appears somewhere.
    """
    section, sep, category = label.partition(":")
    if not sep:
        return UNSECTIONED, label.strip()
    return section.strip(), category.strip()


def sorted_sections(observed):
    """Returns observed section names in display order, extras appended sorted.

    Args:
        observed (iterable of strings): section names present in the data.

    Returns (list of strings):
        the display_order sections that appear in observed, then any others
        alphabetically.
    """
    return sorted_rows(set(observed), list(DISPLAY_ORDER))


def rows_in_order(section_name):
    """Returns display_order's row list for one section (empty if unlisted).

    Args:
        section_name (string): a section name.

    Returns (list of strings):
        that section's rows from display_order, or [] if it isn't listed.
    """
    return DISPLAY_ORDER.get(section_name) or []


def sorted_rows(observed, canonical_order):
    """Returns observed category rows in canonical order, extras appended sorted.

    Args:
        observed (set of strings): category names actually present in the data.
        canonical_order (list of strings): the preferred display order from
              pipeline_rules.yaml's display_order.

    Returns (list of strings):
        canonical rows that appear in observed, followed by any observed
        rows not in canonical_order, sorted alphabetically.
    """
    ordered = [row for row in canonical_order if row in observed]
    # rows the pairs use that display_order doesn't list land here, sorted
    ordered += sorted(observed - set(canonical_order))
    return ordered


def collect_years(*tables):
    """Returns a sorted list of all observation-start years present across tables.

    Args:
        *tables (dicts): any number of category tables of the form
              {category_name: {year_str: set(abbr)}}.

    Returns (list of strings):
        sorted unique year strings found in any table.
    """
    all_years = set()
    for tbl in tables:
        for row_data in tbl.values():
            all_years.update(row_data)
    has_no_date = "No Date" in all_years
    real_years = sorted(all_years - {"No Date"})
    return (["No Date"] + real_years) if has_no_date else real_years
