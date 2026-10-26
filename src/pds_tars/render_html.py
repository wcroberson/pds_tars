"""
render_html.py - HTML table rendering for the integrated archive tables.

HTML is built with Jinja2 (https://jinja.palletsprojects.com/).  The approach
has two steps:
  1. Data preparation - Python collects all display values into plain dicts and
     lists.  No HTML is written at this stage.
  2. Template rendering - a Jinja2 template string defines the HTML skeleton.
     _ENV.from_string(TEMPLATE).render(**data) fills every {{ }} placeholder,
     evaluates {% %} control blocks, and returns the complete HTML string.
"""

import datetime
import json
import os
from collections import defaultdict

from jinja2 import Environment   # pip install jinja2

from pds_tars.schema import rows_in_order, sorted_rows, collect_years, MISSION_TYPE_DISPLAY, MISSION_LANDING_PAGES
from pds_tars.rule_engine import landing_page_for_mission, mission_type_for

# Package lives at <project_root>/src/pds_tars/ -
# background_images/ is a sibling of src/, two directories up from this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))
_IMAGES_DIR = os.path.join(_PROJECT_ROOT, "background_images")


# ── Mission-type chip display properties ───────────────────────────────────────
# Loaded once at import time from pipeline_rules.yaml via MISSION_TYPE_DISPLAY.
# Alternate palettes are defined below; change _ACTIVE_PALETTE to switch.

# Alternate color palettes.  All entries cover every mission_type key in
# MISSION_TYPE_DISPLAY.  "default" mirrors what pipeline_rules.yaml currently encodes.
_PALETTES = {
    "default": {
        entry["type"]: entry["color"] for entry in MISSION_TYPE_DISPLAY
    },
    "cb_accessible": {
        # Paul Tol, "Colour Schemes," SRON/EPS/TN/09-002 (2021), "bright" scheme:
        # https://personal.sron.nl/~pault/. 3 of 7 (lander/ground_based/unknown)
        # are light and need dark chip text - see _PALETTE_TEXT_OVERRIDES below.
        "orbiter":      "#4477AA",
        "flyby":        "#228833",
        "probe":        "#AA3377",
        "lander":       "#BBBBBB",
        "rover":        "#EE6677",
        "ground_based": "#66CCEE",
        "unknown":      "#CCBB44",
    },
    "vivid": {
        "orbiter":      "#1565C0",
        "flyby":        "#2E7D32",
        "probe":        "#E65100",
        "lander":       "#4E342E",
        "rover":        "#6A1B9A",
        "ground_based": "#00695C",
        "unknown":      "#37474F",
    },
    "muted": {
        "orbiter":      "#4A6FA5",
        "flyby":        "#4A8C6A",
        "probe":        "#A0522D",
        "lander":       "#7D6147",
        "rover":        "#7B5EA7",
        "ground_based": "#427070",
        "unknown":      "#7A8694",
    },
}
_ACTIVE_PALETTE = "cb_accessible"
_CHIP_COLORS = _PALETTES[_ACTIVE_PALETTE]

# Per-palette text overrides for chip backgrounds too light for white text
# (WCAG contrast < 3:1) - only entries diverging from MISSION_TYPE_DISPLAY's
# own text_color are listed; other palettes stay dark enough for "#fff".
_PALETTE_TEXT_OVERRIDES = {
    "cb_accessible": {
        "orbiter":      "#222",
        "flyby":        "#222",
        "probe":        "#222",
        "lander":       "#222",
        "rover":        "#222",
        "ground_based": "#222",
        "unknown":      "#222",
    },
}
_CHIP_TEXT_COLORS = {
    entry["type"]: entry.get("text_color", "#fff") for entry in MISSION_TYPE_DISPLAY
}
_CHIP_TEXT_COLORS.update(_PALETTE_TEXT_OVERRIDES.get(_ACTIVE_PALETTE, {}))
_TYPE_LABELS = [
    (entry["type"], entry["label"])
    for entry in sorted(MISSION_TYPE_DISPLAY, key=lambda entry: entry["priority"])
]
_TYPE_LABEL_MAP = dict(_TYPE_LABELS)
_TYPE_PRIORITY_MAP = {
    entry["type"]: entry["priority"] for entry in MISSION_TYPE_DISPLAY
}

# Mission investigation names that are PDS placeholder strings, not real mission
# names - filtered out of all display contexts (missions section, ground-based
# chips, tooltips) so they never appear as labels.
_INVALID_MISSION_DISPLAY_NAMES = frozenset({
    "none",
    "no specific investigation",
})

# dict: investigation name, lowercase (str) -> canonical display name (str),
# from mission_landing_pages' canonical_name (falls back to its first name).
# Dedupes the missions section to one pill per real mission.
_MISSION_CANONICAL = {}
_MISSION_URL_TO_CANONICAL = {}
for _entry in MISSION_LANDING_PAGES:
    _canon = _entry.get("canonical_name") or _entry["names"][0]
    _url   = _entry.get("url")
    if _url and _url not in _MISSION_URL_TO_CANONICAL:
        _MISSION_URL_TO_CANONICAL[_url] = _canon
    for _name in _entry["names"]:
        _MISSION_CANONICAL[_name.lower()] = _canon


def _display_mission(name):
    """Returns a mission/investigation name in its human-vetted display form.

    PDS4 registry data spells the same mission differently across bundles
    (e.g. "GALILEO PROBE" vs "Juno"). Where _MISSION_CANONICAL has a
    hand-written display form for this exact string (from mission_landing_pages),
    use it instead of the raw registry casing.

    Deliberately not a blanket "titlecase if all-caps" transform: real
    acronym names (MAVEN, MRO, MGS) must stay all-caps, so anything without a
    canonical spelling passes through unchanged. Instrument abbreviations come
    from abbreviate() on a separate field and never reach this function.

    Args:
        name (string): raw investigation-name string from the PDS registry.

    Returns (string):
        the canonical display form if known, else `name` unchanged.
    """
    return _MISSION_CANONICAL.get(name.lower(), name)


def _display_missions(names):
    """Returns a comma-joined, deduplicated, canonically-cased mission list.

    Two raw investigation-name variants that canonicalize to the same
    display form (e.g. "Cassini" and "Cassini-Huygens Mission" both ->
    "Cassini-Huygens") collapse to one entry instead of showing twice.

    Args:
        names (iterable of strings): raw investigation-name strings.
    """
    seen = {}
    for name in sorted(names):
        seen.setdefault(_display_mission(name), None)
    return ", ".join(sorted(seen))


def _ground_based_entries(ground_based_abbrs, abbr_to_missions,
                          abbr_to_display_name=None,
                          abbr_to_landing_page=None):
    """Returns {display_label: (url, lookup_name)} for the ground-based section,
    shared by all three table writers (diagnostic, basic, pretty).

    One entry per distinct mission across all ground_based_abbrs, not per abbr
    - two ground-based instruments sharing one observatory's mission name
    collapse to a single label. Placeholder investigation strings ("None", "No
    Specific Investigation") are filtered out; when every mission recorded for
    an abbr turns out to be a placeholder, falls back to the display_name
    override from pipeline_rules.yaml's instrument_type_overrides, then to the
    abbr itself.

    Args:
        ground_based_abbrs (set): abbrs whose dominant mission type is
              "ground_based".
        abbr_to_missions (dict): {abbr: {mission_name, …}}.
        abbr_to_display_name (dict or None): {abbr: display_name} overrides,
              or None if the caller has no display-name fallback to offer.
        abbr_to_landing_page (dict or None): {abbr: url or None}, used only
              on the abbr-fallback path above (no mission name to look a URL
              up from directly there); None if the caller has no landing
              pages to offer (e.g. the diagnostic table, which never links
              ground-based items).

    Returns (dict):
        display_label -> (url, lookup_name). lookup_name is the raw
        investigation-name string (for _mission_tooltip's inv_cache lookup)
        when the label came from a real mission, or None when it came from
        the abbr/display-name fallback path.
    """
    display_name_map = abbr_to_display_name or {}
    landing_page_map  = abbr_to_landing_page or {}
    entries = {}
    for abbr in sorted(ground_based_abbrs):
        valid = {mission for mission in abbr_to_missions.get(abbr, set())
                 if mission.lower() not in _INVALID_MISSION_DISPLAY_NAMES}
        if valid:
            for mission in sorted(valid):
                label = _display_mission(mission)
                if label not in entries:
                    entries[label] = (landing_page_for_mission(mission), mission)
        else:
            label = display_name_map.get(abbr, abbr)
            if label not in entries:
                entries[label] = (landing_page_map.get(abbr), None)
    return entries


# Shared Jinja2 environment.  autoescape=True HTML-escapes all {{ expr }} output
# automatically.  CSS and JS strings are passed as template variables and marked
# safe with | safe so their characters are not corrupted.
_ENV = Environment(autoescape=True)


# ── Data-preparation helpers ───────────────────────────────────────────────────
# These return plain Python lists of dicts for the Jinja2 template to iterate
# over.  All logic (tooltip assembly, style strings, URL lookup) stays in Python
# so the template is pure presentation with no computations.

def _mission_tooltip(display_name, lookup_name, type_label, inv_cache):
    """Builds a labeled, multi-line tooltip for a mission pill.

    Shared by the main missions panel and the ground-based section so both use
    the same investigation-context-product data and the same readable layout.

    Args:
        display_name (string): canonical name shown on the pill. Tried
              first as an inv_cache key - the Investigation context
              product's own name is usually this exact bare form
              ("Galileo", "Cassini-Huygens"), not the specific
              per-instrument investigation string below.
        lookup_name (string or None): raw per-instrument investigation name
              (e.g. "GALILEO PROBE", "Cassini-Huygens Mission") to key into
              inv_cache (lowercased internally) if display_name alone
              doesn't match, or None when no PDS investigation record backs
              this pill (e.g. a ground-based instrument whose only
              "mission" is its own display name).
        type_label (string): already-resolved display label, e.g. "Orbiter"
              or, for a mission whose instruments span more than one
              mission type (a probe and an orbiter under one investigation
              name), the combined "Probe + Orbiter" form.
        inv_cache (dict): {investigation_name.lower(): {type, start_date,
              stop_date, description}} from build_investigation_cache().
    """
    lines = [f"{display_name} — {type_label}"]
    # The registry's per-instrument investigation string is often a variant of the
    # mission's real name ("GALILEO PROBE" vs "Galileo") rather than inv_cache's own
    # key - try the canonical display name first, fall back to the raw variant.
    inv_info = inv_cache.get(display_name.lower()) if display_name else None
    if not inv_info and lookup_name:
        inv_info = inv_cache.get(lookup_name.lower())
    if inv_info:
        start = (inv_info.get("start_date") or "")[:4]
        stop  = (inv_info.get("stop_date") or "")[:4]
        # Same "no end date yet" convention as instrument collections (see
        # CURRENT_YEAR in registry.py): some missions leave stop_date blank, others
        # stub a far-future date (e.g. 3000-01-01) - any year past today is "ongoing."
        if stop.isdigit() and int(stop) > datetime.date.today().year:
            stop = "ongoing"
        dates = f"{start}–{stop}" if start and stop else (start or stop or "")
        if dates:
            lines.append(f"Active: {dates}")
        desc = inv_info.get("description") or ""
        if desc:
            lines.append(desc)
    return "\n".join(lines)


def _split_bands(colors, slant=18.0):
    """Returns equal-area, diagonally-sliced color bands for a mixed-type
    mission pill (e.g. Galileo's Orbiter and Probe), each a solid fill with
    its own clip-path polygon.

    Flat clipped polygons are used instead of a single hard-stop
    linear-gradient because a diagonal gradient hard-stop rasterizes with a
    visible seam - one color bleeding through the other along the pill's
    edges, a known gradient/border-radius edge case. One solid color per
    band, clipped to a crisp shape, avoids it.

    Args:
        colors (list of strings): mission-type colors, first to last, left
              to right.
        slant (float): horizontal shift (percentage points) between a
              boundary's position at the pill's top edge vs. its bottom
              edge - larger values make the diagonal steeper. 0 would be a
              straight vertical split.

    Returns (list of dicts):
        one {"color": hex, "clip": CSS clip-path polygon() string} per band.
        Adjacent polygons share boundary coordinates, so the bands tile the
        pill with no gap or overlap; each covers exactly 1/len(colors) of the
        area (the slant changes each band's shape, not its area).
    """
    band_count = len(colors)
    bands = []
    for i, color in enumerate(colors):
        left, right = i * 100 / band_count, (i + 1) * 100 / band_count
        left_top,  left_bottom  = (0.0, 0.0)     if i == 0     else (left  - slant / 2, left  + slant / 2)
        right_top, right_bottom = (100.0, 100.0) if i == band_count - 1 else (right - slant / 2, right + slant / 2)
        clip = (f"polygon({left_top:.2f}% 0%, {right_top:.2f}% 0%, "
                f"{right_bottom:.2f}% 100%, {left_bottom:.2f}% 100%)")
        bands.append({"color": color, "clip": clip})
    return bands


def _checkbox_items(abbrs, row, yr, section, abbr_to_fullname,
                    abbr_to_missions, abbr_to_nodes):
    """Returns one dict per abbreviation for the diagnostic-table checkbox loop.

    Each dict carries the data-* values the generateText() JS function reads,
    plus the tooltip text shown on hover.

    Args:
        abbrs (list of strings): sorted instrument abbreviations for this cell.
        row (string): category row label (becomes data-row on the checkbox).
        yr (string): year string (becomes data-year on the checkbox).
        section (string): section name (becomes data-section on the checkbox).
        abbr_to_fullname (dict): {abbr: full instrument name}.
        abbr_to_missions (dict): {abbr: set of mission names}.
        abbr_to_nodes (dict): {abbr: set of PDS node names the instrument
              has collections on, from every node build.py's own query saw
              this run (not just the active --node)}.
    """
    items = []
    for abbr in abbrs:
        fullname = abbr_to_fullname.get(abbr, abbr)
        missions = _display_missions(abbr_to_missions.get(abbr, set()))
        nodes    = ", ".join(sorted(abbr_to_nodes.get(abbr, set())))
        tooltip  = f"{missions} — {fullname}" if missions else fullname
        if nodes:
            tooltip += f"  [{nodes}]"
        items.append({
            "abbr":         abbr,
            "tooltip":      tooltip,
            "data_row":     row,
            "data_year":    yr,
            "data_section": section,
        })
    return items


# ── Diagnostic HTML table template ────────────────────────────────────────────
# Variables this template expects (all supplied by write_diagnostic_html_table):
#   title_text (string)
#   css (string, passed with | safe - contains CSS syntax characters)
#   js_code (string, passed with | safe - contains JS syntax characters)
#   all_years (list of strings)
#   num_cols (int)
#   sections (list): {name, rows: [{label, cells: [list of dicts]}]}
#   ground_based_items (list): {abbr, tooltip}

_DIAGNOSTIC_TABLE_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{{ title_text }}</title>
  {# | safe: the CSS block contains characters (e.g. [type=checkbox]) that are
     valid CSS but would be wrongly escaped by Jinja2's autoescape. #}
  <style>{{ css | safe }}</style>
</head>
<body>
  <h2>{{ title_text }}</h2>
  <p class="subtitle">PDS4 registry only.
    Hover an abbreviation to see the full instrument name.
    Check instruments to mark them, then click Generate to produce copyable text.</p>

  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="sticky-col">Category</th>
          {% for yr in all_years %}
          <th>{{ yr }}</th>
          {% endfor %}
        </tr>
      </thead>
      <tbody>
        {% for section in sections %}
        <tr class="banner-row">
          <td colspan="{{ num_cols }}">{{ section.name | upper }}</td>
        </tr>

        {% if section.rows %}
          {% for row in section.rows %}
          <tr>
            <td class="sticky-col cat-col">{{ row.label }}</td>
            {% for cell in row.cells %}
            <td>
              {% for cb in cell %}
              {# Wrapping <input> inside <label> links them implicitly -
                 clicking anywhere on the label text toggles the checkbox. #}
              <label data-tip="{{ cb.tooltip }}">
                <input type="checkbox"
                       data-abbr="{{ cb.abbr }}"
                       data-row="{{ cb.data_row }}"
                       data-year="{{ cb.data_year }}"
                       data-section="{{ cb.data_section }}"/> {{ cb.abbr }}
              </label>
              {% endfor %}
            </td>
            {% endfor %}
          </tr>
          {% endfor %}
        {% else %}
          <tr>
            <td class="sticky-col cat-col">(no {{ section.name }} data indexed)</td>
            {% for _ in all_years %}<td></td>{% endfor %}
          </tr>
        {% endif %}
        {% endfor %}
      </tbody>
    </table>
  </div>

  {# Ground-based instruments are listed below the main table because they don't
     map to a single observation year.  data-abbr holds the mission name here
     (not an instrument abbreviation); the JS treats section="gb" specially. #}
  {% if ground_based_items %}
  <div class="gb-section">
    <h3 class="gb-heading">Ground-Based Observations</h3>
    {% for item in ground_based_items %}
    <label data-tip="{{ item.tooltip }}">
      <input type="checkbox"
             data-abbr="{{ item.abbr }}"
             data-row="Ground-Based"
             data-section="gb"/> {{ item.abbr }}
    </label>
    {% endfor %}
  </div>
  {% endif %}

  <div class="controls">
    <button onclick="generateText(true)">Generate Checked</button>
    <button onclick="generateText(false)">Generate Unchecked</button>
    <textarea id="output" rows="8"
              placeholder="Generated text will appear here…"
              readonly></textarea>
    <button class="copy-btn" onclick="copyOutput()">Copy to Clipboard</button>
  </div>

  {# One row per distinct instrument abbreviation appearing anywhere in the
     table above (deduped - an instrument can occupy multiple cells across
     years/categories). Node badges and notes are per-instrument here, not
     one shared blob for the whole page, so they actually stay useful as the
     table grows. Notes save to localStorage keyed per instrument; see
     _process_target() in build.py for where the node list comes from. #}
  <div class="notes-section">
    <strong>Instrument Notes</strong>
    <table class="notes-index">
      <thead><tr><th>Instrument</th><th>PDS nodes</th><th>Notes</th></tr></thead>
      <tbody>
        {% for item in instrument_index %}
        <tr>
          <td><button class="inst-btn" onclick="openDetail('{{ item.abbr }}')"><span class="idx-abbr">{{ item.abbr }}</span> {{ item.name }}</button></td>
          <td>{% for node in item.nodes %}<span class="node-badge{{ ' node-atm' if node in ('PDS_ATM','ATM') else '' }}">{{ node }}</span>{% endfor %}</td>
          <td><textarea class="notes-field" data-abbr="{{ item.abbr }}" placeholder="notes…"></textarea></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  {# Instruments the rule engine saw but matched to zero categories - never
     get a checkbox cell anywhere above, so without this they'd be invisible
     here. Same per-instrument notes mechanism as the table above. #}
  {% if unsorted_index %}
  <div class="notes-section">
    <strong>Unsorted Instruments <span class="section-hint">(no category match — comment if you know why)</span></strong>
    <table class="notes-index">
      <thead><tr><th>Instrument</th><th>PDS nodes</th><th>Notes</th></tr></thead>
      <tbody>
        {% for item in unsorted_index %}
        <tr>
          <td><button class="inst-btn" onclick="openDetail('{{ item.abbr }}')"><span class="idx-abbr">{{ item.abbr }}</span> {{ item.name }}</button></td>
          <td>{% for node in item.nodes %}<span class="node-badge{{ ' node-atm' if node in ('PDS_ATM','ATM') else '' }}">{{ node }}</span>{% endfor %}</td>
          <td><textarea class="notes-field" data-abbr="{{ item.abbr }}" placeholder="notes…"></textarea></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {# Instruments known to belong here but missing from the registry data
     entirely (not even unsorted above) - e.g. never migrated to PDS4, wrong
     node, not yet archived. Entered by hand here rather than generated,
     since the pipeline has nothing to report for them. Persisted as a JSON
     array in localStorage (same keyed-by-path mechanism as everything else
     on this page), so entries survive a build.py re-run as long as the
     output path doesn't change. #}
  <div class="notes-section">
    <strong>Missing Instruments <span class="section-hint">(known instruments not appearing above at all)</span></strong>
    <div class="missing-add-row">
      <input type="text" id="missing-name" placeholder="Instrument name…">
      <input type="text" id="missing-notes" placeholder="Why it's missing…">
      <button id="missing-add-btn">Add</button>
    </div>
    <table class="notes-index missing-table">
      <thead><tr><th>Instrument</th><th>Explanation</th><th></th></tr></thead>
      <tbody id="missing-tbody"></tbody>
    </table>
  </div>

  <div id="detail-modal" onclick="if(event.target===this)closeDetail()">
    <div id="detail-box">
      <button id="detail-close" onclick="closeDetail()">&times;</button>
      <div id="detail-content"></div>
    </div>
  </div>

  <div id="tip"></div>

  {# | safe: the JS block contains syntax characters like { } that are valid JS
     but should not be HTML-escaped. #}
  <script>
{{ js_code | safe }}
  </script>
</body>
</html>
"""


# ── Diagnostic HTML table (checkboxes) ─────────────────────────────────────────

def write_diagnostic_html_table(section_tables, name_to_abbr, abbr_to_missions,
                     abbr_to_fullname, ground_based_abbrs,
                     abbr_to_nodes, path, target_label,
                     abbr_to_collections=None, abbr_to_display_name=None):
    """Writes a diagnostic HTML category × year table to an .html file.

    Instruments appear as labeled checkboxes stacked vertically per cell.
    Hovering shows the full instrument name, mission, and PDS nodes. "Generate
    Checked / Unchecked" buttons produce copyable plain text. The Instrument
    Notes section below the table lists every distinct instrument once with
    its node badges and its own notes field, persisted via localStorage keyed
    to the file path plus instrument abbreviation.

    Args:
        section_tables (dict): {section_name: {category: {year_str: {abbr, …}}}}
        name_to_abbr (dict): {instrument_display_name: abbr}
        abbr_to_missions (dict): {abbr: {mission_name, …}}
        abbr_to_fullname (dict): {abbr: instrument_display_name}
        ground_based_abbrs (set): abbrs listed in the separate ground-based
              section.
        abbr_to_nodes (dict): {abbr: {pds_node_name, …}}, from every node
              build.py's own query saw this run (see _process_target() in
              build.py). Drives the node filter buttons (distinct_nodes
              below) and each chip's data-nodes attribute.
        path (string): output file path.
        target_label (string): human-readable target name for the page title.
    """
    sections_prepared = [
        (section_name, sorted_rows(set(table), rows_in_order(section_name)), table)
        for section_name, table in section_tables.items()
    ]
    all_years = collect_years(*section_tables.values())
    num_cols  = len(all_years) + 1   # category column + one column per year

    title_text = f"{target_label} — Category × Observation Start Year"

    # ── CSS ───────────────────────────────────────────────────────────────────
    css = """
  body { font-family: monospace; margin: 16px; }
  h2 { margin-bottom: 6px; }
  .subtitle { color: #555; font-size: 0.85em; margin: 0 0 10px; }
  .table-wrap { overflow: auto; max-height: 72vh; border: 1px solid #aaa; }
  table { border-collapse: collapse; white-space: nowrap; }
  th, td { border: 1px solid #bbb; padding: 3px 8px; vertical-align: top; }
  th { background: #e0e0e0; position: sticky; top: 0; z-index: 1; }
  .sticky-col { position: sticky; left: 0; background: #f0f0f0; z-index: 2;
                font-weight: bold; min-width: 160px; }
  thead th.sticky-col { z-index: 3; }
  .banner-row td { text-align: center; font-weight: bold; background: #c8d8e8;
                   padding: 5px; letter-spacing: 0.07em; font-size: 0.9em; }
  label { display: block; cursor: pointer; padding: 1px 3px; border-radius: 2px;
          white-space: nowrap; }
  label:hover { background: #fffbaa; }
  #tip { display: none; position: fixed; background: #222; color: #fff;
         padding: 4px 10px; border-radius: 4px; font-size: 0.82em;
         pointer-events: none; z-index: 9999; max-width: 520px;
         white-space: normal; line-height: 1.4; }
  input[type=checkbox] { cursor: pointer; vertical-align: middle; }
  .controls { margin-top: 14px; }
  .controls button { padding: 5px 14px; margin-right: 8px; cursor: pointer;
                     font-size: 0.9em; }
  #output { display: block; margin-top: 8px; width: 100%; max-width: 960px;
            font-family: monospace; font-size: 0.88em; box-sizing: border-box;
            resize: vertical; }
  .copy-btn { margin-top: 6px; padding: 4px 12px; cursor: pointer; font-size: 0.9em; }
  .notes-section { margin-top: 20px; }
  .notes-section strong { display: block; margin-bottom: 6px; }
  .section-hint { font-weight: normal; color: #777; font-size: 0.85em; }
  table.notes-index { border-collapse: collapse; width: 100%; max-width: 960px; }
  table.notes-index th, table.notes-index td { border: 1px solid #bbb; padding: 4px 8px;
                                                vertical-align: top; font-size: 0.88em; }
  table.notes-index th { background: #e0e0e0; text-align: left; }
  .idx-abbr { font-weight: bold; }
  .notes-field { display: block; width: 100%; min-width: 160px; height: 2.4em;
                 font-family: monospace; font-size: 0.95em; box-sizing: border-box;
                 resize: vertical; }
  .node-badge { display: inline-block; font-size: 0.8em; color: #555; background: #eee;
                border-radius: 8px; padding: 1px 6px; margin: 1px 3px 1px 0; white-space: nowrap; }
  .node-atm { background: #d8ecd8; color: #205020; font-weight: 600; }
  .missing-add-row { display: flex; gap: 8px; margin-bottom: 8px; max-width: 960px; }
  .missing-add-row input { flex: 1; font-family: monospace; font-size: 0.9em;
                            padding: 4px 6px; box-sizing: border-box; }
  .missing-add-row button { padding: 4px 14px; cursor: pointer; }
  table.missing-table td:last-child { width: 28px; text-align: center; }
  .remove-btn { cursor: pointer; border: none; background: none; color: #a00;
                font-size: 1.1em; padding: 0 4px; }
  .gb-section { margin-top: 20px; }
  .gb-heading { font-size: .9em; text-transform: uppercase; letter-spacing: .07em;
                font-weight: bold; color: #555; margin: 0 0 8px; }
  .inst-btn { background: none; border: none; cursor: pointer; font-family: inherit;
              font-size: inherit; text-align: left; color: #24508a; padding: 0;
              text-decoration: underline; }
  .inst-btn:hover { color: #0a2d6e; }
  #detail-modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.45);
                  z-index: 9000; justify-content: center; align-items: flex-start;
                  padding-top: 60px; }
  #detail-modal.open { display: flex; }
  #detail-box { background: white; max-width: 820px; width: 95%; max-height: 80vh;
                overflow-y: auto; border-radius: 4px; padding: 16px 20px; position: relative; }
  #detail-close { position: absolute; top: 8px; right: 12px; cursor: pointer;
                  font-size: 1.4em; border: none; background: none; }
  table.detail-table { border-collapse: collapse; width: 100%; margin: 10px 0 20px; }
  table.detail-table th { background: #eee; text-align: left; padding: 6px 8px; }
  table.detail-table td { border: 1px solid #ddd; padding: 4px 8px; font-size: 0.9em; }
  table.detail-table td:first-child { width: 160px; color: #555; font-weight: 600; }
"""

    # ── JavaScript ────────────────────────────────────────────────────────────
    # SECTION_ORDER / SECTIONS lines interpolate Python values; the rest is literal JS.
    # section_tables is already in display order (see build_table_data()).
    section_order_json = json.dumps(list(section_tables))
    sections_json      = json.dumps({section: sorted_rows(set(table), rows_in_order(section))
                                     for section, table in section_tables.items()})

    details_json = json.dumps({
        abbr: {
            "name": info["name"],
            "lid":  info["lid"],
            "collections": info["collections"],
        }
        for abbr, info in (abbr_to_collections or {}).items()
    })

    js_code = (
        f"  const SECTION_ORDER = {section_order_json};\n"
        f"  const SECTIONS = {sections_json};\n"
        f"  const DETAILS = {details_json};\n"
    ) + r"""
  function _esc(s) {
    const d = document.createElement('div');
    d.textContent = s || '';
    return d.innerHTML;
  }
  function openDetail(abbr) {
    const d = DETAILS[abbr];
    if (!d) return;
    let html = `<h3>${_esc(d.name)}</h3>`;
    if (d.lid) html += `<p style="font-size:0.82em;color:#555;margin:0 0 10px">${_esc(d.lid)}</p>`;
    (d.collections || []).forEach(c => {
      html += `<table class="detail-table"><tr><th colspan="2">${_esc(c.title)}</th></tr>`;
      if (c.pds_node)        html += `<tr><td>Node</td><td>${_esc(c.pds_node)}</td></tr>`;
      if (c.mission)         html += `<tr><td>Mission</td><td>${_esc(c.mission)}</td></tr>`;
      if (c.start || c.stop) html += `<tr><td>Dates</td><td>${_esc(c.start)} – ${_esc(c.stop)}</td></tr>`;
      if (c.discipline)      html += `<tr><td>Discipline</td><td>${_esc(c.discipline)}</td></tr>`;
      if (c.facet1)          html += `<tr><td>Facet</td><td>${_esc(c.facet1)}</td></tr>`;
      if (c.processing)      html += `<tr><td>Processing</td><td>${_esc(c.processing)}</td></tr>`;
      if (c.archive_status)  html += `<tr><td>Archive status</td><td>${_esc(c.archive_status)}</td></tr>`;
      if (c.description)     html += `<tr><td>Description</td><td>${_esc(c.description)}</td></tr>`;
      html += '</table>';
    });
    document.getElementById('detail-content').innerHTML = html;
    document.getElementById('detail-modal').classList.add('open');
  }
  function closeDetail() {
    document.getElementById('detail-modal').classList.remove('open');
  }
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); });

  function generateText(wantChecked) {
    const items = {};
    const sectionOf = {};
    const gbItems = [];
    document.querySelectorAll('input[type=checkbox]').forEach(cb => {
      if (cb.checked === wantChecked) {
        const { abbr, row, year, section } = cb.dataset;
        if (section === 'gb') { gbItems.push(abbr); return; }
        sectionOf[row] = section;
        if (!items[row]) items[row] = {};
        if (!items[row][year]) items[row][year] = [];
        items[row][year].push(abbr);
      }
    });
    const lines = [];
    SECTION_ORDER.forEach(sectionName => {
      const order = SECTIONS[sectionName] || [];
      const sectionRows = order.filter(r => items[r]);
      Object.keys(items).filter(r => sectionOf[r] === sectionName && !order.includes(r))
        .sort().forEach(r => sectionRows.push(r));
      if (sectionRows.length) {
        lines.push(sectionName.toUpperCase());
        sectionRows.forEach(row => {
          lines.push(row);
          Object.keys(items[row]).sort().forEach(yr => {
            lines.push('  ' + yr + ': ' + items[row][yr].sort().join(', '));
          });
        });
        lines.push('');
      }
    });
    if (gbItems.length) {
      lines.push('GROUND-BASED OBSERVATIONS');
      gbItems.sort().forEach(a => lines.push('  ' + a));
    }
    const ta = document.getElementById('output');
    ta.value = lines.join('\n');
    ta.rows = Math.max(8, lines.length + 2);
  }

  function copyOutput() {
    const ta = document.getElementById('output');
    if (!ta.value) return;
    navigator.clipboard.writeText(ta.value).catch(() => {
      ta.select();
      ta.setSelectionRange(0, ta.value.length);
    });
  }

  // Per-instrument notes - keyed by abbr (not just page path), so each
  // instrument's notes stay distinct instead of one shared blob.
  document.querySelectorAll('.notes-field').forEach(ta => {
    const notesKey = 'obs_notes_' + location.pathname + '_' + ta.dataset.abbr;
    const saved = localStorage.getItem(notesKey);
    if (saved) ta.value = saved;
    ta.addEventListener('input', () => localStorage.setItem(notesKey, ta.value));
  });

  // Missing-instrument entries: hand-typed, since the pipeline has nothing
  // to report for an instrument that isn't in the registry data at all.
  // Stored as a JSON array in localStorage, keyed by page path - survives a
  // build.py re-run as long as the output filename doesn't change.
  function escText(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
  }
  const missingKey = 'missing_instruments_' + location.pathname;
  function loadMissing() {
    try { return JSON.parse(localStorage.getItem(missingKey) || '[]'); }
    catch (e) { return []; }
  }
  function saveMissing(list) { localStorage.setItem(missingKey, JSON.stringify(list)); }
  function renderMissing() {
    const tbody = document.getElementById('missing-tbody');
    tbody.innerHTML = '';
    loadMissing().forEach((item, i) => {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td>${escText(item.name)}</td><td>${escText(item.notes)}</td>` +
                      `<td><button class="remove-btn" data-i="${i}" title="Remove">&times;</button></td>`;
      tbody.appendChild(tr);
    });
    tbody.querySelectorAll('.remove-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const list = loadMissing();
        list.splice(Number(btn.dataset.i), 1);
        saveMissing(list);
        renderMissing();
      });
    });
  }
  document.getElementById('missing-add-btn').addEventListener('click', () => {
    const nameEl = document.getElementById('missing-name');
    const notesEl = document.getElementById('missing-notes');
    if (!nameEl.value.trim()) return;
    const list = loadMissing();
    list.push({ name: nameEl.value.trim(), notes: notesEl.value.trim() });
    saveMissing(list);
    nameEl.value = '';
    notesEl.value = '';
    renderMissing();
  });
  renderMissing();

  const tipEl = document.getElementById('tip');
  document.addEventListener('mouseover', e => {
    const lbl = e.target.closest('label[data-tip]');
    if (lbl) { tipEl.textContent = lbl.dataset.tip; tipEl.style.display = 'block'; }
    else { tipEl.style.display = 'none'; }
  });
  document.addEventListener('mousemove', e => {
    if (tipEl.style.display === 'block') {
      tipEl.style.left = (e.clientX + 14) + 'px';
      tipEl.style.top  = (e.clientY - 8) + 'px';
    }
  });
  document.addEventListener('mouseout', e => {
    if (!e.relatedTarget || !e.relatedTarget.closest('label[data-tip]')) {
      tipEl.style.display = 'none';
    }
  });
"""

    # ── Prepare template data ─────────────────────────────────────────────────
    sections_data = []
    abbrs_in_table = set()
    for section_name, rows, table in sections_prepared:
        row_data = []
        for row in rows:
            cells = [
                _checkbox_items(
                    sorted(table[row].get(yr, set())),
                    row, yr, section_name,
                    abbr_to_fullname, abbr_to_missions, abbr_to_nodes,
                )
                for yr in all_years
            ]
            row_data.append({"label": row, "cells": cells})
            for cell in cells:
                abbrs_in_table.update(item["abbr"] for item in cell)
        sections_data.append({"name": section_name, "rows": row_data})

    # Ground-based: one checkbox per mission (not per instrument abbreviation).
    # data-abbr holds the mission name; the JS section="gb" path handles it.
    ground_based_items = [
        {"abbr": label, "tooltip": label}
        for label in sorted(_ground_based_entries(
            ground_based_abbrs, abbr_to_missions,
            abbr_to_display_name=abbr_to_display_name,
        ))
    ]

    # One row per distinct instrument that actually appears in the table
    # above (an instrument can occupy several cells across years/categories,
    # but should only get one notes field).
    instrument_index = [
        {
            "abbr": abbr,
            "name": abbr_to_fullname.get(abbr, abbr),
            "nodes": sorted(abbr_to_nodes.get(abbr, set())),
        }
        for abbr in sorted(abbrs_in_table)
    ]

    # Instruments name_to_abbr covers (every one the rule engine saw) that matched
    # zero categories and aren't ground-based - invisible in the grid above, so
    # they need their own listing here to be reviewable at all.
    unsorted_abbrs = set(name_to_abbr.values()) - abbrs_in_table - set(ground_based_abbrs)
    unsorted_index = [
        {
            "abbr": abbr,
            "name": abbr_to_fullname.get(abbr, abbr),
            "nodes": sorted(abbr_to_nodes.get(abbr, set())),
        }
        for abbr in sorted(unsorted_abbrs)
    ]

    # ── Render ────────────────────────────────────────────────────────────────
    html = _ENV.from_string(_DIAGNOSTIC_TABLE_TEMPLATE).render(
        title_text=title_text,
        css=css,
        js_code=js_code,
        all_years=all_years,
        num_cols=num_cols,
        sections=sections_data,
        ground_based_items=ground_based_items,
        instrument_index=instrument_index,
        unsorted_index=unsorted_index,
    )
    with open(path, "w") as f:
        f.write(html)
    print(f"HTML    written to: {path}")


# ── Basic (plain, zero-dependency) HTML table ─────────────────────────────────
# Variables this template expects (all supplied by write_basic_html_table):
#   title_text (string)
#   today (string)
#   all_years (list of strings)
#   num_cols (int)
#   sections (list): {name, rows: [{label, cells: [list of dicts]}]}
#   ground_based_missions (list): {label, url}
#
# Split into a page shell and content block for the same reason as the pretty
# table above (see that comment). This template's CSS is meant to be
# stripped/overridden, so CONTENT is the more likely variant to get pasted in.

_BASIC_TABLE_HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{{ title_text }}</title>
  <style>
    /* Minimal base - override or remove entirely when integrating into your site. */
    body{font-family:sans-serif;margin:16px;color:#111}
    h1{font-size:1.2em;margin:0 0 4px}
    .datestamp{color:#555;font-size:.85em;margin:0 0 14px}
    .table-wrap{overflow-x:auto}
    table{border-collapse:collapse;white-space:nowrap}
    th,td{border:1px solid #ccc;padding:4px 8px;vertical-align:top;text-align:left}
    th{background:#f0f0f0}
    .cat-col{white-space:normal;min-width:160px}
    .section-banner td{background:#d8d8d8;font-weight:bold;text-transform:uppercase;
                       font-size:.8em;letter-spacing:.06em}
    .gb-section{margin-top:20px}
    .abbr-key{margin-top:28px}
    .abbr-key h2{font-size:1em;margin:0 0 6px}
    a{color:#0645ad}
  </style>
</head>
<body>
"""

_BASIC_TABLE_FOOT = """\
</body>
</html>
"""

_BASIC_TABLE_CONTENT = """\
  <h1>{{ title_text }}</h1>
  <p class="datestamp">Generated {{ today }}</p>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th class="cat-col">Category</th>
          {% for yr in all_years %}<th>{{ yr }}</th>{% endfor %}
        </tr>
      </thead>
      <tbody>
        {% for section in sections %}
        <tr class="section-banner">
          <td colspan="{{ num_cols }}">{{ section.name }}</td>
        </tr>
        {% for row in section.rows %}
        <tr>
          <td class="cat-col">{{ row.label }}</td>
          {% for cell in row.cells %}
          <td>{% for chip in cell %}{%- if chip.url -%}<a href="{{ chip.url }}" target="_blank" rel="noopener noreferrer" title="{{ chip.tooltip }}">{{ chip.abbr }}</a>{%- else -%}<span title="{{ chip.tooltip }}">{{ chip.abbr }}</span>{%- endif -%}{%- if not loop.last %}, {% endif -%}{% endfor %}</td>
          {% endfor %}
        </tr>
        {% endfor %}
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% if ground_based_missions %}
  <div class="gb-section">
    <table>
      <tbody>
        <tr class="section-banner"><td>Ground-Based Observations</td></tr>
        {% for mission in ground_based_missions %}
        <tr>
          <td>{% if mission.url %}<a href="{{ mission.url }}" target="_blank" rel="noopener noreferrer">{{ mission.label }}</a>{% else %}{{ mission.label }}{% endif %}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}
  <div class="abbr-key">
    <h2>Abbreviation key</h2>
    <table>
      <thead><tr><th>Abbr</th><th>Mission(s)</th><th>Instrument</th></tr></thead>
      <tbody>
        {% for entry in abbr_key %}
        <tr>
          <td>{{ entry.abbr }}</td>
          <td>{{ entry.missions }}</td>
          <td>{{ entry.name }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
"""

_BASIC_TABLE_TEMPLATE = _BASIC_TABLE_HEAD + _BASIC_TABLE_CONTENT + _BASIC_TABLE_FOOT


def _basic_cell_items(abbrs, abbr_to_fullname, abbr_to_missions,
                      abbr_to_landing_page):
    items = []
    for abbr in abbrs:
        fullname = abbr_to_fullname.get(abbr, abbr)
        missions = _display_missions(abbr_to_missions.get(abbr, set()))
        tooltip  = f"{missions} — {fullname}" if missions else fullname
        items.append({"abbr": abbr, "tooltip": tooltip, "url": abbr_to_landing_page.get(abbr)})
    return items


def write_basic_html_table(section_tables, name_to_abbr, abbr_to_missions,
                            abbr_to_fullname, abbr_to_landing_page,
                            ground_based_abbrs, path, target_label,
                            abbr_to_display_name=None):
    """Writes a plain, dependency-free HTML table suitable for direct integration.

    No background images, no colored chips, no JavaScript. Instrument
    abbreviations appear as comma-separated plain text (with a <title> tooltip)
    or plain links where a landing page is available. The only CSS present is a
    minimal base that integrators can override or strip entirely.

    Intended as the basic variant shipping alongside the stylized pretty table.

    Args:
        section_tables (dict): {section_name: {category: {year_str: {abbr, …}}}}
        name_to_abbr (dict): {instrument_display_name: abbr}
        abbr_to_missions (dict): {abbr: {mission_name, …}}
        abbr_to_fullname (dict): {abbr: instrument_display_name}
        abbr_to_landing_page (dict): {abbr: url_str or None}
        ground_based_abbrs (set): abbrs listed in the separate ground-based
              section.
        path (string): output file path.
        target_label (string): human-readable target name for the page title.
    """
    sections_prepared = [
        (section_name, sorted_rows(set(table), rows_in_order(section_name)), table)
        for section_name, table in section_tables.items()
    ]
    all_years = collect_years(*section_tables.values())
    num_cols  = len(all_years) + 1
    today     = datetime.date.today().isoformat()
    title_text = f"{target_label} Archive"

    sections_data = []
    for section_name, rows, table in sections_prepared:
        row_data = []
        for row in rows:
            cells = [
                _basic_cell_items(
                    sorted(table[row].get(yr, set())),
                    abbr_to_fullname, abbr_to_missions, abbr_to_landing_page,
                )
                for yr in all_years
            ]
            row_data.append({"label": row, "cells": cells})
        sections_data.append({"name": section_name, "rows": row_data})

    ground_based_missions_data = [
        {"label": label, "url": url}
        for label, (url, _lookup) in sorted(_ground_based_entries(
            ground_based_abbrs, abbr_to_missions,
            abbr_to_display_name=abbr_to_display_name,
            abbr_to_landing_page=abbr_to_landing_page,
        ).items())
    ]

    # Abbreviation key: every distinct abbr in any cell, sorted. Ground-based
    # abbrs are listed separately in the grid but included here too, so readers
    # can still look up what e.g. "IRAM-30m" refers to.
    all_abbrs = set(name_to_abbr.values())
    abbr_key = [
        {
            "abbr":    abbr,
            "name":    abbr_to_fullname.get(abbr, abbr),
            "missions": _display_missions(abbr_to_missions.get(abbr, set())),
        }
        for abbr in sorted(all_abbrs)
    ]

    html = _ENV.from_string(_BASIC_TABLE_TEMPLATE).render(
        title_text=title_text,
        today=today,
        all_years=all_years,
        num_cols=num_cols,
        sections=sections_data,
        ground_based_missions=ground_based_missions_data,
        abbr_key=abbr_key,
    )
    with open(path, "w") as f:
        f.write(html)
    print(f"Basic   written to: {path}")


# ── Pretty (display-only) HTML table template ──────────────────────────────────
# Variables this template expects (all supplied by write_pretty_html_table):
#   title_text           : str
#   target_label         : str
#   today                : str  (ISO date string)
#   css                  : str  (passed with | safe)
#   tip_js               : str  (passed with | safe)
#   all_years            : list[str]
#   num_cols             : int
#   main_table_class     : str  (CSS class string for the main table wrapper div)
#   gb_table_class       : str  (CSS class string for the ground-based table wrapper)
#   type_labels          : list of {mission_type, label, color}
#   sections             : list of {name, rows: [{label, cells: [list[dict]]}]}
#   ground_based_missions: list of {label, url, link_style, span_style}
#
# Split into a page shell and content block so a future site-integration path
# can render CONTENT alone into an existing page - see README.md's
# "Integrating into an existing website" section for the CSS-scoping caveats.

_PRETTY_TABLE_HEAD = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>{{ title_text }}</title>
  <style>{{ css | safe }}</style>
</head>
<body>
"""

_PRETTY_TABLE_FOOT = """\
</body>
</html>
"""

_PRETTY_TABLE_CONTENT = """\
  <h1>{{ title_text }}</h1>
  <p class="datestamp">Generated {{ today }}</p>

  {# Missions section: one card per distinct mission in the table. Each card
     shows the type label and mission name, links to the landing page, and
     shows a rich tooltip (type · dates · description) on hover. #}
  {% if missions_data %}
  <div class="missions-panel">
    <div class="missions-heading">MISSIONS IN THIS TABLE</div>
    <div class="missions-grid">
    {% for mission in missions_data %}
    {% if mission.bands %}
    {# Mixed-type pill: each band is its own link, clipped to only its own
       diagonal region, so clicking the orbiter-colored side goes to the
       orbiter's page and clicking the probe-colored side goes to the
       probe's page. mc-type is now its own link too (mission.url - see the
       default_type comment above) rather than a pointer-events:none
       passthrough: the diagonal seam runs right under a centered label,
       so a plain click on the text landed on whichever band happened to
       be under the cursor (usually the "wrong" one) instead of a
       predictable default. #}
    <span class="mission-card mixed" data-tip="{{ mission.tooltip }}" style="--mc-text:{{ mission.text_col }}">
      <span class="mc-bg">
        {% for band in mission.bands %}
        {% if band.url %}
        <a class="mc-band" href="{{ band.url }}" target="_blank" rel="noopener noreferrer" aria-label="{{ mission.name }} — {{ band.label }}" style="background:{{ band.color }};clip-path:{{ band.clip }}"></a>
        {% else %}
        <span class="mc-band" style="background:{{ band.color }};clip-path:{{ band.clip }}"></span>
        {% endif %}
        {% endfor %}
      </span>
      <a class="mc-type" href="{{ mission.url }}" target="_blank" rel="noopener noreferrer" aria-label="{{ mission.name }}">{{ mission.name }}</a>
    </span>
    {% elif mission.url %}
    <a class="mission-card" href="{{ mission.url }}" target="_blank" rel="noopener noreferrer" data-tip="{{ mission.tooltip }}" style="--mc-color:{{ mission.color }};--mc-text:{{ mission.text_col }}">
      <span class="mc-type">{{ mission.name }}</span>
    </a>
    {% else %}
    <span class="mission-card" data-tip="{{ mission.tooltip }}" style="--mc-color:{{ mission.color }};--mc-text:{{ mission.text_col }}">
      <span class="mc-type">{{ mission.name }}</span>
    </span>
    {% endif %}
    {% endfor %}
    </div>
  </div>
  {% endif %}

  {# Legend: one color swatch + label per mission type, ordered by priority.
     Only types that actually appear as chips in either table are shown
     (ground_based included, now that it goes through the same category
     placement as everything else - see the ground-based table below). #}
  <div class="legend">
    <span class="legend-label">Mission type:</span>
    {% for entry in type_labels %}
    {# legend items double as mission-type filter toggles; click to hide/show
       chips of that type. active = visible; filter-off = hidden. #}
    <span class="legend-item mtype-toggle active" data-mtype="{{ entry.mission_type }}">
      <span class="swatch" style="background:{{ entry.color }}"></span>{{ entry.label }}
    </span>
    {% endfor %}
  </div>

  {# Filter bar: chip-level filters that never hide category rows - only
     hide/show individual instrument chips within cells. Click a legend
     item to toggle that mission type; click node or technique buttons
     to show only chips that meet the selected criteria.
     All filters are opt-out (everything on by default); clicking a button
     turns it grey and hides matching chips.
     Technique buttons only appear if at least one displayed instrument
     has that tag (from its "Technique:..." science sorting labels). #}
  {% if distinct_nodes or distinct_techniques %}
  <div class="filter-bar">
    {% if distinct_nodes %}
    <span class="filter-group-label">Node:</span>
    {% for node in distinct_nodes %}
    <button class="filter-btn node-btn active" data-node="{{ node }}" type="button">{{ node | replace('PDS_','') }}</button>
    {% endfor %}
    {% endif %}
    {% if distinct_techniques %}
    <span class="filter-group-label" style="margin-left:12px">Technique:</span>
    {% for tech in distinct_techniques %}
    <button class="filter-btn tech-btn" data-tech="{{ tech }}" type="button">{{ tech }}</button>
    {% endfor %}
    {% endif %}
    <button class="filter-btn show-all-btn" id="show-all" type="button">Show all</button>
    <input type="text" id="chip-search" class="chip-search" placeholder="Search instruments…" autocomplete="off">
  </div>
  {% endif %}

  {# Both the main table and the ground-based table (below) render from this
     one macro and share one year axis, so the two read as one continuous
     timeline split across two tables. Their row sets don't have to match -
     each shows only the categories it actually has data for (see
     build_table_data()'s gb_section_tables) - only the columns need to
     line up, which the shared colgroup guarantees regardless of content.

     Each category is one or more physical <tr>s ("lanes"): every instrument
     is a single pill spanning colspan = its year range, greedily packed
     into the fewest lanes with no overlap (_category_lanes() in Python -
     lane assignment is a whole-bar decision, so a bar never fragments
     across cells or changes lane partway through). Using colspan for the
     pill's own width (rather than one <td> per year with the pill visually
     overflowing into its neighbors) means there's no cross-cell seam for a
     later cell's own background to paint over the earlier part of a pill -
     each pill is a real, single, correctly-sized cell. #}
  {% macro render_matrix(wrap_id, wrap_class, title, categories, show_banners) %}
  <div id="{{ wrap_id }}" class="{{ wrap_class }}">
    <table>
      {# table-layout:fixed determines column widths from the FIRST row's
         cells - which here is the colspan title row, not the per-column
         .cat-col/.yr-col row, so without an explicit colgroup each table
         falls back to sizing its own columns by content. That's harmless
         for one table alone, but with two tables sharing year columns
         (main and ground-based) it means their column boundaries drift
         apart. An explicit colgroup, plus an explicit width on the <table>
         itself (see table_total_width), pins both tables to the exact same
         per-column pixel widths regardless of either one's content -
         col_widths (computed in Python from both tables' pills) gives each
         column its own width rather than one uniform value, so a column
         only grows past the minimum when something in it actually needs
         the room. #}
      <colgroup>
        <col style="width:{{ cat_col_width }}px">
        {% for yr in all_years %}
        <col style="width:{{ col_widths[loop.index0] }}px">
        {% endfor %}
      </colgroup>
      <thead>
        <tr class="table-title-row">
          <th colspan="{{ num_cols }}">{{ title }}</th>
        </tr>
        <tr>
          <th class="cat-col">Category</th>
          {% for yr in all_years %}
          <th class="yr-col">{{ yr }}</th>
          {% endfor %}
        </tr>
      </thead>
      <tbody>
        {% for cat in categories %}
        {# data-section-banner used by the collapse click handler to hide/show
           the whole section; only the main table gets banners - the
           ground-based table reads fine as a flat "Section - Category" list
           without them. #}
        {% if show_banners and (loop.first or cat.section != loop.previtem.section) %}
        <tr class="banner" data-section-banner="{{ cat.section }}">
          <td colspan="{{ num_cols }}"><span class="section-arrow">▼</span>{{ cat.section }}</td>
        </tr>
        {% endif %}
        {% for lane in cat.lanes %}
        <tr data-section="{{ cat.section }}">
          {% if loop.first %}
          <td class="cat-col" rowspan="{{ cat.lanes | length }}">{% if show_banners %}{{ cat.category }}{% else %}{{ cat.section }} - {{ cat.category }}{% endif %}</td>
          {% endif %}
          {% for cell in lane %}
          {% if cell.kind == 'pill' %}
          <td class="yr-col pill-col"{% if cell.colspan > 1 %} colspan="{{ cell.colspan }}"{% endif %}>
            {% if cell.url %}
            <a class="chip chip-pill{% if cell.ongoing %} chip-ongoing{% endif %}" data-mtype="{{ cell.mission_type }}"
               data-nodes="{{ cell.nodes_str }}" data-tech="{{ cell.tech_str }}"
               data-abbr="{{ cell.abbr }}" data-count="{{ cell.count }}"
               data-tc='{{ cell.technique_counts | tojson }}'
               href="{{ cell.url }}" target="_blank" rel="noopener noreferrer"
               style="background:{{ cell.color }};color:{{ cell.text_col }}"
               data-tip="{{ cell.tooltip }}">{{ cell.abbr }}{% if cell.count > 0 %}<span class="chip-count">{{ cell.count }}</span>{% endif %}{% if cell.is_nonarchived %}<span class="chip-status" title="Includes non-archived {{ count_unit }}s"></span>{% endif %}</a>
            {% else %}
            <span class="chip chip-pill{% if cell.ongoing %} chip-ongoing{% endif %}" data-mtype="{{ cell.mission_type }}"
                  data-nodes="{{ cell.nodes_str }}" data-tech="{{ cell.tech_str }}"
                  data-abbr="{{ cell.abbr }}" data-count="{{ cell.count }}"
                  data-tc='{{ cell.technique_counts | tojson }}'
                  style="background:{{ cell.color }};color:{{ cell.text_col }}"
                  data-tip="{{ cell.tooltip }}">{{ cell.abbr }}{% if cell.count > 0 %}<span class="chip-count">{{ cell.count }}</span>{% endif %}{% if cell.is_nonarchived %}<span class="chip-status" title="Includes non-archived {{ count_unit }}s"></span>{% endif %}</span>
            {% endif %}
          </td>
          {% else %}
          <td class="yr-col"{% if cell.colspan > 1 %} colspan="{{ cell.colspan }}"{% endif %}></td>
          {% endif %}
          {% endfor %}
        </tr>
        {% endfor %}
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endmacro %}

  {{ render_matrix('main-table-wrap', main_table_class, target_label ~ ' Integrated Archive Table', categories, true) }}

  {% if has_ground_based %}
  {{ render_matrix('gb-table-wrap', gb_table_class, 'Ground-Based Observations', gb_categories, false) }}
  {% endif %}

  <div id="tip"></div>

  {# tip_js uses [data-tip] (not label[data-tip]) so it matches both <a> and <span> chips. #}
  <script>
{{ tip_js | safe }}
  </script>
"""

_PRETTY_TABLE_TEMPLATE = _PRETTY_TABLE_HEAD + _PRETTY_TABLE_CONTENT + _PRETTY_TABLE_FOOT


# ── Pretty (display-only) HTML table ──────────────────────────────────────────

def _find_background_image(basename):
    """Returns the on-disk filename of a background image for `basename`, or
    None if no matching file exists.

    Scans _IMAGES_DIR case-insensitively and returns the file's real on-disk
    name, so the emitted URL keeps the actual case. Probing fixed lowercase
    extensions instead would work on a case-insensitive Mac filesystem but
    emit a `.jpg` URL that 404s for a file saved as `.JPG` on a
    case-sensitive server.

    Args:
        basename (string): image filename without extension, e.g.
              "jupiter_background".

    Returns (string or None):
        the matching file's real name (e.g. "earth_background.JPG"), or
        None if nothing matches.
    """
    _EXTENSION_PRIORITY = {"jpg": 0, "jpeg": 1, "png": 2, "webp": 3}
    prefix = f"{basename.lower()}."
    try:
        entries = os.listdir(_IMAGES_DIR)
    except OSError:
        return None
    matches = [
        (_EXTENSION_PRIORITY[ext], entry)
        for entry in entries
        for ext in [entry.rsplit(".", 1)[-1].lower()]
        if entry.lower().startswith(prefix) and ext in _EXTENSION_PRIORITY
    ]
    return min(matches)[1] if matches else None


# All display properties are derived from mission_type_display in pipeline_rules.yaml.
# To change colors, labels, or add a new type: edit pipeline_rules.yaml only.

# Base CSS for write_pretty_html_table()'s output. Doesn't reference any
# variables - the two dynamic pieces (.yr-col width, .has-bg/.gb-has-bg
# background-image rules) get appended in the function itself.
_PRETTY_TABLE_BASE_CSS = """
  *{box-sizing:border-box}
  body{font-family:-apple-system,"Segoe UI",Arial,sans-serif;
       margin:0;padding:16px 24px;background:#f0f2f5;color:#1a1a2e}
  h1{font-size:1.4rem;margin:0 0 4px;font-weight:700}
  .datestamp{color:#555;font-size:.82em;margin:0 0 14px}
  .legend{display:flex;align-items:center;gap:18px;flex-wrap:wrap;
          margin-bottom:18px;font-size:.82em}
  .legend-label{font-weight:600;color:#444}
  .legend-item{display:flex;align-items:center;gap:5px}
  .swatch{width:28px;height:16px;border-radius:8px;display:inline-block}
  .table-wrap,.gb-table-wrap{overflow:auto;border-radius:6px;
                              box-shadow:0 1px 4px rgba(0,0,0,.15)}
  .gb-table-wrap{margin-top:24px}
  table{border-collapse:collapse;table-layout:fixed;white-space:nowrap;
        background:#fff}
  .centered table{margin:0 auto}
  th,td{border:1px solid #d8dde4;padding:4px 8px;vertical-align:top}
  th{background:#e8ecf0;font-weight:600;font-size:.8em;
     position:sticky;top:0;z-index:1;text-align:center}
  .cat-col{position:sticky;left:0;z-index:2;background:#f5f7fa;
           font-size:.85em;font-weight:500;
           white-space:normal;line-height:1.35;vertical-align:middle}
  thead th.cat-col{z-index:3;background:#e8ecf0}
  .table-title-row th{position:static;background:#2b4f6e;color:#e8f0f8;
    font-size:.75em;font-weight:700;text-align:center;text-transform:uppercase;
    letter-spacing:.1em;padding:8px 12px}
  /* Section banners: main table only (show_banners=true) - the ground-based
     table reads fine as a flat "Section - Category" list without them. */
  .banner td{background:#2b4f6e;color:#e8f0f8;font-weight:700;
             font-size:.75em;letter-spacing:.1em;text-transform:uppercase;
             text-align:center;padding:6px 10px}
  .section-arrow{display:inline-block;transition:transform .15s;
                 margin-right:6px;font-size:.85em;opacity:.7}
  .banner{cursor:pointer}
  .banner.section-collapsed .section-arrow{transform:rotate(-90deg)}
  .collapsed{display:none}
  #tip{display:none;position:fixed;background:#222;color:#fff;
       padding:4px 10px;border-radius:4px;font-size:.82em;
       pointer-events:none;z-index:9999;max-width:520px;
       white-space:pre-line;line-height:1.4}
  .legend-item{cursor:pointer;user-select:none;transition:opacity .15s}
  .legend-item.filter-off{opacity:.3}
  .legend-item.filter-off .swatch{box-shadow:inset 0 0 0 1px #999}
  .filter-bar{display:flex;align-items:center;gap:6px;flex-wrap:wrap;
              margin-bottom:10px;font-size:.8em}
  .filter-group-label{font-weight:600;color:#444;white-space:nowrap}
  .filter-btn{padding:2px 8px;border:1px solid #bbb;border-radius:10px;
              cursor:pointer;font-size:.8em;background:#f0f0f0;
              transition:opacity .15s,background .15s}
  .filter-btn:not(.active){opacity:.35;background:#e0e0e0}
  .show-all-btn{margin-left:8px;background:#eef;border-color:#99b}
  .chip-hidden{display:none!important}
  .tech-btn:not(.active){opacity:1;background:#f0f0f0;border-color:#ccc}
  .tech-btn.active{background:#2e6ea0;color:#fff;border-color:#1e5088}
  .chip-count{font-size:.7em;opacity:.8;margin-left:4px;font-weight:400;
              background:rgba(0,0,0,.18);border-radius:8px;padding:0 4px}
  .chip-status{display:inline-block;width:7px;height:7px;border-radius:50%;
               background:#f0a000;margin-left:4px;vertical-align:middle;
               flex-shrink:0}
  /* Pill timeline: every instrument is one bar, colspan'd across its whole
     year range (_category_lanes() in Python) - a one-year instrument is
     colspan=1, filling its single cell exactly like the old small chip; a
     multi-year one is the same pill just wider, instead of a badge plus a
     separately-drawn connector line. Because the colspan cell IS the bar's
     full width, there's nothing to overflow into a neighboring column and
     no cross-cell z-index/background seam to worry about. */
  .pill-col{padding:2px 3px}
  .chip-pill{display:block;width:100%;box-sizing:border-box;
             padding:3px 8px;border-radius:13px;font-size:.75em;font-weight:700;
             white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
             text-decoration:none;text-align:left}
  a.chip-pill{cursor:pointer}
  span.chip-pill{cursor:default}
  /* Still-active instrument (PDS's year-3000 stop-date sentinel, capped at
     today's year - see current_year_col): a literal end pill would be a
     false end date, so the bar's right edge is a point instead, signaling
     "continues past here." Only the right side is clipped (the polygon's
     left edge runs straight down the bounding box, x=0 to x=0, so it
     doesn't cut into anything border-radius already excluded there) -
     border-radius keeps rounding the left end normally. */
  .chip-ongoing{clip-path:polygon(0 0, calc(100% - 14px) 0, 100% 50%, calc(100% - 14px) 100%, 0 100%);
                padding-right:22px}
  .chip-search{padding:2px 8px;border:1px solid #bbb;border-radius:10px;
               font-size:.8em;width:160px;font-family:inherit;background:#fff;
               margin-left:8px}
  .missions-panel{background:#f7f9fc;border:1px solid #dce3ea;border-radius:10px;
                  padding:14px 18px;margin-bottom:18px}
  .missions-heading{font-size:.68em;font-weight:700;letter-spacing:.12em;
                    color:#7a8899;margin-bottom:10px;text-transform:uppercase}
  .missions-grid{display:flex;flex-wrap:wrap;gap:8px}
  .mission-card{display:inline-flex;align-items:center;padding:5px 14px;
                border-radius:20px;font-size:.82em;font-weight:600;
                color:var(--mc-text,#fff);background:var(--mc-color,#4a5568);
                text-decoration:none;cursor:pointer;white-space:nowrap;
                box-shadow:0 0 0 2px transparent;
                transition:filter .15s,box-shadow .15s;
                position:relative;overflow:hidden}
  .mission-card:hover{filter:brightness(1.12);box-shadow:0 0 0 2px rgba(255,255,255,.4)}
  a.mission-card:focus{outline:2px solid #2b6cb0;outline-offset:2px}
  /* Mixed-type pill (e.g. Galileo Orbiter+Probe): flat, clip-path-cut solid
     bands rather than a gradient - see _split_bands() for why. mc-bg holds
     the stacked bands below the label; mc-type sits above them via z-index.
     Hover ring uses box-shadow, not border - border's padding-box would
     misalign with inset:0's border-box, letting the --mc-color fallback
     show through around the pill's edges. */
  .mc-bg{position:absolute;inset:0;z-index:0}
  .mc-band{position:absolute;inset:0;display:block;cursor:pointer}
  .mc-band:focus{outline:2px solid #2b6cb0;outline-offset:-2px;z-index:2}
  .mission-card .mc-type{position:relative;z-index:1;pointer-events:none}
  /* Mixed-type pill's label is its own link (see the mixed-pill template
     comment) - override the pointer-events:none above so clicks land on
     its default_type URL instead of falling through to whichever
     diagonal band happens to be under the cursor. No visual difference
     from a normal (single-type) mission-card label otherwise - it's
     still just a link, styled to match. */
  .mission-card.mixed .mc-type{pointer-events:auto;color:inherit;
                                text-decoration:none}
  .mission-card.mixed .mc-type:focus{outline:2px solid #2b6cb0;outline-offset:1px}
"""


def _abbr_display_info(abbr, abbr_to_mission_type, abbr_to_fullname,
                        abbr_to_missions, abbr_to_landing_page,
                        abbr_to_nodes, abbr_to_techniques,
                        abbr_to_collection_count, abbr_nonarchived,
                        abbr_to_technique_counts=None, count_unit="collection"):
    """Per-abbreviation label/tooltip/color/URL info for the pretty table's
    pill renderer (_category_lanes()), so an abbreviation's look and link are
    built in one place. count_unit names what the count badge counts
    ("collection" or "bundle"), for the tooltip's "N collections" text.
    """
    mission_type = abbr_to_mission_type.get(abbr, "unknown")
    fullname     = abbr_to_fullname.get(abbr, abbr)
    missions_raw = (mission for mission in abbr_to_missions.get(abbr, set())
                    if mission.lower() not in _INVALID_MISSION_DISPLAY_NAMES)
    missions     = _display_missions(missions_raw)
    count        = abbr_to_collection_count.get(abbr, 0)
    count_label  = f"{count} {count_unit}{'s' if count != 1 else ''}"
    type_label   = _TYPE_LABEL_MAP.get(mission_type, "Unknown")
    mission_line = f"{missions} ({type_label})" if missions else type_label
    techs_raw    = sorted(abbr_to_techniques.get(abbr, set()))
    detail_line  = count_label + (f" · Techniques: {', '.join(techs_raw)}" if techs_raw else "")
    return {
        "abbr":              abbr,
        "fullname":          fullname,
        "mission_line":      mission_line,
        "detail_line":       detail_line,
        "mission_type":      mission_type,
        "color":             _CHIP_COLORS.get(mission_type, _CHIP_COLORS["unknown"]),
        "text_col":          _CHIP_TEXT_COLORS.get(mission_type, "#fff"),
        "url":               abbr_to_landing_page.get(abbr),
        "nodes_str":         ",".join(sorted(abbr_to_nodes.get(abbr, set()))),
        "tech_str":          ",".join(sorted(abbr_to_techniques.get(abbr, set()))),
        "count":             count,
        "technique_counts":  (abbr_to_technique_counts or {}).get(abbr, {}),
        "is_nonarchived":    abbr in abbr_nonarchived,
    }


def _category_lanes(category, year_map, spans_for_category, all_years,
                     col_index_by_year, current_year_col,
                     abbr_to_mission_type, abbr_to_fullname,
                     abbr_to_missions, abbr_to_landing_page, abbr_to_nodes,
                     abbr_to_techniques, counts_for_category,
                     abbr_nonarchived, abbr_to_technique_counts=None,
                     count_unit="collection"):
    """Builds one category row's instruments as elongated pill bars, packed
    into the minimum number of non-overlapping vertical "lanes".

    Every instrument - whether it has one year of data or spans many -
    becomes a single bar covering colspan = (end_col - start_col + 1)
    columns, so a one-year instrument looks exactly like the old small chip
    (a pill filling its one column) and a multi-year one is the same pill
    just wider, instead of a badge-plus-separate-connector-line. A still-
    active instrument (PDS's year-3000 stop-date sentinel) is capped at
    current_year_col with an "ongoing" flag the template draws as a pointed
    right edge, rather than a real end year that would be false.

    Lane assignment is a single whole-bar decision (not made per cell, so a
    bar reads as one continuous object): instruments are sorted by start
    column, and each is placed in the lowest-numbered lane that's free for
    its entire range (greedy interval-graph coloring). A lane's occupant
    never moves once placed, even if an earlier lane frees up later, so two
    instruments never visually swap lanes partway across the row.

    Args:
        category (string): this row's category label (for lookups only).
        year_map (dict): section_tables[section][category]: {year_str: {abbr, ...}}.
        spans_for_category (dict): category_spans.get(category, {}):
              {abbr: {start,end,ongoing}}.
        all_years (list of strings): this page's shared year columns, in order.
        col_index_by_year (dict): {year_str: column index}.
        current_year_col (int or None): column index for today's year, or
              None if no ongoing instrument on the page needs one.
        counts_for_category (dict): category_collection_counts.get(category, {}):
              {abbr: number of collections contributing to THIS category
              only} - not the instrument's total across every category it
              appears in.
        (remaining args: same per-abbreviation lookups as _abbr_display_info().)

    Returns (list of lists of dicts):
        One list per lane; each lane is a list of cell dicts covering all_years
        exactly once, in order - either {"kind": "empty", "colspan": N} or a
        pill dict (kind="pill", plus colspan, ongoing, and _abbr_display_info()'s
        fields) for the columns an instrument's bar occupies.
    """
    num_cols = len(all_years)
    abbrs_present = sorted({abbr for abbrs in year_map.values() for abbr in abbrs})
    if not abbrs_present:
        return []

    raw_spans = []  # (start_col, end_col, ongoing, abbr)
    for abbr in abbrs_present:
        span = spans_for_category.get(abbr)
        if span:
            start_col = col_index_by_year.get(str(span["start"]))
            if start_col is None:
                continue
            ongoing = span["ongoing"]
            if ongoing:
                end_col = max(start_col, current_year_col if current_year_col is not None else start_col)
            else:
                end_col = col_index_by_year.get(str(span["end"]), start_col)
        else:
            # Not in category_spans means either exactly one dated year, or
            # "No Date" - either way it occupies a single real column here.
            years_here = [yr for yr, abbrs in year_map.items() if abbr in abbrs]
            if not years_here:
                continue
            start_col = col_index_by_year.get(years_here[0])
            if start_col is None:
                continue
            end_col = start_col
            ongoing = False
        raw_spans.append((start_col, end_col, ongoing, abbr))

    # Greedy interval-graph coloring: earliest start first, lowest free lane wins.
    # "Free" means the lane's last bar ends before this one starts - ties go to
    # whichever bar was considered first (single pass, no re-optimization).
    raw_spans.sort(key=lambda span: (span[0], span[3]))
    lane_busy_until = []
    placed = []
    for start_col, end_col, ongoing, abbr in raw_spans:
        for lane_idx, busy_until in enumerate(lane_busy_until):
            if busy_until < start_col:
                lane_busy_until[lane_idx] = end_col
                placed.append((start_col, end_col, ongoing, abbr, lane_idx))
                break
        else:
            lane_busy_until.append(end_col)
            placed.append((start_col, end_col, ongoing, abbr, len(lane_busy_until) - 1))

    lanes = [[] for _ in lane_busy_until]
    for start_col, end_col, ongoing, abbr, lane_idx in placed:
        lanes[lane_idx].append((start_col, end_col, ongoing, abbr))

    lane_cells = []
    for lane_spans in lanes:
        lane_spans.sort(key=lambda span: span[0])
        cells = []
        col = 0
        for start_col, end_col, ongoing, abbr in lane_spans:
            # One cell per empty column, not a merged colspan cell - border-collapse
            # only draws a vertical grid line between separate cells, so a merged
            # empty cell would erase the column dividers wanted wherever a pill isn't covering.
            while col < start_col:
                cells.append({"kind": "empty", "colspan": 1})
                col += 1
            info = _abbr_display_info(
                abbr, abbr_to_mission_type, abbr_to_fullname, abbr_to_missions,
                abbr_to_landing_page, abbr_to_nodes, abbr_to_techniques,
                counts_for_category, abbr_nonarchived, abbr_to_technique_counts,
                count_unit=count_unit,
            )
            info["kind"]    = "pill"
            info["colspan"] = end_col - start_col + 1
            info["ongoing"] = ongoing
            year_range = f"{all_years[start_col]}–ongoing" if ongoing else (
                all_years[start_col] if start_col == end_col else f"{all_years[start_col]}–{all_years[end_col]}"
            )
            info["tooltip"] = f"{info['abbr']} — {info['fullname']}\n{info['mission_line']}\n{info['detail_line']}\n{year_range}"
            cells.append(info)
            col = end_col + 1
        while col < num_cols:
            cells.append({"kind": "empty", "colspan": 1})
            col += 1
        lane_cells.append(cells)
    return lane_cells


def write_pretty_html_table(section_tables, name_to_abbr, abbr_to_missions,
                             abbr_to_fullname, abbr_to_mission_type,
                             abbr_to_landing_page, ground_based_abbrs,
                             abbr_to_nodes, abbr_to_techniques,
                             category_collection_counts, abbr_nonarchived,
                             path, target_label,
                             abbr_to_display_name=None,
                             investigation_cache=None,
                             abbr_to_technique_counts=None,
                             category_spans=None,
                             gb_section_tables=None,
                             gb_category_spans=None,
                             gb_category_collection_counts=None,
                             count_unit="collection"):
    """Writes a read-only, display-quality HTML table to an .html file.

    Instruments appear as colored chips (no checkboxes).  Chip color encodes
    mission type (flyby / orbiter / probe / …) per the §3.4 vocabulary defined
    in _CHIP_COLORS.  Chips link to the mission landing page when available.

    Args:
        section_tables (dict): {section_name: {category: {year_str: {abbr, …}}}}
        name_to_abbr (dict): {instrument_display_name: abbr}
        abbr_to_missions (dict): {abbr: {mission_name, …}}
        abbr_to_fullname (dict): {abbr: instrument_display_name}
        abbr_to_mission_type (dict): {abbr: mission_type_str}
        abbr_to_landing_page (dict): {abbr: url_str or None}
        ground_based_abbrs (set): abbrs listed in the separate ground-based
              section.
        category_collection_counts (dict): {category: {abbr: number of
              collections contributing to that category}} from
              build_table_data() - an instrument's chip shows how many
              collections are actually behind *that* pill, not its
              instrument-wide total (e.g. 10 on a UV pill, 1 on the same
              instrument's Atmospheric Composition pill).
        abbr_nonarchived (set): abbrs with at least one non-archived collection.
        path (string): output file path.
        target_label (string): human-readable target name for the page title.
        category_spans (dict or None): {category: {abbr: {"start": int,
              "end": int|None, "ongoing": bool}}} from build_table_data() -
              drives the start/end-pill timeline connector drawn under each
              row's chips. Absent entries just don't get a connector (a
              single-year instrument has nothing to draw a line to).
        gb_section_tables (dict or None): same shape as section_tables,
              ground-based abbreviations only, row-aligned with it (see
              build_table_data()'s docstring). Drives a second table below
              the main one, sharing its year columns and row order.
        gb_category_spans (dict or None): same shape as category_spans, for
              gb_section_tables' own timeline.
        gb_category_collection_counts (dict or None): same shape as
              category_collection_counts, for gb_section_tables' own chips.
        count_unit (string): what the chip counts and the non-archived marker
              refer to - "collection", or "bundle" when the table is built
              from bundles.
    """
    category_spans = category_spans or {}
    gb_section_tables = gb_section_tables or {}
    gb_category_spans = gb_category_spans or {}
    gb_category_collection_counts = gb_category_collection_counts or {}
    has_ground_based = any(
        abbrs for table in gb_section_tables.values()
        for year_map in table.values()
        for abbrs in year_map.values()
    )
    for section_name, table in section_tables.items():
        if not table:
            raise ValueError(
                f"section_tables contains empty section '{section_name}'; "
                "build.py must strip empty sections before calling this function."
            )

    sections_prepared = [
        (section_name, sorted_rows(set(table), rows_in_order(section_name)), table)
        for section_name, table in section_tables.items()
    ]
    # Independent of sections_prepared - the ground-based table only shows
    # categories that actually have ground-based data (see build_table_data()).
    gb_sections_prepared = [
        (section_name, sorted_rows(set(table), rows_in_order(section_name)), table)
        for section_name, table in gb_section_tables.items()
    ]

    # Both tables share one year axis - collect_years() over both
    # section_tables and gb_section_tables, since the two tables' row sets
    # don't have to match but their columns always do.
    all_years = collect_years(*section_tables.values(), *gb_section_tables.values())

    # A span's end year may not otherwise be a column - e.g. a bar running to 1989
    # needs a real 1989 column for its end pill even if nothing else started that
    # year. Same for "ongoing" bars, capped at today's year, which needs a column.
    span_end_years = {
        str(span["end"])
        for abbrs in list(category_spans.values()) + list(gb_category_spans.values())
        for span in abbrs.values()
        if span["end"] is not None
    }
    current_year = datetime.date.today().year
    has_ongoing = any(
        span["ongoing"]
        for abbrs in list(category_spans.values()) + list(gb_category_spans.values())
        for span in abbrs.values()
    )
    extra_years = span_end_years | ({str(current_year)} if has_ongoing else set())
    if extra_years - set(all_years):
        has_no_date = "No Date" in all_years
        real_years = sorted((set(all_years) | extra_years) - {"No Date"})
        all_years = (["No Date"] + real_years) if has_no_date else real_years
    col_index_by_year = {yr: i for i, yr in enumerate(all_years)}
    current_year_col = col_index_by_year.get(str(current_year)) if has_ongoing else None
    num_cols  = len(all_years) + 1
    today     = datetime.date.today().isoformat()

    # Built here (rather than down in "Prepare template data") because the
    # column-width computation just below needs to look at every pill's
    # abbreviation/count before the colgroup can be sized.
    def _build_categories(sections_prepared_list, spans_by_category, counts_by_category):
        """Flatten (section, category) rows into one ordered list of
        {section, category, lanes} dicts. The template adds its own section
        banners (main table) or "Section - Category" labels (ground-based
        table) - this doesn't care which, it just carries both section and
        category through per row."""
        out = []
        for section_name, rows, table in sections_prepared_list:
            for row in rows:
                lanes = _category_lanes(
                    row, table[row], spans_by_category.get(row, {}),
                    all_years, col_index_by_year, current_year_col,
                    abbr_to_mission_type, abbr_to_fullname, abbr_to_missions,
                    abbr_to_landing_page, abbr_to_nodes, abbr_to_techniques,
                    counts_by_category.get(row, {}), abbr_nonarchived,
                    abbr_to_technique_counts, count_unit=count_unit,
                )
                if not lanes:
                    continue
                out.append({"section": section_name, "category": row, "lanes": lanes})
        return out

    categories_data = _build_categories(sections_prepared, category_spans, category_collection_counts)
    gb_categories_data = _build_categories(gb_sections_prepared, gb_category_spans, gb_category_collection_counts)

    # Each year column gets its own width, grown only when something in that
    # column actually needs more room. A multi-year pill spreads its label across
    # its spanned columns, so only a colspan=1 pill forces its own column wider.
    cat_col_width    = 260
    min_yr_col_width = 66
    max_yr_col_width = 260
    col_widths = [min_yr_col_width] * len(all_years)
    for cat in categories_data + gb_categories_data:
        for lane in cat["lanes"]:
            col = 0
            for cell in lane:
                if cell["kind"] == "pill" and cell["colspan"] == 1:
                    # px-per-character + fixed overhead, regression-fit to real measured
                    # chip-pill widths. Biased generous on purpose: an overestimate just
                    # widens a column, an underestimate truncates real text.
                    needed = round(7.5 * len(cell["abbr"])) + 50
                    col_widths[col] = min(max_yr_col_width, max(col_widths[col], needed))
                col += cell["colspan"]

    # table-layout:fixed only pins widths to the colgroup if <table> has an
    # explicit width - left "auto", a nowrap cell's min-content width still
    # stretches its column, breaking the main/ground-based tables' shared alignment.
    table_total_width = cat_col_width + sum(col_widths)

    bg_image_location = None
    bg_filename = _find_background_image(f"{target_label.lower()}_background")
    if bg_filename:
        bg_image_location = os.path.relpath(
            os.path.join(_IMAGES_DIR, bg_filename), os.path.dirname(os.path.abspath(path))
        ).replace(os.sep, "/")

    gb_bg_image_location = None
    gb_filename = _find_background_image("earth_background")
    if gb_filename:
        gb_bg_image_location = os.path.relpath(
            os.path.join(_IMAGES_DIR, gb_filename), os.path.dirname(os.path.abspath(path))
        ).replace(os.sep, "/")

    main_table_class = "table-wrap has-bg" if bg_image_location else "table-wrap"
    gb_table_class   = "gb-table-wrap gb-has-bg" if gb_bg_image_location else "gb-table-wrap"

    title_text = f"{target_label} Archive"

    # ── CSS ───────────────────────────────────────────────────────────────────
    # Per-column widths come from the colgroup (col_widths, computed above)
    # in each render_matrix() call, not a uniform .yr-col rule.
    css = (_PRETTY_TABLE_BASE_CSS
           + f"\n  #main-table-wrap table,#gb-table-wrap table{{width:{table_total_width}px}}\n")

    if bg_image_location:
        css += (
            f"\n  .has-bg table{{background-image:url('{bg_image_location}');"
            "background-size:cover;background-position:center;}"
            # .has-bg itself (the scrollable wrapper, not just the table) gets the same
            # tint as th/td, so scrolling past the rightmost column shows a consistent
            # tint instead of the raw background image breaking abruptly.
            "\n  .has-bg{background-color:rgba(255,255,255,0.30)}"
            "\n  .has-bg th,.has-bg td{background:rgba(255,255,255,0.30)}"
            "\n  .has-bg th{background:rgba(232,236,240,0.55)}"
            "\n  .has-bg .table-title-row th{background:rgba(43,79,110,0.85)}"
            "\n  .has-bg .cat-col{background:#f5f7fa}"
            "\n  .has-bg thead th.cat-col{background:#e8ecf0}"
            "\n  .has-bg .banner td{background:rgba(43,79,110,0.65)}"
        )
    if gb_bg_image_location:
        css += (
            f"\n  .gb-has-bg{{background-image:url('{gb_bg_image_location}');"
            "background-size:cover;background-position:left center;}"
            # Same tint on the wrapper itself as .has-bg gets, so scrolling past the
            # ground-based table's last column shows a consistent tint instead of the
            # raw image breaking abruptly - keeps both tables' backgrounds equally opaque.
            "\n  .gb-has-bg{background-color:rgba(255,255,255,0.30)}"
            "\n  .gb-has-bg table{background:transparent}"
            "\n  .gb-has-bg td{background:rgba(255,255,255,0.30)}"
            # .gb-has-bg td also matches .cat-col (a td) and, being more specific, was
            # winning - causing the same sticky-column bleed-through .has-bg's own
            # .cat-col rule above already fixes for the main table. Same fix here.
            "\n  .gb-has-bg .cat-col{background:#f5f7fa}"
            "\n  .gb-has-bg thead th.cat-col{background:#e8ecf0}"
        )

    tip_js = r"""
  let _searchTerm='';
""" + """
  const tip=document.getElementById('tip');
  document.addEventListener('mouseover',e=>{
    const el=e.target.closest('[data-tip]');
    if(el){tip.textContent=el.dataset.tip;tip.style.display='block';}
    else tip.style.display='none';
  });
  document.addEventListener('mousemove',e=>{
    if(tip.style.display==='block'){
      tip.style.left=(e.clientX+14)+'px';
      tip.style.top=(e.clientY-8)+'px';
    }
  });

  // Center a table within its wrap when it fits without horizontal
  // scrolling; leave it left-aligned when it's wide enough to need scroll,
  // so the table never starts partly off-screen in both directions.
  function centerTablesThatFit(){
    document.querySelectorAll('.table-wrap, .gb-table-wrap').forEach(wrap=>{
      const table=wrap.querySelector('table');
      if(!table) return;
      wrap.classList.toggle('centered', table.scrollWidth<=wrap.clientWidth+1);
    });
  }
  window.addEventListener('load', centerTablesThatFit);
  window.addEventListener('resize', centerTablesThatFit);

  // Ground-based table (if present) shares the main table's year columns -
  // keep them scrolling together so it reads as one continuous timeline
  // split across two tables. The two tables' row sets don't have to match
  // (each shows only the categories it has real data for), so there's no
  // row-height syncing to do - only the columns need to line up, which the
  // shared colgroup (see render_matrix()) already guarantees.
  const mainTableWrap = document.getElementById('main-table-wrap');
  const gbTableWrap    = document.getElementById('gb-table-wrap');
  if (mainTableWrap && gbTableWrap) {
    let syncingScroll = false;
    function syncScroll(from, to) {
      if (syncingScroll) return;
      syncingScroll = true;
      to.scrollLeft = from.scrollLeft;
      syncingScroll = false;
    }
    mainTableWrap.addEventListener('scroll', () => syncScroll(mainTableWrap, gbTableWrap));
    gbTableWrap.addEventListener('scroll', () => syncScroll(gbTableWrap, mainTableWrap));
  }

  // applyFilters: hide/show individual chips based on all active filters.
  // Mission type and node buttons are opt-out (all active by default; clicking
  // removes that type).  Technique buttons are opt-in (none active by default;
  // clicking adds that technique to the filter).  When no technique is selected
  // there is no technique constraint and all chips pass.  When at least one
  // technique is selected, only chips tagged with a matching technique pass;
  // chips with no technique tag are hidden.
  function applyFilters(){
    const offMtypes=new Set([...document.querySelectorAll('.mtype-toggle:not(.active)')].map(el=>el.dataset.mtype));
    const onNodes  =new Set([...document.querySelectorAll('.node-btn.active')].map(el=>el.dataset.node));
    const onTechs  =new Set([...document.querySelectorAll('.tech-btn.active')].map(el=>el.dataset.tech));

    document.querySelectorAll('.chip').forEach(chip=>{
      const mtypeOk = !offMtypes.has(chip.dataset.mtype);

      const nodeVals=(chip.dataset.nodes||'').split(',').filter(Boolean);
      const nodeOk = onNodes.size===0 || nodeVals.length===0 || nodeVals.some(n=>onNodes.has(n));

      const techVals=(chip.dataset.tech||'').split(',').filter(Boolean);
      const techOk  = onTechs.size===0 || techVals.some(t=>onTechs.has(t));

      const searchOk = !_searchTerm ||
        (chip.dataset.abbr||'').toLowerCase().includes(_searchTerm) ||
        (chip.dataset.tip||'').toLowerCase().includes(_searchTerm);

      chip.classList.toggle('chip-hidden', !(mtypeOk && nodeOk && techOk && searchOk));
    });

    // Update each chip's count badge to reflect the active technique filter.
    // If no technique is selected, show the full collection count; if one or
    // more techniques are selected, sum the per-technique counts stored on the
    // chip as a JSON object in data-tc (keyed by the full technique name, so
    // multi-word technique names work without any attribute-name mangling).
    document.querySelectorAll('.chip').forEach(chip=>{
      const badge=chip.querySelector('.chip-count');
      if(!badge) return;
      if(onTechs.size===0){
        badge.textContent=chip.dataset.count||'';
      } else {
        const counts=JSON.parse(chip.dataset.tc||'{}');
        let n=0;
        onTechs.forEach(t=>{ if(counts[t]) n+=counts[t]; });
        badge.textContent=n>0?String(n):(chip.dataset.count||'');
      }
    });

  }

  // Legend items toggle mission type visibility on click.
  document.querySelectorAll('.mtype-toggle').forEach(item=>{
    item.addEventListener('click',()=>{
      item.classList.toggle('active');
      item.classList.toggle('filter-off');
      applyFilters();
    });
  });

  // Node and technique filter buttons.
  document.querySelectorAll('.node-btn, .tech-btn').forEach(btn=>{
    btn.addEventListener('click',()=>{
      btn.classList.toggle('active');
      applyFilters();
    });
  });

  // "Show all" resets every filter: node buttons back to all-active (opt-out
  // default); technique buttons back to none-active (opt-in default = no filter);
  // search box cleared.
  const showAllBtn=document.getElementById('show-all');
  if(showAllBtn) showAllBtn.addEventListener('click',()=>{
    document.querySelectorAll('.mtype-toggle').forEach(el=>{el.classList.add('active');el.classList.remove('filter-off');});
    document.querySelectorAll('.node-btn').forEach(el=>el.classList.add('active'));
    document.querySelectorAll('.tech-btn').forEach(el=>el.classList.remove('active'));
    const s=document.getElementById('chip-search');
    if(s){s.value='';_searchTerm='';}
    applyFilters();
  });

  // Text search: filter chips by abbreviation or tooltip text as you type.
  const searchEl=document.getElementById('chip-search');
  if(searchEl) searchEl.addEventListener('input',()=>{
    _searchTerm=searchEl.value.trim().toLowerCase();
    applyFilters();
  });

  // Collapsible section banners (main table only - the ground-based table
  // has none): click to fold/unfold the rows below.
  document.querySelectorAll('.banner[data-section-banner]').forEach(bannerRow=>{
    bannerRow.addEventListener('click',()=>{
      const name=bannerRow.dataset.sectionBanner;
      const collapsed=bannerRow.classList.toggle('section-collapsed');
      document.querySelectorAll('tr[data-section]').forEach(tr=>{
        if(tr.dataset.section===name) tr.classList.toggle('collapsed',collapsed);
      });
    });
  });

"""

    # ── Prepare template data ─────────────────────────────────────────────────

    # Distinct node and technique values across chips actually in the table,
    # for filter buttons. Only techniques present in at least one displayed
    # instrument get a button - don't show a filter for a tag nobody has.
    distinct_nodes = sorted({node for nodes in abbr_to_nodes.values() for node in nodes})
    distinct_techniques = sorted({tech for techs in abbr_to_techniques.values() for tech in techs})

    # Legend: only show swatches for mission types actually appearing as pills in
    # either table. ground_based goes through the same category placement as
    # everything else, earning its swatch the same way any other type does.
    present_types = {
        cell["mission_type"]
        for cat in (categories_data + gb_categories_data)
        for lane in cat["lanes"]
        for cell in lane
        if cell["kind"] == "pill"
    }
    type_labels_data = [
        {"mission_type": mt, "label": lbl, "color": _CHIP_COLORS[mt]}
        for mt, lbl in _TYPE_LABELS
        if mt in present_types
    ]

    # Missions section: one pill per distinct mission, all chips + ground-based.
    # Variant name spellings (e.g. "GALILEO ORBITER"/"Galileo") collapse via
    # canonical_name; placeholder strings ("None", etc.) are filtered out.
    chip_abbrs = {
        cell["abbr"]
        for cat in (categories_data + gb_categories_data)
        for lane in cat["lanes"]
        for cell in lane
        if cell["kind"] == "pill"
    }
    all_relevant_abbrs = chip_abbrs | ground_based_abbrs

    # Pass 1: group by canonical mission name, not raw investigation string -
    # e.g. Pioneer Venus's Orbiter/Multiprobe share one investigation name but
    # need different URLs, known only via build.py's per-abbr instrument_type_overrides.
    types_by_canonical     = defaultdict(set)
    url_by_canonical_type  = {}
    sample_mission         = {}  # canonical -> one raw mission string, for tooltip/inv_cache lookup
    for abbr in sorted(all_relevant_abbrs):
        abbr_type = abbr_to_mission_type.get(abbr, "unknown")
        abbr_url  = abbr_to_landing_page.get(abbr)
        for mission in sorted(abbr_to_missions.get(abbr, set())):
            if mission.lower() in _INVALID_MISSION_DISPLAY_NAMES:
                continue
            canonical = _MISSION_CANONICAL.get(mission.lower(), mission)
            types_by_canonical[canonical].add(abbr_type)
            if abbr_url:
                url_by_canonical_type[(canonical, abbr_type)] = abbr_url
            sample_mission.setdefault(canonical, mission)
    for canonical, types in list(types_by_canonical.items()):
        if types == {"unknown"}:
            unscoped = mission_type_for(sample_mission[canonical], None)
            if unscoped != "unknown":
                types_by_canonical[canonical] = {unscoped}

    # Pass 2: one pill per canonical mission name.
    inv_cache = investigation_cache or {}
    missions_by_name = {}
    for canonical in sorted(types_by_canonical):
        mission      = sample_mission[canonical]
        types        = types_by_canonical[canonical]
        sorted_types = sorted(types, key=lambda mission_type: _TYPE_PRIORITY_MAP.get(mission_type, 99))
        best_type    = sorted_types[0]
        # Single-type: prefers the type's own URL, else the investigation lookup. Mixed
        # pill (e.g. Pioneer Venus) defaults to "orbiter", not best_type's priority order -
        # bare "Pioneer Venus" means the Orbiter to everyone but PDS's own PV1/PV2 naming.
        default_type = "orbiter" if len(types) > 1 and "orbiter" in types else best_type
        url = (url_by_canonical_type.get((canonical, default_type))
               or landing_page_for_mission(mission))
        bands = None
        if len(sorted_types) == 1:
            color      = _CHIP_COLORS.get(best_type, _CHIP_COLORS["unknown"])
            text_col   = _CHIP_TEXT_COLORS.get(best_type, "#fff")
            type_label = _TYPE_LABEL_MAP.get(best_type, "Unknown")
        else:
            # Spans more than one mission type under one mission (e.g. Galileo Orbiter +
            # Probe) - solid diagonal bands, not a gradient (see _split_bands()). Each
            # band gets its own url/label - one pill-wide href can't cover two pages.
            geometry = _split_bands(
                [_CHIP_COLORS.get(mission_type, _CHIP_COLORS["unknown"]) for mission_type in sorted_types]
            )
            bands = [
                {
                    "color": geo["color"],
                    "clip":  geo["clip"],
                    "url":   (url_by_canonical_type.get((canonical, mission_type))
                              or landing_page_for_mission(mission)),
                    "label": _TYPE_LABEL_MAP.get(mission_type, "Unknown"),
                }
                for mission_type, geo in zip(sorted_types, geometry)
            ]
            color      = bands[0]["color"]  # fallback base fill only
            text_col   = "#222"
            type_label = " + ".join(band["label"] for band in bands)
        tooltip = _mission_tooltip(canonical, mission, type_label, inv_cache)
        missions_by_name[canonical] = {
            "name":          canonical,
            "tooltip":       tooltip,
            "url":           url,
            "type_priority": _TYPE_PRIORITY_MAP.get(best_type, 99),
            "mission_type":  best_type,
            "color":         color,
            "bands":         bands,
            "text_col":      text_col,
        }
    missions_data = sorted(
        missions_by_name.values(),
        key=lambda mission: (mission["type_priority"], mission["name"]),
    )

    # ── Render ────────────────────────────────────────────────────────────────
    html = _ENV.from_string(_PRETTY_TABLE_TEMPLATE).render(
        title_text=title_text,
        target_label=target_label,
        today=today,
        css=css,
        tip_js=tip_js,
        all_years=all_years,
        num_cols=num_cols,
        col_widths=col_widths,
        cat_col_width=cat_col_width,
        main_table_class=main_table_class,
        gb_table_class=gb_table_class,
        missions_data=missions_data,
        type_labels=type_labels_data,
        categories=categories_data,
        gb_categories=gb_categories_data,
        has_ground_based=has_ground_based,
        distinct_nodes=distinct_nodes,
        distinct_techniques=distinct_techniques,
        count_unit=count_unit,
    )
    with open(path, "w") as f:
        f.write(html)
    print(f"Pretty  written to: {path}")
