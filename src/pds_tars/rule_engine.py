"""
rule_engine.py - Mission-type and landing-page lookups from pipeline_rules.yaml.

Categories and techniques no longer come from rules: they're read from the
science sorting pairs on bundle labels (see sorting_labels() in registry.py
and split_label() in schema.py). What's left here maps a PDS investigation
name to the pipeline's mission type (orbiter, flyby, ground_based, ...) and
landing page, from pipeline_rules.yaml's mission_types and
mission_landing_pages.
"""

from pds_tars.schema import MISSION_TYPES, MISSION_LANDING_PAGES, MISSION_TYPE_DISPLAY


# ── Mission type helpers ───────────────────────────────────────────────────────

# Sorted so the most specific spacecraft role comes first (lowest priority
# number in pipeline_rules.yaml wins tie-breaks).
_TYPE_PRIORITY = [
    entry["type"]
    for entry in sorted(MISSION_TYPE_DISPLAY, key=lambda entry: entry["priority"])
]


def mission_type_for(mission_name, planet=None):
    """Returns the mission_type for a PDS investigation name string.

    Matches case-insensitively against the "names" lists in the mission_types
    section of pipeline_rules.yaml.  When planet is supplied, a planet-scoped entry
    (one with a "planet" field matching that planet, case-insensitively) takes
    precedence over an unscoped entry with the same name.

    Args:
        mission_name (string): a pds:Investigation_Area.pds:name value from
              the registry, e.g. "GALILEO ORBITER".
        planet (string or None): optional target planet name, e.g. "Jupiter"
              or "Saturn".

    Returns (string):
        one of "flyby", "orbiter", "probe", "lander", "rover",
        "ground_based", or "unknown".
    """
    lower = mission_name.lower()
    planet_lower = planet.lower() if planet else None

    scoped_match = None
    unscoped_match = None

    for entry in MISSION_TYPES:
        if not any(name.lower() == lower for name in entry.get("names", [])):
            continue
        entry_planet = entry.get("planet")
        if entry_planet and planet_lower and entry_planet.lower() == planet_lower:
            if scoped_match is None:
                scoped_match = entry["type"]
        elif not entry_planet and unscoped_match is None:
            unscoped_match = entry["type"]

    return scoped_match or unscoped_match or "unknown"


def landing_page_for_mission(mission_name):
    """Returns the landing page URL for one PDS investigation name, or None.

    Args:
        mission_name (string): a pds:Investigation_Area.pds:name value.

    Returns (string or None):
        landing page URL from pipeline_rules.yaml's mission_landing_pages, or
        None if mission_name isn't listed there.
    """
    lower = mission_name.lower()
    for entry in MISSION_LANDING_PAGES:
        if any(name.lower() == lower for name in entry.get("names", [])):
            return entry.get("url")
    return None


def abbr_mission_type(mission_set, planet=None):
    """Returns the dominant mission type for an instrument abbreviation's missions.

    When an instrument spans multiple investigations with different types
    (e.g. both "Galileo" -> flyby and "GALILEO ORBITER" -> orbiter), the most
    specific type wins per _TYPE_PRIORITY.

    Args:
        mission_set (set of strings): pds:Investigation_Area.pds:name values
              associated with one instrument abbreviation.
        planet (string or None): optional target planet name passed through
              to mission_type_for.

    Returns (string):
        the highest-priority type found in mission_set per _TYPE_PRIORITY,
        or "unknown" if mission_set is empty.
    """
    types = {mission_type_for(mission, planet) for mission in mission_set}
    for mission_type in _TYPE_PRIORITY:
        if mission_type in types:
            return mission_type
    return "unknown"
