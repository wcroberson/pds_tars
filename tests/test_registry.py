"""Tests for pds_tars/registry.py pure functions.

All fixture data (LIDs, names) is drawn from the live PDS registry and verified
correct at the time of writing.  No network calls are made here.
"""

import pytest
from pds_tars.registry import (
    abbreviate, scalar, listify,
    _normalize_instrument_lid, instrument_components, collection_fields,
    is_science_collection, is_ancillary_purpose, _resolve_investigation_keys,
    investigation_identity, investigation_date_bounds, clamp_to_investigation_dates,
    sorting_labels, latest_versions, parent_bundle_lids, bare_lid,
    fetch_products, observatory_lids, TELESCOPE_LID_KEY, FACILITY_LID_KEY,
    INST_LID_KEY, PURPOSE_KEY, PROC_KEY, BUNDLE_KEY, START_KEY, STOP_KEY,
    SORTING_INST_LID_KEY, SORTING_LABEL_KEY,
)

# Spacecraft host abbreviations sampled from the live registry via Pass 1 of
# build_instrument_name_cache().  go = Galileo, vg1/vg2 = Voyager, mpo = BepiColombo.
SPACECRAFT_IDS = {"go", "vg1", "vg2", "mpo"}

# Ground-based facility abbreviations sampled the same way, from the
# registry's :facility: context products (e.g. RELAB, PAL).
FACILITY_IDS = {"relab", "pal"}

# ── abbreviate() ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, expected", [
    # Parenthesized acronym is used directly.
    ("Near Infrared Mapping Spectrometer (NIMS)", "NIMS"),
    # A hyphenated acronym like "OEFD-2" must be matched whole, not truncated at the hyphen.
    ("Orbiter Electric Field Detector (OEFD-2)", "OEFD-2"),
    # The whole parenthetical must be stripped before taking initials, not just its
    # parens - otherwise "not an acronym" would leak in as words contributing to the result.
    ("Solid State Imaging System (not an acronym)", "SSIS"),
    # Stop words (the, and, for, a, to) must be excluded from the initials.
    ("The Cassini Radar", "CR"),
    # A bare trailing mission code ("for GO") is stripped, not folded into the
    # initials - it's reserved for build.py's collision-suffix logic
    # (_mission_tag()), which would produce "PWR-GO" only if "PWR" collides.
    ("Plasma Wave Receiver for GO", "PWR"),
    # A mixed-case tail after "for" is a real part of the instrument name, not
    # a bare mission code, and must not be stripped.
    ("Atmospheric Structure Experiment for Pioneer Venus Large Probe", "ASEPVLP"),
    ("NIMS", "NIMS"),
    # Real registry data: build_instrument_name_cache injects (NIMS) before the "for GO" suffix.
    ("Near Infrared Mapping Spectrometer (NIMS) for GO", "NIMS"),
    ("Europa Clipper Magnetometer (ECM)", "ECM"),
    ("The Cassini Radar (RADAR)", "RADAR"),
    ("MAss SPectrometer for Planetary EXploration (MASPEX)", "MASPEX"),
])
def test_abbreviate_cases(name, expected):
    assert abbreviate(name) == expected

def test_abbreviate_raises_on_empty_result():
    # A paren-stripping fallback lets "(foo bar)" yield "FB" rather than raising.
    # "()" still reaches ValueError because both the parenthetical strip and the
    # paren fallback leave clean="".
    with pytest.raises(ValueError):
        abbreviate("()")

# ── scalar() ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (["x"], "x"),
    ("x", "x"),
    (None, None),
    ([], None),
])
def test_scalar_cases(value, expected):
    assert scalar(value) == expected

def test_scalar_multi_value_raises():
    # scalar() must catch an unexpectedly multi-valued property loudly rather
    # than silently dropping values by picking one.
    with pytest.raises(ValueError):
        scalar(["a", "b"])

# ── listify() ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    (None, None),
    ([], None),
    ([1, 2], [1, 2]),
    ("x", ["x"]),
])
def test_listify_cases(value, expected):
    assert listify(value) == expected

# ── _normalize_instrument_lid() ───────────────────────────────────────────────

@pytest.mark.parametrize("lid, facility_ids, expected", [
    # go.nims is the real Galileo NIMS LID — host comes first (old-style) — must flip.
    ("urn:nasa:pds:context:instrument:go.nims", FACILITY_IDS, "urn:nasa:pds:context:instrument:nims.go"),
    # Already normalized (instrument.host) — left unchanged.
    ("urn:nasa:pds:context:instrument:nims.go", FACILITY_IDS, "urn:nasa:pds:context:instrument:nims.go"),
    ("urn:nasa:pds:context:instrument:vg1.pws", FACILITY_IDS, "urn:nasa:pds:context:instrument:pws.vg1"),
    ("urn:nasa:pds:context:instrument:mpo.bela", FACILITY_IDS, "urn:nasa:pds:context:instrument:bela.mpo"),
    ("urn:nasa:pds:context:instrument:bela.mpo", FACILITY_IDS, "urn:nasa:pds:context:instrument:bela.mpo"),
    # Only the final colon-segment is reordered; the "urn:nasa:pds:context:instrument:" prefix survives untouched.
    ("urn:nasa:pds:context:instrument:go.ssi", FACILITY_IDS, "urn:nasa:pds:context:instrument:ssi.go"),
    # relab.bc-ftir1 is a facility-hosted LID (host.instrument, old-style) —
    # facility_ids must be honored the same way spacecraft_ids is.
    ("urn:nasa:pds:context:instrument:relab.bc-ftir1", FACILITY_IDS, "urn:nasa:pds:context:instrument:bc-ftir1.relab"),
    # Without facility_ids, a facility-hosted host.instrument LID is left
    # unrecognized (the bug this fix addresses).
    ("urn:nasa:pds:context:instrument:relab.bc-ftir1", set(), "urn:nasa:pds:context:instrument:relab.bc-ftir1"),
])
def test_normalize_instrument_lid_cases(lid, facility_ids, expected):
    assert _normalize_instrument_lid(lid, SPACECRAFT_IDS, facility_ids) == expected

# ── instrument_components() ───────────────────────────────────────────────────

def test_instrument_components_normalizes_before_lookup():
    # ref_lid_instrument arrives as host.instrument (old-style);
    # instrument_components must normalize before cache lookup so it finds the name
    cache = {"urn:nasa:pds:context:instrument:nims.go": "Near Infrared Mapping Spectrometer (NIMS)"}
    props = {INST_LID_KEY: "urn:nasa:pds:context:instrument:go.nims"}
    result = instrument_components(props, cache, {"go"}, set())
    assert result == ["Near Infrared Mapping Spectrometer (NIMS)"]

def test_instrument_components_fallback_on_cache_miss():
    # LID not in cache → falls back to last colon-segment as the display name
    props = {INST_LID_KEY: "urn:nasa:pds:context:instrument:nims.go"}
    result = instrument_components(props, {}, set(), set())
    assert result == ["nims.go"]

def test_instrument_components_multiple_instruments():
    cache = {
        "urn:nasa:pds:context:instrument:nims.go": "Near Infrared Mapping Spectrometer (NIMS)",
        "urn:nasa:pds:context:instrument:ssi.go":  "Solid State Imaging System (SSI)",
    }
    props = {INST_LID_KEY: [
        "urn:nasa:pds:context:instrument:go.nims",
        "urn:nasa:pds:context:instrument:go.ssi",
    ]}
    result = instrument_components(props, cache, {"go"}, set())
    assert result == [
        "Near Infrared Mapping Spectrometer (NIMS)",
        "Solid State Imaging System (SSI)",
    ]

def test_instrument_components_facility_hosted():
    # A facility-hosted LID (e.g. RELAB) must normalize and look up the same
    # way a spacecraft-hosted one does, given facility_ids.
    cache = {"urn:nasa:pds:context:instrument:bc-ftir1.relab": "Bidirectional FTIR Spectrometer 1 (BC-FTIR1)"}
    props = {INST_LID_KEY: "urn:nasa:pds:context:instrument:relab.bc-ftir1"}
    result = instrument_components(props, cache, set(), {"relab"})
    assert result == ["Bidirectional FTIR Spectrometer 1 (BC-FTIR1)"]

def test_instrument_components_no_instruments():
    assert instrument_components({}, {}, set(), set()) == []

# ── collection_fields() ───────────────────────────────────────────────────────

class _MockCol:
    """Minimal stand-in for a peppi Product; only .properties and .id needed."""
    def __init__(self, properties, lid="test:col:lid"):
        self.properties = properties
        self.id = lid

def test_collection_fields_applies_pipeline_rules_date_override():
    # pipeline_rules.yaml's collection_date_overrides corrects the Voyager 2
    # LECP Uranus 24-second-scan collections' inverted start date (raw value
    # 1989-01-24, three years after its own stop) to the real 1986-01-24 -
    # confirm collection_fields() actually applies it for this real LID.
    col = _MockCol({
        START_KEY: "1989-01-24T00:00:00Z",
        STOP_KEY: "1986-01-27T23:56:54Z",
    }, lid="urn:nasa:pds:vg2-lecp-urn:data-scan-24sec-asc::1.0")
    fields = collection_fields(col, "Uranus")
    assert fields["start"] == "1986-01-24T00:00:00Z"
    assert fields["stop"] == "1986-01-27T23:56:54Z"  # untouched - override only lists "start"

def test_collection_fields_no_override_for_unlisted_lid():
    col = _MockCol({
        START_KEY: "1989-01-24T00:00:00Z",
        STOP_KEY: "1986-01-27T23:56:54Z",
    }, lid="urn:nasa:pds:some_other_bundle:data::1.0")
    fields = collection_fields(col, "Uranus")
    assert fields["start"] == "1989-01-24T00:00:00Z"
    assert fields["stop"] == "1986-01-27T23:56:54Z"

@pytest.mark.parametrize("properties, field_key, expected", [
    # Every listify()'d field must come back as a list (or None), never a
    # bare string — callers join/iterate it without a type check first.
    ({PROC_KEY: "Calibrated"}, "processing_level", ["Calibrated"]),
    ({}, "processing_level", None),
    ({PURPOSE_KEY: "Science"}, "purpose", ["Science"]),
    ({BUNDLE_KEY: "urn:nasa:pds:go_nims"}, "parent_bundle_lid", ["urn:nasa:pds:go_nims"]),
    ({}, "instrument_lid", None),
    ({INST_LID_KEY: "urn:nasa:pds:context:instrument:nims.go"}, "instrument_lid",
     ["urn:nasa:pds:context:instrument:nims.go"]),
])
def test_collection_fields_listify_behavior(properties, field_key, expected):
    fields = collection_fields(_MockCol(properties), "Jupiter")
    assert fields[field_key] == expected

# ── is_science_collection() ───────────────────────────────────────────────────

@pytest.mark.parametrize("collection_type, expected", [
    ("Context", False),
    ("Data", True),
])
def test_is_science_collection_cases(collection_type, expected):
    col = _MockCol({"pds:Collection.pds:collection_type": collection_type})
    assert is_science_collection(col) == expected

# ── is_ancillary_purpose() ──────────────────────────────────────────────────────

@pytest.mark.parametrize("purpose, expected", [
    ("Engineering", True),
    ("Observation Geometry", True),
    (["Engineering", "Observation Geometry"], True),
    # A collection with both Science and Engineering purposes must not be
    # flagged ancillary - only when ALL of a collection's purposes are ancillary.
    (["Science", "Engineering"], False),
    (None, False),
])
def test_is_ancillary_purpose_cases(purpose, expected):
    col = _MockCol({} if purpose is None else {PURPOSE_KEY: purpose})
    assert is_ancillary_purpose(col) == expected

# ── _resolve_investigation_keys() ──────────────────────────────────────────────

def test_resolve_investigation_keys_exact_match():
    cache = {"magellan": {"start_date": "1989-05-04", "stop_date": "1994-10-12"}}
    assert _resolve_investigation_keys("Magellan", cache) == ["magellan"]

def test_resolve_investigation_keys_via_pipeline_rules_alias_group():
    # "MAGELLAN" and "Magellan" share a names: [...] group in pipeline_rules.yaml
    # (mission_types/mission_landing_pages) - a raw investigation string using
    # either spelling must resolve to the same cache entry.
    cache = {"magellan": {"start_date": "1989-05-04", "stop_date": "1994-10-12"}}
    assert _resolve_investigation_keys("MAGELLAN", cache) == ["magellan"]

def test_resolve_investigation_keys_word_boundary_substring():
    # "Some Probe" isn't in any pipeline_rules.yaml alias group, so this can
    # only resolve via the word-boundary substring fallback, not the alias step.
    cache = {"some probe orbiter mission": {"start_date": "2007-08-04", "stop_date": "2008-11-10"}}
    assert _resolve_investigation_keys("Some Probe", cache) == ["some probe orbiter mission"]

def test_resolve_investigation_keys_rejects_bare_substring_collision():
    # "hayabusa" is a bare substring of "hayabusa2 asteroid sample return
    # mission" (a real, later, different mission) - a naive substring match
    # would wrongly attach Hayabusa 1's collections to Hayabusa 2's dates.
    # Word-boundary matching must prefer the real "hayabusa mission" entry.
    cache = {
        "hayabusa mission": {"start_date": "2003-05-09", "stop_date": "2010-06-13"},
        "hayabusa2 asteroid sample return mission": {"start_date": "2014-12-03", "stop_date": ""},
    }
    assert _resolve_investigation_keys("HAYABUSA", cache) == ["hayabusa mission"]

def test_resolve_investigation_keys_no_match_returns_empty():
    cache = {"magellan": {"start_date": "1989-05-04", "stop_date": "1994-10-12"}}
    assert _resolve_investigation_keys("Some Untracked Mission", cache) == []

def test_resolve_investigation_keys_ambiguous_returns_every_candidate():
    # A bare "Mariner" word-boundary-matches every real Mariner mission's cache
    # entry - rather than guessing one, every match is returned so the caller
    # can union their bounds permissively.
    cache = {
        "mariner 9":  {"start_date": "1971-05-30", "stop_date": "1972-10-27"},
        "mariner 67": {"start_date": "1967-06-14", "stop_date": "1967-10-21"},
        "mariner 69": {"start_date": "1969-02-24", "stop_date": "1969-08-05"},
    }
    assert _resolve_investigation_keys("Mariner", cache) == \
        ["mariner 9", "mariner 67", "mariner 69"]

# ── investigation_identity() ────────────────────────────────────────────────────

def test_investigation_identity_same_landing_page_group_for_name_variants():
    # "Galileo" and "GALILEO ORBITER" are two raw spellings PDS uses for the
    # same real mission - both must resolve to one identity, via their shared
    # mission_landing_pages group.
    cache = {"galileo": {"start_date": "1989-10-18", "stop_date": "2003-09-21"}}
    assert investigation_identity("Galileo", cache) == investigation_identity("GALILEO ORBITER", cache)

def test_investigation_identity_same_mission_despite_different_mission_types_group():
    # mission_types deliberately keeps unscoped "Galileo" (flyby) and
    # planet-scoped "GALILEO ORBITER" (orbiter) in separate groups - that's
    # real and correct for mission_type resolution, but investigation_identity
    # must still treat them as one mission (see _MISSION_LANDING_PAGE_GROUPS
    # vs _MISSION_ALIAS_GROUPS). Also covers "New Horizons" vs its Kuiper Belt
    # Extended Mission phases, each a separate real Investigation context
    # product (own cache entry, own date range) that mission_landing_pages
    # still groups as one mission on purpose.
    assert investigation_identity("Galileo", {}) == investigation_identity("GALILEO ORBITER", {})
    cache = {
        "new horizons": {"start_date": "2006-01-19", "stop_date": "3000-01-01"},
        "new horizons kuiper belt extended mission 2": {"start_date": "2021-01-01", "stop_date": "3000-01-01"},
    }
    assert investigation_identity("New Horizons", cache) == \
        investigation_identity("New Horizons Kuiper Belt Extended Mission 2", cache)

def test_investigation_identity_differs_for_different_missions():
    cache = {
        "mariner 9": {"start_date": "1971-05-30", "stop_date": "1972-10-27"},
        "mars global surveyor": {"start_date": "1996-11-07", "stop_date": "2006-11-02"},
    }
    assert investigation_identity("Mariner 9", cache) != investigation_identity("Mars Global Surveyor", cache)

def test_investigation_identity_falls_back_to_alias_group_without_cache_entry():
    # Not every real mission has a build_investigation_cache() entry (the
    # cache only has what's actually been fetched) - an alias-group match
    # still gives two name variants the same identity.
    cache = {}
    assert investigation_identity("Magellan", cache) == investigation_identity("MAGELLAN", cache)

def test_investigation_identity_falls_back_to_lowercase_name():
    cache = {}
    assert investigation_identity("Some Untracked Mission", cache) == "some untracked mission"

# ── investigation_date_bounds() ────────────────────────────────────────────────

def test_investigation_date_bounds_single_investigation():
    cache = {"magellan": {"start_date": "1989-05-04", "stop_date": "1994-10-12"}}
    assert investigation_date_bounds("Magellan", cache) == (1989, 1994)

def test_investigation_date_bounds_still_ongoing_stop_is_none():
    # A far-future stop_date (the investigation-context-product equivalent of
    # Juno's real "no end date yet" convention) means no real upper bound.
    cache = {"juno": {"start_date": "2011-08-05", "stop_date": "3000-01-01"}}
    assert investigation_date_bounds("Juno", cache) == (2011, None)

def test_investigation_date_bounds_ambiguous_name_permissive_union():
    # One raw name resolving to more than one investigation_cache entry (see
    # _resolve_investigation_keys) is valid if it fits ANY of them - bounds
    # are the widest start and widest stop.
    cache = {
        "some probe 1": {"start_date": "1976-08-07", "stop_date": "1980-07-25"},
        "some probe 2": {"start_date": "1996-11-07", "stop_date": "2006-11-02"},
    }
    assert investigation_date_bounds("Some Probe", cache) == (1976, 2006)

def test_investigation_date_bounds_unresolvable_returns_none():
    assert investigation_date_bounds("Untracked Mission", {}) is None

def test_investigation_date_bounds_no_name_returns_none():
    assert investigation_date_bounds(None, {"magellan": {"start_date": "1989-05-04"}}) is None

# ── clamp_to_investigation_dates() ─────────────────────────────────────────────

def test_clamp_noop_when_no_start():
    fields = {"start": None, "stop": None, "investigation": ["Magellan"]}
    clamp_to_investigation_dates(fields, {"magellan": {"start_date": "1989-05-04", "stop_date": "1994-10-12"}})
    assert fields == {"start": None, "stop": None, "investigation": ["Magellan"]}

def test_clamp_noop_when_investigation_unresolvable():
    fields = {"start": "1965-01-01T00:00:00Z", "stop": "3000-01-01T00:00:00Z", "investigation": ["Untracked"]}
    clamp_to_investigation_dates(fields, {})
    assert fields["start"] == "1965-01-01T00:00:00Z"
    assert fields["stop"] == "3000-01-01T00:00:00Z"

def test_clamp_partial_overlap_clamps_only_bad_end():
    # Real start (2018) is inside the mission window; only the far-future
    # placeholder stop needs clamping - the real start must be preserved,
    # not discarded, since discarding it would lose real information.
    fields = {"start": "2018-05-05T00:00:00Z", "stop": "2028-01-01T00:00:00Z",
              "investigation": ["InSight"]}
    clamp_to_investigation_dates(fields, {"insight": {"start_date": "2018-05-05", "stop_date": "2022-12-15"}})
    assert fields["start"] == "2018-05-05T00:00:00Z"
    assert fields["stop"] == "2022-12-31T23:59:59Z"

def test_clamp_superset_clamps_both_ends():
    # Magellan's real placeholder pair: the collection's range is a strict
    # superset of the mission's real window on both ends.
    fields = {"start": "1965-01-01T00:00:00.000Z", "stop": "3000-01-01T00:00:00.000Z",
              "investigation": ["Magellan"]}
    clamp_to_investigation_dates(fields, {"magellan": {"start_date": "1989-05-04", "stop_date": "1994-10-12"}})
    assert fields["start"] == "1989-01-01T00:00:00Z"
    assert fields["stop"] == "1994-12-31T23:59:59Z"

def test_clamp_full_non_overlap_discards_both():
    # A derived product dated entirely after its mission ended (no overlap at
    # all) has nothing real to clamp to - both dates are discarded rather
    # than fabricating a clamped value with no basis.
    fields = {"start": "2022-06-14T00:00:00Z", "stop": "2024-09-29T00:00:00Z",
              "investigation": ["Cassini-Huygens"]}
    clamp_to_investigation_dates(fields, {"cassini-huygens": {"start_date": "1997-10-15", "stop_date": "2017-09-15"}})
    assert fields["start"] is None
    assert fields["stop"] is None

def test_clamp_real_ongoing_mission_untouched():
    # Juno's genuinely still-active instruments: real start, far-future stop,
    # and the investigation itself is still ongoing - nothing should change.
    fields = {"start": "2016-07-10T08:16:47Z", "stop": "3000-01-01T00:00:00.000Z",
              "investigation": ["Juno"]}
    clamp_to_investigation_dates(fields, {"juno": {"start_date": "2011-08-05", "stop_date": "3000-01-01"}})
    assert fields["start"] == "2016-07-10T08:16:47Z"
    assert fields["stop"] == "3000-01-01T00:00:00.000Z"


# ── Bundle sorting pairs ──────────────────────────────────────────────────────
# LIDs from the pvmp_dlbi bundle (four Pioneer Venus probe DLBI instruments).

LP_DLBI  = "urn:nasa:pds:context:instrument:pvmp.lp.dlbi"
DAY_DLBI = "urn:nasa:pds:context:instrument:pvmp.sp-day.dlbi"


def test_sorting_labels_pairs_lists_by_position():
    """Item N of the LID list goes with item N of the label list, and an
    instrument repeated across pairs collects all of its labels."""
    props = {
        SORTING_INST_LID_KEY: [LP_DLBI, LP_DLBI, DAY_DLBI],
        SORTING_LABEL_KEY:    ["Radio", "Winds", "Winds"],
    }
    assert sorting_labels(props, SPACECRAFT_IDS, FACILITY_IDS) == {
        LP_DLBI: ["Radio", "Winds"],
        DAY_DLBI: ["Winds"],
    }


def test_sorting_labels_drops_repeated_pairs():
    """The same instrument/label pair written twice counts once."""
    props = {SORTING_INST_LID_KEY: [LP_DLBI, LP_DLBI], SORTING_LABEL_KEY: ["Radio", "Radio"]}
    assert sorting_labels(props, SPACECRAFT_IDS, FACILITY_IDS) == {LP_DLBI: ["Radio"]}


def test_sorting_labels_rejects_lists_of_different_lengths():
    """Unequal lists can't be paired safely - every label after the gap would
    attach to the wrong instrument - so they must raise instead."""
    props = {SORTING_INST_LID_KEY: [LP_DLBI, DAY_DLBI], SORTING_LABEL_KEY: ["Radio"]}
    with pytest.raises(ValueError, match="pair up one to one"):
        sorting_labels(props, SPACECRAFT_IDS, FACILITY_IDS)


def test_sorting_labels_normalizes_old_style_lids():
    """An old-style host.instrument LID in a pair is keyed the same way as the
    modern instrument.host form, so it matches the bundle's own references."""
    props = {SORTING_INST_LID_KEY: ["urn:nasa:pds:context:instrument:go.nims"],
             SORTING_LABEL_KEY:    ["IR"]}
    assert sorting_labels(props, SPACECRAFT_IDS, FACILITY_IDS) == {
        "urn:nasa:pds:context:instrument:nims.go": ["IR"],
    }


def test_sorting_labels_empty_when_bundle_has_no_pairs():
    assert sorting_labels({}, SPACECRAFT_IDS, FACILITY_IDS) == {}


class _FakeProduct:
    def __init__(self, lidvid):
        self.id = lidvid
        self.properties = {}


def test_latest_versions_keeps_newest_version_per_lid():
    """1.10 is newer than 1.9 - versions compare as numbers, not text."""
    products = [_FakeProduct("urn:nasa:pds:pv_sed::1.9"),
                _FakeProduct("urn:nasa:pds:pv_sed::1.10"),
                _FakeProduct("urn:nasa:pds:pvmp_dlbi::1.0")]
    kept = sorted(product.id for product in latest_versions(products))
    assert kept == ["urn:nasa:pds:pv_sed::1.10", "urn:nasa:pds:pvmp_dlbi::1.0"]


def test_parent_bundle_lids_prefers_registry_field_then_falls_back_to_lid():
    listed = {"parent_bundle_lid": ["urn:nasa:pds:pvmp_dlbi::1.0"],
              "collection_lid": "urn:nasa:pds:other:data"}
    assert parent_bundle_lids(listed) == {"urn:nasa:pds:pvmp_dlbi"}
    derived = {"parent_bundle_lid": None, "collection_lid": "urn:nasa:pds:pvmp_dlbi:data"}
    assert parent_bundle_lids(derived) == {"urn:nasa:pds:pvmp_dlbi"}


def test_bare_lid_strips_version():
    assert bare_lid("urn:nasa:pds:pvmp_dlbi::1.0") == "urn:nasa:pds:pvmp_dlbi"


# ── fetch_products(): asking for named fields only ───────────────────────────

def test_fetch_products_asks_for_fields_and_drops_null_values():
    """A requested property a product doesn't have comes back as ["null"]; it
    must be removed so the rest of the pipeline sees it as absent."""

    class FakeProduct:
        def __init__(self, props):
            self.properties = props

    class FakeQuery:
        def fields(self, fields):
            self.asked = fields
            return [FakeProduct({START_KEY: ["null"], STOP_KEY: ["2001-01-01T00:00:00Z"]})]

    query = FakeQuery()
    products = fetch_products(lambda: query, [START_KEY, STOP_KEY])
    assert query.asked == [START_KEY, STOP_KEY]
    assert products[0].properties == {STOP_KEY: ["2001-01-01T00:00:00Z"]}

def test_observatory_lids_lists_telescopes_then_facilities():
    """Values from the irtf_io_photometry bundle record."""
    props = {TELESCOPE_LID_KEY: ["urn:nasa:pds:context:telescope:irtf.3m0"],
             FACILITY_LID_KEY: ["urn:nasa:pds:context:facility:observatory.irtf"]}
    assert observatory_lids(props) == ["urn:nasa:pds:context:telescope:irtf.3m0",
                                       "urn:nasa:pds:context:facility:observatory.irtf"]
    assert observatory_lids({}) == []

# ── Context caches: falling back to an old copy when the refresh fails ──────

import json
import os

from pds_tars import registry


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    """Registry retries wait between attempts; tests of failures shouldn't."""
    monkeypatch.setattr(registry, "REGISTRY_RETRY_WAIT_SECS", 0)


def _write_old_json(path, data):
    """Writes data to path as JSON and backdates its mtime past every cache max age."""
    with open(path, "w") as f:
        json.dump(data, f)
    old = os.path.getmtime(path) - 30 * 24 * 60 * 60
    os.utime(path, (old, old))


def _failing_registry_call(*args, **kwargs):
    raise RuntimeError("(503)\nReason: Service Temporarily Unavailable")


class _FailingProducts:
    def __init__(self, client):
        pass

    def contexts(self):
        _failing_registry_call()


def test_build_context_uses_old_targets_cache_when_refresh_fails(tmp_path, monkeypatch, capsys):
    path = tmp_path / "context_targets_cache.json"
    _write_old_json(path, {"targets": {"VENUS": {"lid": "urn:nasa:pds:context:target:planet.venus",
                                                 "name": "Venus", "description": "", "type": "Planet"}}})
    monkeypatch.setattr(registry, "CONTEXT_TARGETS_CACHE_PATH", str(path))
    monkeypatch.setattr(registry.pep, "Context", _failing_registry_call)

    context = registry.build_context()

    assert context.TARGETS.VENUS.lid == "urn:nasa:pds:context:target:planet.venus"
    assert "NOTE: could not refresh context_targets_cache.json ((503))" in capsys.readouterr().out


def test_build_context_raises_when_refresh_fails_and_no_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "CONTEXT_TARGETS_CACHE_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(registry.pep, "Context", _failing_registry_call)

    with pytest.raises(registry.RegistryUnavailable):
        registry.build_context()


def test_context_product_caches_use_old_copies_when_refresh_fails(tmp_path, monkeypatch, capsys):
    inst_path = tmp_path / "context_cache.json"
    invest_path = tmp_path / "context_investigations_cache.json"
    _write_old_json(inst_path, {"cache": {"urn:nasa:pds:context:instrument:nims.go": "NIMS"},
                                "spacecraft_ids": ["go"], "facility_ids": ["relab"]})
    _write_old_json(invest_path, {"galileo": {"name": "Galileo", "type": "Mission", "description": "",
                                              "start_date": "1989-10-18", "stop_date": "2003-09-21"}})
    monkeypatch.setattr(registry, "CONTEXT_CACHE_PATH", str(inst_path))
    monkeypatch.setattr(registry, "INVESTIGATION_CACHE_PATH", str(invest_path))
    monkeypatch.setattr(registry.pep, "Products", _FailingProducts)

    cache, spacecraft_ids, facility_ids = registry.build_instrument_name_cache(client=object())
    investigations = registry.build_investigation_cache(client=object())

    assert cache == {"urn:nasa:pds:context:instrument:nims.go": "NIMS"}
    assert spacecraft_ids == {"go"} and facility_ids == {"relab"}
    assert investigations["galileo"]["name"] == "Galileo"
    out = capsys.readouterr().out
    assert "NOTE: could not refresh context_cache.json" in out
    assert "NOTE: could not refresh context_investigations_cache.json" in out


def test_context_product_caches_raise_when_refresh_fails_and_a_cache_is_missing(tmp_path, monkeypatch):
    inst_path = tmp_path / "context_cache.json"
    _write_old_json(inst_path, {"cache": {}, "spacecraft_ids": [], "facility_ids": []})
    monkeypatch.setattr(registry, "CONTEXT_CACHE_PATH", str(inst_path))
    monkeypatch.setattr(registry, "INVESTIGATION_CACHE_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(registry.pep, "Products", _FailingProducts)

    with pytest.raises(registry.RegistryUnavailable):
        registry.build_instrument_name_cache(client=object())


# ── fetch_products(): retries and saved results ──────────────────────────────

class _Query:
    """A peppi query stand-in: fails `failures` times, then returns products."""

    def __init__(self, failures, products):
        self.failures = failures
        self.products = products
        self.calls = 0

    def make(self):
        return self

    def fields(self, fields):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("(504)\nReason: Gateway Time-out")
        return self.products


def test_fetch_products_retries_a_failing_request(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_RESULTS_DIR", str(tmp_path))
    query = _Query(failures=2, products=[registry.SavedProduct("urn:x::1.0", {START_KEY: ["2000"]})])
    products = registry.fetch_products(query.make, [START_KEY])
    assert query.calls == 3
    assert products[0].id == "urn:x::1.0"


def test_fetch_products_falls_back_to_the_saved_result(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(registry, "REGISTRY_RESULTS_DIR", str(tmp_path))
    ok = _Query(failures=0, products=[registry.SavedProduct("urn:x::1.0", {START_KEY: ["2000"]})])
    registry.fetch_products(ok.make, [START_KEY], saved_as="venus_bundles")

    down = _Query(failures=99, products=[])
    products = registry.fetch_products(down.make, [START_KEY], saved_as="venus_bundles")
    assert [(product.id, product.properties) for product in products] == [("urn:x::1.0", {START_KEY: ["2000"]})]
    assert "using the results saved" in capsys.readouterr().out


def test_fetch_products_explains_when_nothing_is_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "REGISTRY_RESULTS_DIR", str(tmp_path))
    down = _Query(failures=99, products=[])
    with pytest.raises(registry.RegistryUnavailable, match="registry's side"):
        registry.fetch_products(down.make, [START_KEY], saved_as="venus_bundles")
