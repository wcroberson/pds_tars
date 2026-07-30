"""
rule_engine.py - Category matching via rules loaded from pipeline_rules.yaml.

All detection rules live in pipeline_rules.yaml under category_rules and techniques.
To add or change a rule, edit that file - no code changes needed here.

Rule type (defined in pipeline_rules.yaml):
  attribute_keyword   case-insensitive keyword match across specified PDS4
                      fields (e.g. fields: [title] for a title-only match)
                      (optional: exclude_keyword skips the rule when also
                      present - always a list of strings, even for a single
                      value; any one of which disqualifies the match)

Also supports an optional exclude_investigation: a list of keywords (always
a list, even for a single value) checked against the collection's
investigation/mission name, independent of whatever field the rule itself
matched on. Use this when a keyword is only wrong for one *mission*,
regardless of which instrument on that mission trips it - generalizes to any
future instrument on that mission, unlike excluding by instrument
name/abbreviation one at a time.

Attrs dict keys used by the evaluator (note: these are fields read straight
off a collection's own label - not PDS4 Context Products, which are a
different, separately-registered kind of thing; see build_investigation_cache()
etc. in registry.py for those):
  title             instrument or collection title (str)
  discipline        pds:Science_Facets.pds:discipline_name values (list[str])
  facet1            pds:Science_Facets.pds:facet1 values (list[str])
  facet2            pds:Science_Facets.pds:facet2 values (list[str])
  wavelength_range  pds:Science_Facets.pds:wavelength_range values (list[str])
  description       pds:Citation_Information.pds:description free text (list[str])
  domain            pds:Science_Facets.pds:domain values (list[str]) - usable
                    as a positive match field via attribute_keyword, e.g.
                    fields: [domain], keyword: ionosphere
  investigation     pds:Context_Area.pds:Investigation_Area investigation/
                    mission name(s) (list[str]) - only used by
                    exclude_investigation, never as a positive match field

Use make_attrs() to build an attrs dict with correct defaults.
"""

from pds_tars.schema import (CATEGORY_SECTIONS, CATEGORY_RULES, TECHNIQUE_RULES,
                    MISSION_TYPES, MISSION_LANDING_PAGES, MISSION_TYPE_DISPLAY)

# dict: section name (str) -> frozenset of row labels, for fast membership
# checks in categories_for_section() below.
_CATS_BY_SECTION = {
    section: frozenset(labels)
    for section, labels in CATEGORY_SECTIONS.items()
}


# ── Attrs builder ──────────────────────────────────────────────────────────────

def make_attrs(title="",
               discipline=[],
               facet1=[],
               facet2=[],
               wavelength_range=[],
               description="",
               domain=[],
               investigation=[]):
    """Builds an attrs dict for the category-matching functions.

    Callers must pass an actual list (or omit the argument) for every list
    field - never None - since the defaults here already cover "nothing to
    report."

    Args:
        title (string): instrument or collection title.
        discipline (list of strings): pds:Science_Facets.pds:discipline_name values.
        facet1 (list of strings): pds:Science_Facets.pds:facet1 values.
        facet2 (list of strings): pds:Science_Facets.pds:facet2 values.
        wavelength_range (list of strings): pds:Science_Facets.pds:wavelength_range values.
        description (string): pds:Citation_Information.pds:description free
              text, scoped to one collection - never the parent bundle's
              description, which covers sibling instruments too and would
              cause cross-contamination between them.
        domain (list of strings): pds:Science_Facets.pds:domain values (e.g.
              "Ionosphere", "Heliosphere") - usable as a positive match field
              via attribute_keyword.
        investigation (list of strings): pds:Context_Area.pds:Investigation_Area
              investigation/mission name(s) for this collection - only
              consulted by a rule's exclude_investigation, never a positive
              match field on its own.

    Returns (dict):
        the attrs dict consumed by _eval_rule(), matched_categories(),
        categories_for_section(), etc.
    """
    return {
        "title":            title,
        "discipline":       discipline,
        "facet1":           facet1,
        "facet2":           facet2,
        "wavelength_range": wavelength_range,
        "description":      [description] if description else [],
        "domain":           domain,
        "investigation":    investigation,
    }


# ── Rule evaluator ─────────────────────────────────────────────────────────────

def _eval_rule(rule, attrs):
    """Evaluates a single detection rule against an attrs dict.

    Args:
        rule (dict): a rule from pipeline_rules.yaml with at least a "type"
              key. Supported types: "attribute_keyword", "hardcoded_instruments"
              (see pipeline_rules.yaml's "Hardcoded instrument exceptions"
              section for when that one is appropriate - it is NOT a pattern
              to reuse for ordinary rules).
        attrs (dict): built by make_attrs().

    Raises:
        ValueError: if rule's "type" is not one of the supported types.

    Returns (bool):
        True if the rule matches, False otherwise.
    """
    match rule.get("type", ""):
        case "attribute_keyword":
            words = []
            for field in rule.get("fields", []):
                values = [attrs.get("title", "")] if field == "title" else (attrs.get(field) or [])
                for value in values:
                    words.append(value.lower())
            text = " ".join(words)
            keyword = rule["keyword"].lower()
            if not text or keyword not in text:
                return False

            # exclude_keyword/exclude_investigation only apply to attribute_keyword
            # rules: hardcoded_instruments is an exact-name allowlist, so there is
            # nothing left to disqualify once a name matches.
            exclude = rule.get("exclude_keyword", [])
            if any(kw.lower() in text for kw in exclude):
                return False
            exclude_inv = rule.get("exclude_investigation", [])
            if exclude_inv:
                inv_text = " ".join(name.lower() for name in (attrs.get("investigation") or []))
                if any(kw.lower() in inv_text for kw in exclude_inv):
                    return False
            return True

        case "hardcoded_instruments":
            return attrs.get("title", "") in rule.get("names", [])

        case _:
            raise ValueError(f"Unknown rule type: {rule.get('type', '')!r}")


# ── Public matching functions ─────────────────────────────────────────────────

def matched_categories(attrs):
    """Returns all category names matched by any rule, in YAML definition order.

    Args:
        attrs (dict): built by make_attrs().

    Returns (list of strings):
        category names from CATEGORY_RULES whose rules fire.
    """
    return [cat for cat, rules in CATEGORY_RULES.items()
            if any(_eval_rule(rule, attrs) for rule in rules)]


def categories_for_section(section_name, attrs):
    """Returns category names in section_name that are matched by attrs.

    Args:
        section_name (string): a key from CATEGORY_SECTIONS (e.g.
              "Electromagnetic Spectrum").
        attrs (dict): built by make_attrs().

    Returns (list of strings):
        subset of matched_categories() whose labels belong to section_name.
    """
    cats = _CATS_BY_SECTION.get(section_name, frozenset())
    return [cat for cat in matched_categories(attrs) if cat in cats]


def technique_categories(attrs):
    """Returns technique tags (Imaging, Spectroscopy, …) matched for attrs.

    Args:
        attrs (dict): built by make_attrs().

    Returns (list of strings):
        technique names from TECHNIQUE_RULES whose rules fire.
    """
    return [tech for tech, rules in TECHNIQUE_RULES.items()
            if any(_eval_rule(rule, attrs) for rule in rules)]


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
