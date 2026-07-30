"""
schema.py - Loads pipeline_rules.yaml and exposes the category vocabulary.

Single source of truth for section names, row labels, detection rules,
technique tags, and table-ordering utilities.  All other pipeline modules
import from here.
"""

import os
import yaml

_RULES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline_rules.yaml")


def _load():
    """Parses pipeline_rules.yaml and returns its top-level data structures.

    Returns (tuple):
        sections (dict): section name -> ordered row labels
        category_rules (dict): category name -> list of rule dicts
        technique_rules (dict): technique name -> list of rule dicts
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
    return (data["sections"],
            data["category_rules"],
            data["techniques"],
            data["mission_types"],
            data["mission_landing_pages"],
            data["mission_type_display"],
            data.get("omitted_instruments", []),
            data.get("instrument_type_overrides", []),
            data.get("display_name_overrides", []),
            data.get("collection_date_overrides", []))


(CATEGORY_SECTIONS, CATEGORY_RULES, TECHNIQUE_RULES,
 MISSION_TYPES, MISSION_LANDING_PAGES, MISSION_TYPE_DISPLAY,
 OMITTED_INSTRUMENTS, INSTRUMENT_TYPE_OVERRIDES,
 _DISPLAY_NAME_OVERRIDES,
 _COLLECTION_DATE_OVERRIDES) = _load()

ALL_CATEGORIES = []
for _section_labels in CATEGORY_SECTIONS.values():
    ALL_CATEGORIES.extend(_section_labels)

SECTION_ORDER = list(CATEGORY_SECTIONS.keys())

# Rekeyed once here (each entry's own lookup key is already known when the
# YAML list is parsed) so every caller gets an O(1) dict instead of each
# repeating the same rekey at its own call site.
DISPLAY_NAME_OVERRIDES_BY_NAME = {
    entry["name"]: entry["display_name"] for entry in _DISPLAY_NAME_OVERRIDES
}

COLLECTION_DATE_OVERRIDES_BY_LID = {
    entry["collection_lid"]: entry for entry in _COLLECTION_DATE_OVERRIDES
}


def sorted_rows(observed, canonical_order):
    """Returns observed category rows in canonical order, extras appended sorted.

    Args:
        observed (set of strings): category names actually present in the data.
        canonical_order (list of strings): the preferred display order from
              pipeline_rules.yaml.

    Returns (list of strings):
        canonical rows that appear in observed, followed by any observed
        rows not in canonical_order, sorted alphabetically.
    """
    ordered = [row for row in canonical_order if row in observed]
    # categories not yet added to pipeline_rules.yaml land here, sorted
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
