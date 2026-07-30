"""
registry.py - PDS registry querying helpers.

Handles all interaction with the PDS Search API via peppi: building the
instrument name cache, parsing collection properties, and filtering
collections to science-only data products.
"""

import datetime
import json
import os
import re
import time

import pds.peppi as pep

from pds_tars.schema import MISSION_TYPES, MISSION_LANDING_PAGES, COLLECTION_DATE_OVERRIDES_BY_LID

# Package lives at <project_root>/src/pds_tars/ - cache/ is
# a sibling of src/, two directories up from this file.
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_HERE))

# ── Context-product cache ───────────────────────────────────────────────────────
# Caches the PDS context-product catalog (instruments, spacecraft, targets,
# ...) - avoids re-fetching it every run. Refresh with --refresh-contexts.
CONTEXT_CACHE_PATH = os.path.join(_PROJECT_ROOT, "cache", "context_cache.json")
CONTEXT_CACHE_MAX_AGE_SECS = 7 * 24 * 60 * 60

CONTEXT_TARGETS_CACHE_PATH = os.path.join(_PROJECT_ROOT, "cache", "context_targets_cache.json")
CONTEXT_TARGETS_CACHE_MAX_AGE_SECS = 7 * 24 * 60 * 60

INVESTIGATION_CACHE_PATH = os.path.join(_PROJECT_ROOT, "cache", "context_investigations_cache.json")
INVESTIGATION_CACHE_MAX_AGE_SECS = 7 * 24 * 60 * 60

# ── PDS4 property key constants ───────────────────────────────────────────────
# All confirmed against the live registry.

WL_KEY            = "pds:Science_Facets.pds:wavelength_range"
FACET1_KEY        = "pds:Science_Facets.pds:facet1"
FACET2_KEY        = "pds:Science_Facets.pds:facet2"
DISCIPLINE_KEY    = "pds:Science_Facets.pds:discipline_name"
DOMAIN_KEY        = "pds:Science_Facets.pds:domain"
PROC_KEY          = "pds:Primary_Result_Summary.pds:processing_level"
PURPOSE_KEY       = "pds:Primary_Result_Summary.pds:purpose"
START_KEY         = "pds:Time_Coordinates.pds:start_date_time"
STOP_KEY          = "pds:Time_Coordinates.pds:stop_date_time"
NODE_KEY          = "ops:Harvest_Info.ops:node_name"
INVESTIGATION_KEY = "pds:Investigation_Area.pds:name"
BUNDLE_KEY        = "ops:Provenance.ops:parent_bundle_identifier"
STATUS_KEY        = "ops:Tracking_Meta.ops:archive_status"
TITLE_KEY         = "pds:Identification_Area.pds:title"
INST_LID_KEY      = "ref_lid_instrument"
DESCRIPTION_KEY   = "pds:Citation_Information.pds:description"
COLLECTION_TYPE_KEY = "pds:Collection.pds:collection_type"

# Property keys for instrument context products (not collections).
INST_NAME_KEY = "pds:Instrument.pds:name"
CONTEXT_LID_KEY = "lid"

# Property keys for investigation context products.
INVEST_NAME_KEY  = "pds:Investigation.pds:name"
INVEST_TYPE_KEY  = "pds:Investigation.pds:type"
INVEST_START_KEY = "pds:Investigation.pds:start_date"
INVEST_STOP_KEY  = "pds:Investigation.pds:stop_date"
INVEST_DESC_KEY  = "pds:Investigation.pds:description"

# Used to mark missions and instruments that continue into the future
# as ongoing.
CURRENT_YEAR = datetime.date.today().year

# ── Collection filter constants ───────────────────────────────────────────────

DATA_TYPE = "Data"

ANCILLARY_PURPOSES = {
    "Engineering",
    "Observation Geometry",
    "Calibration",
    "Navigation",
}


# ── Small utilities ───────────────────────────────────────────────────────────

def scalar(v):
    """Returns the single value from a peppi property, or None if absent.

    Raises ValueError if peppi returns a list with more than one element,
    since that indicates an unexpected multi-valued property where we assumed
    singular.

    Args:
        v (list, scalar, or None): as returned by peppi properties.get().

    Returns (scalar or None):
        v itself if not a list; the sole element if a single-item list; None
        if v is None or an empty list.
    """
    if isinstance(v, list):
        if len(v) > 1:
            raise ValueError(f"expected scalar property, got {v!r}")
        return v[0] if v else None
    return v


def listify(v):
    """Normalizes a peppi property value to a list; returns None if absent or empty.

    Args:
        v (list, scalar, or None): as returned by peppi properties.get().

    Returns (list or None):
        v unchanged if already a non-empty list; [v] if a scalar; None if v is
        None or an empty list.  Callers that need an empty-list guarantee should
        use ``listify(v) or []``.
    """
    if v is None:
        return None
    if isinstance(v, list):
        return v or None
    return [v]


def _flatten_whitespace(text):
    """Collapses newlines/whitespace runs in free text to single spaces.

    Registry free-text fields (e.g. description) can contain embedded
    newlines, which break the one-line-per-field diagnostic output - collapse
    here before the text is written out.

    Args:
        text (string or None): free text to collapse.

    Returns (string or None):
        text with all whitespace runs collapsed to single spaces; if
        None, returns unchanged.
    """
    return " ".join(text.split()) if text else text


# ── LID / instrument name helpers ─────────────────────────────────────────────


# These words are skipped when building an abbreviation from an instrument name.
_SKIP_WORDS = {"the", "and", "for", "a", "to"}


def to_initials(text):
    """Reduces free text to a short uppercase tag: initials of each word
    (dropping skip words: the, and, for, a, to), or that one word
    itself if only one remains.

    Shared by abbreviate()'s word-initials fallback and by build.py's
    _probe_variant_tag()/_mission_tag()/_leading_words_tags() - all do the same
    "text -> short tag" operation and used to be separate implementations with
    different skip-word handling, which produced inconsistent tags for the
    same input (e.g. "TICE" vs "ICE" for "the International Cometary Explorer").

    Args:
        text (string): e.g. an instrument name fragment.

    Returns (string):
        the uppercase, alphanumeric-only tag; if text has no alphanumeric
        content at all, returns empty.
    """
    words = [word for word in text.split() if word.lower() not in _SKIP_WORDS]
    tag = "".join(word[0] for word in words) if len(words) > 1 else "".join(words)
    return re.sub(r'[^A-Za-z0-9]', '', tag).upper()


def abbreviate(name):
    """Turns an instrument display name into a short uppercase initials code.

    If the name contains a parenthesized acronym (all-caps/digits/hyphens,
    2-12 chars), use that directly - e.g. "Orbiter Electric Field Detector
    (OEFD)" -> OEFD.  Otherwise strip any parenthetical, drop short skip words
    (the, and, for, a, to), and convert to initials.

    A parenthesized acronym is trusted directly when the rest of the name has
    mixed case (PDS4 titles often prepend mission context ahead of the "real"
    instrument name the acronym was drawn from, so checking the acronym
    against the whole surrounding text would wrongly reject e.g. "...Alpha
    Particle X-ray Spectrometer (APXS)"). When the surrounding text is itself
    all-caps (e.g. "PIONEER VENUS SMALL PROBE (DAY) DIFFERENTIAL LONG BASE
    LINE INTERFEROMETER"), there's no case difference to signal a real
    acronym, so the parenthetical is only trusted if it's derived from the
    surrounding words - their initials (e.g. "FRS" from "DFRS") or embedded
    in one of them (e.g. "MAG" in "MPO-MAG"). "(DAY)"/"(NIGHT)"/"(NORTH)" are
    neither, so they fall through to the word-initials branch instead,
    leaving them available to build.py's _probe_variant_tag() as a
    collision suffix instead of becoming a bare abbreviation on their own.

    A trailing "for <MISSION>" is handled the same way when the word-initials
    branch is reached: many PDS3-era names append the spacecraft as a bare
    mission code (e.g. "Plasma Wave Receiver for GO") rather than a
    parenthetical, so that code is stripped before taking initials and left
    for build.py's _mission_tag() to use as a collision suffix instead - a
    mixed-case tail like "for Pioneer Venus Large Probe" is real instrument
    name and stays.

    Args:
        name (string): the instrument display name, e.g. "Near Infrared
              Mapping Spectrometer (NIMS)".

    Returns (string):
        the short uppercase code, e.g. "NIMS" or initials like "SSI".
    """
    m = re.search(r'\(([A-Z0-9-]{2,12})\)', name)
    if m:
        acronym = m.group(1)
        outside_paren = name[:m.start()] + name[m.end():]
        if any(char.islower() for char in outside_paren):
            return acronym
        acronym_chars = acronym.replace("-", "")
        tokens = [word for word in outside_paren.split() if word.lower() not in _SKIP_WORDS]
        initials = "".join(word[0] for word in tokens).upper()
        is_initialism = acronym_chars == initials
        is_embedded = any(
            acronym_chars in re.sub(r'[^A-Za-z0-9]', '', word).upper() for word in tokens
        )
        if is_initialism or is_embedded:
            return acronym
    clean = re.sub(r'\([^)]*\)', '', name).strip()
    if not clean:
        clean = re.sub(r'[()]', '', name).strip()
    without_mission_code = re.sub(r'\bfor\s+[A-Z][A-Z0-9]*$', '', clean, flags=re.IGNORECASE).rstrip()
    if without_mission_code:
        clean = without_mission_code
    result = to_initials(clean)
    if not result:
        raise ValueError(f"could not construct abbreviated name for {name!r}")
    return result


# ── Context-product caches ────────────────────────────────────────────────────

# These classes act as stand-ins for peppi's Context/Targets/Target objects
# (pep.Context, pep.Context().TARGETS, and an individual target,
# respectively). If new methods or attributes are accessed for those real
# objects in build.py, these classes will need to be modified correspondingly.
class _TargetProxy:
    """Minimal stand-in for a peppi Target object, restored from the cache."""
    __slots__ = ("lid", "name", "description", "type")

    def __init__(self, data):
        self.lid         = data.get("lid", "") or ""
        self.name        = data.get("name", "") or ""
        self.description = data.get("description", "") or ""
        self.type        = data.get("type", "") or ""


class _TargetsNamespace:
    """Stand-in for context.TARGETS - supports getattr by name and dir()."""

    def __init__(self, targets):
        for attr_name, data in targets.items():
            object.__setattr__(self, attr_name, _TargetProxy(data))
        object.__setattr__(self, "_attr_names", list(targets.keys()))

    def __dir__(self):
        return self._attr_names


class CachedContext:
    """Drop-in replacement for pep.Context() backed by the on-disk cache."""

    def __init__(self, targets):
        self.TARGETS = _TargetsNamespace(targets)


def _normalize_instrument_lid(lid, spacecraft_ids, facility_ids):
    """Returns lid with its final segment in instrument.host order.

    Old-style LIDs use host.instrument order; this flips them to the modern
    instrument.host form so the cache only needs one key per instrument.

    Args:
        lid (string): an instrument LID in either order.
        spacecraft_ids (set of strings): spacecraft host abbreviations used
              to detect old-style order.
        facility_ids (set of strings): ground-based facility abbreviations,
              used the same way as spacecraft_ids.

    Returns (string):
        lid with its final segment reordered to instrument.host; if already
        in that order, returns lid unchanged.
    """
    parts = lid.split(":")
    last = parts[-1].split(".")
    if len(last) == 2 and last[0].lower() in spacecraft_ids | facility_ids:
        parts[-1] = f"{last[1]}.{last[0]}"
        return ":".join(parts)
    return lid


def _load_context_cache():
    """Returns the on-disk (cache, spacecraft_ids, facility_ids) triple if present and not stale.

    Returns (tuple or None):
        a (cache, spacecraft_ids, facility_ids) tuple; if the cache is
        missing or stale, returns None.
    """
    if not os.path.exists(CONTEXT_CACHE_PATH):
        return None
    if time.time() - os.path.getmtime(CONTEXT_CACHE_PATH) > CONTEXT_CACHE_MAX_AGE_SECS:
        return None
    with open(CONTEXT_CACHE_PATH) as f:
        data = json.load(f)
    return data["cache"], set(data["spacecraft_ids"]), set(data["facility_ids"])


def _save_context_cache(cache, spacecraft_ids, facility_ids):
    """Writes the instrument-name cache to disk.

    Args:
        cache (dict): maps instrument LID to display name.
        spacecraft_ids (set): spacecraft host abbreviations seen in
              instrument_host LIDs.
        facility_ids (set): ground-based facility abbreviations seen in
              facility LIDs.
    """
    os.makedirs(os.path.dirname(CONTEXT_CACHE_PATH), exist_ok=True)
    with open(CONTEXT_CACHE_PATH, "w") as f:
        json.dump({
            "cache": cache,
            "spacecraft_ids": sorted(spacecraft_ids),
            "facility_ids": sorted(facility_ids),
        }, f, indent=2)


def _load_context_targets_cache():
    """Returns the on-disk targets dict if present and not stale.

    Returns (dict or None):
        target data keyed by attribute name; if the cache is missing or
        stale, returns None.
    """
    if not os.path.exists(CONTEXT_TARGETS_CACHE_PATH):
        return None
    if time.time() - os.path.getmtime(CONTEXT_TARGETS_CACHE_PATH) > CONTEXT_TARGETS_CACHE_MAX_AGE_SECS:
        return None
    with open(CONTEXT_TARGETS_CACHE_PATH) as f:
        return json.load(f).get("targets")


def _save_context_targets_cache(targets):
    """Writes the target-context-product cache to disk.

    Args:
        targets (dict): target data keyed by attribute name.
    """
    os.makedirs(os.path.dirname(CONTEXT_TARGETS_CACHE_PATH), exist_ok=True)
    with open(CONTEXT_TARGETS_CACHE_PATH, "w") as f:
        json.dump({"targets": targets}, f, indent=2)


def _load_investigation_cache():
    """Returns on-disk investigation data if present and not stale.

    Returns (dict or None):
        investigation data keyed by name (lowercase); if the cache is
        missing or stale, returns None.
    """
    if not os.path.exists(INVESTIGATION_CACHE_PATH):
        return None
    if time.time() - os.path.getmtime(INVESTIGATION_CACHE_PATH) > INVESTIGATION_CACHE_MAX_AGE_SECS:
        return None
    with open(INVESTIGATION_CACHE_PATH) as f:
        return json.load(f)


def _save_investigation_cache(data):
    """Writes the investigation cache to disk.

    Args:
        data (dict): investigation data keyed by name (lowercase).
    """
    os.makedirs(os.path.dirname(INVESTIGATION_CACHE_PATH), exist_ok=True)
    with open(INVESTIGATION_CACHE_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _refresh_context_product_caches(client, force_refresh=False):
    """Rebuilds the instrument-name and investigation caches together if either is stale.

    Both are filtered out of the exact same live pep.Products(client).contexts()
    call - the full PDS context-product catalog, just kept for different
    entry types (instrument vs. investigation) - so there's no extra
    registry cost to refreshing both whenever either one needs it.

    Staleness is checked internally (via each cache's own on-disk mtime, in
    _load_context_cache()/_load_investigation_cache()) - callers don't need
    to check it themselves. force_refresh only adds a manual override on top
    of that: it forces a live re-fetch even when both caches are still fresh.

    Args:
        client (peppi PDSRegistryClient): used for the live fetch.
        force_refresh (bool): if True, force a live re-fetch even if both
              caches are fresh.
    """
    if not force_refresh:
        if _load_context_cache() is not None and _load_investigation_cache() is not None:
            return

    all_contexts = list(pep.Products(client).contexts())

    # Instrument name cache:
    # Pass 1: collect spacecraft abbreviations from instrument_host context LIDs.
    # instrument_host LIDs end in <type>.<id>, e.g. "spacecraft.go" - the id
    # segment is the abbreviation that appears as the host part of old-style
    # instrument LIDs (host.instrument order rather than the modern instrument.host).
    spacecraft_ids = set()
    # Same idea, but for ground-based lab/facility hosts (e.g. RELAB, PAL) --
    # facility context LIDs use the same <type>.<id> final-segment shape,
    # under ":facility:" instead of ":instrument_host:".
    facility_ids = set()
    for context_product in all_contexts:
        lid = context_product.properties.get(CONTEXT_LID_KEY)
        if not lid:
            continue
        lid_str = scalar(lid)
        parts = lid_str.split(":")[-1].split(".")
        if ":instrument_host:" in lid_str:
            if len(parts) == 2:
                spacecraft_ids.add(parts[1].lower())
        elif ":facility:" in lid_str:
            if len(parts) == 2:
                facility_ids.add(parts[1].lower())

    # Pass 2: build name cache with normalized (instrument.host) keys.
    inst_cache = {}
    for context_product in all_contexts:
        name = context_product.properties.get(INST_NAME_KEY)
        lid  = context_product.properties.get(CONTEXT_LID_KEY)
        if name and lid:
            lid_str  = scalar(lid)
            name_str = scalar(name)
            if not re.search(r'\([A-Z0-9-]{2,12}\)', name_str):
                parts = lid_str.split(":")[-1].split(".")
                if len(parts) == 2:
                    first_part, second_part = parts
                    acronym = (second_part if first_part.lower() in spacecraft_ids | facility_ids else first_part).upper()
                    name_str = re.sub(r'( for .+)$', f' ({acronym})\\1', name_str)
                    if not re.search(r'\([A-Z0-9-]{2,12}\)', name_str):
                        name_str = f"{name_str} ({acronym})"
            normalized_lid = _normalize_instrument_lid(lid_str, spacecraft_ids, facility_ids)
            inst_cache[normalized_lid] = name_str
    _save_context_cache(inst_cache, spacecraft_ids, facility_ids)

    # dict: investigation name, lowercase (str) -> {name, type, description,
    # start_date, stop_date} (the shape build_investigation_cache() returns).
    investigations = {}
    for context_product in all_contexts:
        lid = scalar(context_product.properties.get(CONTEXT_LID_KEY)) or ""
        if ":investigation:" not in lid:
            continue
        name  = scalar(context_product.properties.get(INVEST_NAME_KEY)) or ""
        itype = scalar(context_product.properties.get(INVEST_TYPE_KEY)) or ""
        start = scalar(context_product.properties.get(INVEST_START_KEY)) or ""
        stop  = scalar(context_product.properties.get(INVEST_STOP_KEY)) or ""
        desc  = scalar(context_product.properties.get(INVEST_DESC_KEY)) or ""
        if not name:
            continue
        investigations[name.lower()] = {
            "name":        name,
            "type":        itype,
            "description": _flatten_whitespace(desc),
            "start_date":  start[:10] if start else "",
            "stop_date":   stop[:10] if stop else "",
        }
    _save_investigation_cache(investigations)


def build_investigation_cache(client=None, force_refresh=False):
    """Returns investigation context product data keyed by name (lowercase).

    Refreshes via _refresh_context_product_caches(), then reads back the
    on-disk cache at INVESTIGATION_CACHE_PATH. Note: that shared refresh also
    rebuilds the instrument-name cache if it's stale, since both are filtered
    from the same live fetch - see build_instrument_name_cache() and
    _refresh_context_product_caches(). This function's name describes its
    return value, not everything it may refresh as a side effect.

    Args:
        client (peppi PDSRegistryClient or None): pass None to create a
              default one.
        force_refresh (bool): if True, bypass the on-disk cache.

    Returns (dict):
        mapping investigation name (lowercase) -> {
            "name":        canonical name string,
            "type":        investigation type (e.g. "Mission"),
            "description": flattened description text,
            "start_date":  ISO date string or "",
            "stop_date":   ISO date string or "",
        }
    """
    if client is None:
        client = pep.PDSRegistryClient()
    _refresh_context_product_caches(client, force_refresh=force_refresh)
    return _load_investigation_cache()


def build_context(force_refresh=False):
    """Returns a CachedContext whose TARGETS namespace mirrors pep.Context().TARGETS.

    Fetches all target context products from the PDS registry on first call
    (or when the cache is stale / force_refresh=True), then serializes lid,
    name, description, and type for every target to
    CONTEXT_TARGETS_CACHE_PATH. Subsequent calls within
    CONTEXT_TARGETS_CACHE_MAX_AGE_SECS return instantly from the cache.

    Args:
        force_refresh (bool): if True, force a live re-fetch even if the
              cache is fresh.

    Returns (CachedContext):
        a CachedContext object with a TARGETS attribute built from
        CONTEXT_TARGETS_CACHE_PATH, to mirror pep.Context().
    """
    if not force_refresh:
        targets = _load_context_targets_cache()
        if targets is not None:
            return CachedContext(targets)

    ctx = pep.Context()
    # dict: attribute name (str) -> {lid, name, description, type} (the shape
    # _TargetProxy expects to read back).
    targets = {}
    for attr_name in dir(ctx.TARGETS):
        if attr_name.startswith("_"):
            continue
        target = getattr(ctx.TARGETS, attr_name)
        if not hasattr(target, "lid"):
            continue
        targets[attr_name] = {
            "lid":         getattr(target, "lid", "") or "",
            "name":        getattr(target, "name", "") or "",
            "description": getattr(target, "description", "") or "",
            "type":        getattr(target, "type", "") or "",
        }
    _save_context_targets_cache(targets)
    return CachedContext(targets)


def build_instrument_name_cache(client=None, force_refresh=False):
    """Returns a lookup table of instrument display names from PDS context products.

    Refreshes via _refresh_context_product_caches(), then reads back the
    on-disk cache at CONTEXT_CACHE_PATH. Pass all three return values
    together to instrument_components(). Note: that shared refresh also
    rebuilds the investigation cache if it's stale, since both are filtered
    from the same live fetch - see build_investigation_cache() and
    _refresh_context_product_caches(). This function's name describes its
    return value, not everything it may refresh as a side effect.

    Args:
        client (peppi PDSRegistryClient or None): pass None to create a
              default one.
        force_refresh (bool): if True, bypass the on-disk cache.

    Returns (tuple):
        (cache, spacecraft_ids, facility_ids):
        - cache: instrument LID (normalized to instrument.host order) ->
          display name. If a name has no parenthesized acronym in the
          registry, one is derived from the LID segment (e.g. "nims" ->
          "(NIMS)") so abbreviate() returns the canonical identifier rather
          than initials.
        - spacecraft_ids: spacecraft host abbreviations seen in
          instrument_host LIDs (e.g. {"go", "vgr1"}), used to distinguish
          old-style host.instrument LIDs from modern instrument.host ones.
        - facility_ids: ground-based facility abbreviations (e.g. {"relab",
          "pal"}), used the same way as spacecraft_ids.
    """
    if client is None:
        client = pep.PDSRegistryClient()
    _refresh_context_product_caches(client, force_refresh=force_refresh)
    return _load_context_cache()


def instrument_components(props, inst_name_cache, spacecraft_ids, facility_ids):
    """Returns display names for each instrument referenced by a collection.

    Args:
        props (dict): peppi Product.properties for a collection.
        inst_name_cache (dict): normalized instrument LID to display name,
              from build_instrument_name_cache().
        spacecraft_ids (set): spacecraft host abbreviations from
              build_instrument_name_cache(), used to normalize
              ref_lid_instrument values before lookup.
        facility_ids (set): ground-based facility abbreviations from
              build_instrument_name_cache(), used the same way as
              spacecraft_ids.

    Returns (list of strings):
        display name for each instrument LID in props[INST_LID_KEY]. Falls
        back to the last colon-segment of the LID when not in the cache.
    """
    inst_lids = listify(props.get(INST_LID_KEY)) or []
    return [
        inst_name_cache.get(_normalize_instrument_lid(lid, spacecraft_ids, facility_ids))
        or lid.split(":")[-1]
        for lid in inst_lids
    ]


# ── Collection parsing ────────────────────────────────────────────────────────

def collection_fields(col, target_label):
    """Builds the per-collection metadata dict from a peppi Product object.

    A collection matching pipeline_rules.yaml's collection_date_overrides
    (by exact LID) has its start/stop replaced with the corrected value
    there instead of the raw registry value.

    Args:
        col (peppi Product): represents a PDS4 collection.
        target_label (string): human-readable target name (e.g. "Jupiter")
              stored verbatim in the "target" field.

    Returns (dict):
        string keys mapping to scalar or list values extracted from
        col.properties.  Values are None when the property is absent.
    """
    props = col.properties
    start = scalar(props.get(START_KEY))
    stop = scalar(props.get(STOP_KEY))
    date_override = COLLECTION_DATE_OVERRIDES_BY_LID.get(col.id)
    if date_override:
        start = date_override.get("start", start)
        stop = date_override.get("stop", stop)
    return {
        "collection_lid":    col.id,
        "target":            target_label,
        "investigation":     listify(props.get(INVESTIGATION_KEY)),
        "pds_node":          scalar(props.get(NODE_KEY)),
        "start":             start,
        "stop":              stop,
        "processing_level":  listify(props.get(PROC_KEY)),
        "purpose":           listify(props.get(PURPOSE_KEY)),
        "wavelength_range":  listify(props.get(WL_KEY)),
        "discipline":        listify(props.get(DISCIPLINE_KEY)),
        "facet1":            listify(props.get(FACET1_KEY)),
        "facet2":            listify(props.get(FACET2_KEY)),
        "description":       _flatten_whitespace(scalar(props.get(DESCRIPTION_KEY))),
        "domain":            listify(props.get(DOMAIN_KEY)),
        "instrument_lid":    listify(props.get(INST_LID_KEY)),
        "parent_bundle_lid": listify(props.get(BUNDLE_KEY)),
        "archive_status":    scalar(props.get(STATUS_KEY)),
    }


def is_science_collection(col):
    """Returns True if this is a PDS4 Data-type collection.

    Args:
        col (peppi Product): a PDS4 collection.

    Returns (bool):
        True if pds:Collection.pds:collection_type == "Data".
    """
    return scalar(col.properties.get(COLLECTION_TYPE_KEY)) == DATA_TYPE


def is_ancillary_purpose(col):
    """Returns True if every one of this collection's purposes is ancillary.

    A collection with no purpose set, or with at least one non-ancillary
    purpose (e.g. mixed Science + Engineering), returns False.

    Args:
        col (peppi Product): a PDS4 collection.

    Returns (bool):
        True if col's purposes are all in ANCILLARY_PURPOSES (Engineering,
        Observation Geometry, Calibration, Navigation).
    """
    purposes = listify(col.properties.get(PURPOSE_KEY)) or []
    return bool(purposes) and all(purpose in ANCILLARY_PURPOSES for purpose in purposes)


# ── Mission-date-bounds check ──────────────────────────────────────────────────
# This section clamps or discards a collection's Time_Coordinates when they
# fall outside its parent mission's real operating window (investigation_cache,
# from build_investigation_cache()) - covers a placeholder date pair, a "still
# active" stop long after the mission ended, and a shared bundle stamping one
# date range across unrelated instruments.

# Reuses pipeline_rules.yaml's mission_types/mission_landing_pages "names"
# groups, rather than building a separate table, to resolve a collection's raw
# investigation name to investigation_cache's canonical key. Note this can
# still split one real mission's phases into separate groups where
# mission_types deliberately does (e.g. unscoped "Galileo" flyby vs
# planet-scoped "GALILEO ORBITER") - that's fine for this list's purpose
# (finding investigation_cache entries with real date data), but wrong for
# "is this the same mission" identity checks; see _MISSION_LANDING_PAGE_GROUPS
# below for that.
_MISSION_ALIAS_GROUPS = [
    {name.lower() for name in entry.get("names", [])}
    for entry in MISSION_TYPES + MISSION_LANDING_PAGES
    if entry.get("names")
]

# Same idea, but built from mission_landing_pages alone - every raw
# investigation-name variant for one real mission (e.g.
# "GALILEO ORBITER"/"GALILEO PROBE"/"Galileo" all really are Galileo,
# regardless of which spacecraft phase mission_types types them as) grouped
# together, since mission_landing_pages exists precisely to say "these names
# are the same mission, with one page." Used by investigation_identity() to
# check that one instrument's collections never name more than one real
# mission - _MISSION_ALIAS_GROUPS is the wrong list for that (see above).
_MISSION_LANDING_PAGE_GROUPS = [
    {name.lower() for name in entry.get("names", [])}
    for entry in MISSION_LANDING_PAGES
    if entry.get("names")
]


def _resolve_investigation_keys(name, investigation_cache):
    """Returns every investigation_cache key that could match a raw investigation name.

    Tries, in order, stopping at the first level that finds anything: (1) the
    name itself, lowercased, as an exact cache key; (2) every alias sharing a
    mission_types/mission_landing_pages group with it that's also a cache key;
    (3) every word-boundary-safe substring match (e.g. "Phoenix" matching cache
    key "mars phoenix") - not a bare substring match, which would wrongly match
    "hayabusa" against "hayabusa2 asteroid sample return mission" (a real,
    different mission). An ambiguous name returns every candidate rather than
    guessing one, since investigation_date_bounds() unions them permissively.

    Args:
        name (string): raw pds:Investigation_Area.pds:name value.
        investigation_cache (dict): from build_investigation_cache().

    Returns (list of strings):
        every matching investigation_cache key; empty if nothing resolves.
    """
    lower = name.lower()
    if lower in investigation_cache:
        return [lower]

    for group in _MISSION_ALIAS_GROUPS:
        if lower in group:
            matches = [alt for alt in group if alt in investigation_cache]
            if matches:
                return matches

    return [
        key for key in investigation_cache
        if key.startswith(lower + " ") or lower.startswith(key + " ")
    ]


def investigation_identity(name, investigation_cache):
    """Returns a stable, hashable identity for a raw investigation name.

    Two raw investigation-name strings that share an identity represent the
    same real mission, even when spelled or cased differently (e.g. "Galileo"
    and "GALILEO ORBITER") - used to check that one instrument's collections
    never name more than one real mission (see build_table_data() in
    build.py).

    Checks mission_landing_pages group membership first, before
    investigation_cache resolution: a mission's primary and extended-mission
    phases (e.g. "New Horizons" and "New Horizons Kuiper Belt Extended
    Mission 2") are each their own real Investigation context product with
    their own real date range, so _resolve_investigation_keys correctly
    treats them as separate cache entries for date-bounds purposes - but
    mission_landing_pages groups them as one mission on purpose (one page for
    all of them), and that deliberate choice should win here. Uses
    _MISSION_LANDING_PAGE_GROUPS rather than _MISSION_ALIAS_GROUPS, since the
    latter also folds in mission_types and so still splits a mission's
    deliberately-scoped phases (e.g. unscoped "Galileo" flyby vs
    planet-scoped "GALILEO ORBITER") that this check must NOT treat as two
    missions.

    Args:
        name (string): raw pds:Investigation_Area.pds:name value.
        investigation_cache (dict): from build_investigation_cache().

    Returns:
        a frozenset of the shared mission_landing_pages group if name belongs
        to one, else the first key from _resolve_investigation_keys(), else
        name.lower() itself.
    """
    lower = name.lower()
    for group in _MISSION_LANDING_PAGE_GROUPS:
        if lower in group:
            return frozenset(group)

    keys = _resolve_investigation_keys(name, investigation_cache)
    if keys:
        return keys[0]
    return lower


def investigation_date_bounds(name, investigation_cache):
    """Returns the permissive union of real operating years for a collection's investigation.

    name itself can still resolve to more than one investigation_cache entry
    when it's ambiguous (see _resolve_investigation_keys) - the collection is
    valid if it fits *any* of them, so the returned bounds are the widest
    start (earliest of all) and widest stop (latest of all, or None if any
    resolved investigation is itself still ongoing).

    Args:
        name (string or None): a collection's investigation name (its
              pds:Investigation_Area.pds:name value), or None if it has none.
        investigation_cache (dict): from build_investigation_cache().

    Returns (tuple or None):
        (earliest_start_year, latest_stop_year_or_None), or None if name
        doesn't resolve to an investigation_cache entry with a start_date.
    """
    if not name:
        return None
    bounds = []
    for key in _resolve_investigation_keys(name, investigation_cache):
        entry = investigation_cache[key]
        if not entry.get("start_date"):
            continue
        start_year = int(entry["start_date"][:4])
        stop_date = entry.get("stop_date") or ""
        stop_year = int(stop_date[:4]) if stop_date else None
        if stop_year is not None and stop_year > CURRENT_YEAR:
            stop_year = None  # investigation itself is still ongoing
        bounds.append((start_year, stop_year))

    if not bounds:
        return None
    earliest_start = min(bound[0] for bound in bounds)
    stops = [bound[1] for bound in bounds]
    latest_stop = None if any(stop is None for stop in stops) else max(stops)
    return (earliest_start, latest_stop)


def clamp_to_investigation_dates(fields, investigation_cache):
    """Clamps or discards a collection's dates that fall outside its investigation's real window.

    Mutates fields["start"]/["stop"] in place. Three cases:
      - No investigation resolves, or the collection has no start date: left
        untouched (nothing to check against).
      - The collection's [start, stop] has zero overlap with its
        investigation's real [start, stop] (e.g. a derived product dated
        years after its mission ended): both dates are discarded ("No Date"
        downstream) rather than clamped, since there's no real observation
        inside the mission window to anchor a clamped value to.
      - Partial overlap (including the collection's range being a strict
        superset of the mission's, like Magellan's 1965-3000 placeholder
        pair around its real 1989-1994): each violating end is clamped to the
        investigation's own boundary rather than discarded, since the other
        end of the range is real and worth keeping.

    Args:
        fields (dict): one collection's fields as returned by
              collection_fields() - mutated in place.
        investigation_cache (dict): from build_investigation_cache().
    """
    if not fields.get("start"):
        return
    investigation = fields.get("investigation") or []
    bounds = investigation_date_bounds(investigation[0] if investigation else None, investigation_cache)
    if bounds is None:
        return
    min_start, max_stop = bounds

    start_year = int(fields["start"][:4])
    stop_year = int(fields["stop"][:4]) if fields.get("stop") else None

    too_early = start_year < min_start
    too_late = stop_year is not None and max_stop is not None and stop_year > max_stop
    if not too_early and not too_late:
        return

    # Overlap test uses the collection's own stop when present, else its
    # start (a single dated day/point with no stop still has a real position
    # to check against the mission window).
    col_end_for_overlap = stop_year if stop_year is not None else start_year
    no_overlap = (start_year > (max_stop if max_stop is not None else float("inf"))
                  or col_end_for_overlap < min_start)
    if no_overlap:
        fields["start"] = None
        fields["stop"] = None
        return

    if too_early:
        fields["start"] = f"{min_start}-01-01T00:00:00Z"
    if too_late:
        fields["stop"] = f"{max_stop}-12-31T23:59:59Z"
