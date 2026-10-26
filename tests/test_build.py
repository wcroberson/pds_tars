"""Tests for pds_tars/build.py pure functions.

All tests are self-contained — no network calls, no file I/O.
"""

import sys
from collections import defaultdict
from types import SimpleNamespace

import pytest
from pds_tars import build
from pds_tars.build import build_table_data

def _col(*, single, proc, wl, disc=None, inv=None, start="2000-01-01T00:00:00Z",
         facet1=None, facet2=None, labels=None):
    """Build a minimal bundle/collection fields dict for testing."""
    return {
        "_single_instrument": single,
        "processing_level":   proc,   # None or list[str]
        "wavelength_range":   wl,     # None or list[str]
        "discipline":         disc,
        "investigation":      inv,
        "start":              start,
        "facet1":             facet1,
        "facet2":             facet2,
        "science_sorting_labels": labels or [],
    }

# ── build_table_data(): abbreviation collision resolution ─────────────────────

def test_two_way_abbreviation_collision_resolved():
    """Two instrument names sharing a base abbreviation must get distinct abbrs."""
    by_instrument = {
        "Imaging Orbiter (IO)": {
            "IO-A": _col(single=True, proc=["Raw"], wl=None, inv=["Mission A"])
        },
        "Ionosphere Observer (IO)": {
            "IO-B": _col(single=True, proc=["Raw"], wl=None, inv=["Mission B"],
                         start="2001-01-01T00:00:00Z")
        },
    }
    name_to_abbr = build_table_data(by_instrument).name_to_abbr
    abbrs = list(name_to_abbr.values())
    assert len(set(abbrs)) == len(abbrs), f"Duplicate abbreviations: {abbrs}"

def test_three_way_abbreviation_collision_resolved():
    """Three instruments sharing a base abbreviation should get IO, IO2, IO3."""
    by_instrument = {
        "Imaging Orbiter (IO)":     {"A": _col(single=True, proc=["Raw"], wl=None)},
        "Ionosphere Observer (IO)": {"B": _col(single=True, proc=["Raw"], wl=None,
                                               start="2001-01-01T00:00:00Z")},
        "Infrared Optics (IO)":     {"C": _col(single=True, proc=["Raw"], wl=None,
                                               start="2002-01-01T00:00:00Z")},
    }
    name_to_abbr = build_table_data(by_instrument).name_to_abbr
    abbrs = list(name_to_abbr.values())
    assert len(set(abbrs)) == len(abbrs), f"Duplicate abbreviations: {abbrs}"
    assert len(abbrs) == 3

def test_instruments_without_start_date_skipped():
    """Collections missing a start date must not appear in the output tables."""
    by_instrument = {
        "Test Radar (TR)": {
            "TR No Date":    _col(single=True, proc=["Raw"], wl=None, start=None),
            "TR With Date":  _col(single=True, proc=["Raw"], wl=["Radio"],
                                  start="2000-01-01T00:00:00Z"),
        }
    }
    name_to_abbr = build_table_data(by_instrument).name_to_abbr
    # "Test Radar (TR)" must appear — it has at least one dated collection
    assert "Test Radar (TR)" in name_to_abbr

# ── build_table_data(): section_tables structure and category assignment ───────

def test_clouds_label_lands_in_atmosphere_clouds():
    """End-to-end: an "Atmosphere:Clouds" science sorting label must put the
    instrument in section_tables['Atmosphere']['Clouds'] at the entry's start
    year.

    This is the integration check for the full chain:
      label -> split_label() -> section_tables entry.
    """
    by_instrument = {
        "Nephelometer (NEP)": {
            "NEP Raw": _col(single=True, proc=["Raw"], wl=None,
                            inv=["GALILEO PROBE"], start="1995-01-01T00:00:00Z",
                            labels=["Atmosphere:Clouds"]),
        }
    }
    table_data = build_table_data(by_instrument)
    section_tables, name_to_abbr = table_data.section_tables, table_data.name_to_abbr
    assert "Clouds" in section_tables["Atmosphere"], (
        "Nephelometer should be assigned to Atmosphere > Clouds"
    )
    abbr = name_to_abbr["Nephelometer (NEP)"]
    assert abbr in section_tables["Atmosphere"]["Clouds"].get("1995", set()), (
        "Nephelometer abbreviation should appear in the Clouds/1995 cell"
    )

def test_em_instrument_does_not_appear_in_atmosphere():
    """An instrument labeled only "Electromagnetic Spectrum:UV" must not land in any Atmosphere row."""
    by_instrument = {
        "Ultraviolet Spectrometer (UVS)": {
            "UVS Raw": _col(single=True, proc=["Raw"], wl=["Ultraviolet"],
                            inv=["Voyager 1"], start="1979-01-01T00:00:00Z",
                            labels=["Electromagnetic Spectrum:UV"]),
        }
    }
    section_tables = build_table_data(by_instrument).section_tables
    assert "Atmosphere" not in section_tables, (
        "UV spectrometer should have no Atmosphere category rows"
    )

def test_category_collection_counts_scoped_per_category_not_instrument_total():
    """An instrument with 2 UV-labeled entries and 1 IR-labeled entry must show a
    count of 2 on its UV pill and 1 on its IR pill - not its 3-entry
    instrument-wide total on both.
    """
    by_instrument = {
        "Test Multi (TM)": {
            "TM UV 1": _col(single=True, proc=["Raw"], wl=None, labels=["Electromagnetic Spectrum:UV"]),
            "TM UV 2": _col(single=True, proc=["Raw"], wl=None,
                            start="2001-01-01T00:00:00Z", labels=["Electromagnetic Spectrum:UV"]),
            "TM IR":   _col(single=True, proc=["Raw"], wl=None, labels=["Electromagnetic Spectrum:IR"]),
        }
    }
    table_data = build_table_data(by_instrument)
    name_to_abbr, category_collection_counts = table_data.name_to_abbr, table_data.category_collection_counts
    abbr = name_to_abbr["Test Multi (TM)"]
    assert category_collection_counts["UV"][abbr] == 2
    assert category_collection_counts["IR"][abbr] == 1

def test_sections_and_rows_come_from_the_labels():
    """A section and category no list names still get a row: the labels are
    the vocabulary, display_order only orders them."""
    by_instrument = {
        "Test Probe (TP)": {
            "TP": _col(single=True, proc=["Raw"], wl=None,
                       labels=["Atmosphere:Winds", "New Section:New Category"]),
        }
    }
    section_tables = build_table_data(by_instrument).section_tables
    assert "Winds" in section_tables["Atmosphere"]
    assert "New Category" in section_tables["New Section"]
    assert list(section_tables) == ["Atmosphere", "New Section"], (
        "display_order sections come first, unlisted ones after"
    )

def test_label_with_no_section_is_shown_with_warning(capsys):
    """A label missing its "Section:" part must not crash or vanish - it goes
    under UNSECTIONED and is reported."""
    by_instrument = {"Test Probe (TP)": {"TP": _col(single=True, proc=["Raw"], wl=None, labels=["Winds"])}}
    section_tables = build_table_data(by_instrument).section_tables
    assert "Winds" in section_tables[build.UNSECTIONED]
    assert "'Winds' names no section" in capsys.readouterr().out

def test_technique_labels_become_tags_not_rows():
    """"Technique:..." labels tag the instrument's chips and make no rows."""
    by_instrument = {
        "Test Camera (TC)": {
            "TC 1": _col(single=True, proc=["Raw"], wl=None,
                         labels=["Electromagnetic Spectrum:Visual", "Technique:Imaging"]),
            "TC 2": _col(single=True, proc=["Raw"], wl=None, labels=["Technique:Imaging"]),
        }
    }
    table_data = build_table_data(by_instrument)
    abbr = table_data.name_to_abbr["Test Camera (TC)"]
    assert "Technique" not in table_data.section_tables
    assert table_data.abbr_to_techniques[abbr] == {"Imaging"}
    assert table_data.abbr_to_technique_counts[abbr] == {"Imaging": 2}

def test_instrument_naming_two_missions_warns_instead_of_failing(capsys):
    """Two real missions on one instrument must not stop the build."""
    investigation_cache = {
        "galileo": {"name": "Galileo", "type": "Mission", "description": "",
                    "start_date": "1989-10-18", "stop_date": "2003-09-21"},
        "voyager": {"name": "Voyager", "type": "Mission", "description": "",
                    "start_date": "1977-08-20", "stop_date": ""},
    }
    by_instrument = {
        "Test Probe (TP)": {
            "A": _col(single=True, proc=["Raw"], wl=None, inv=["Galileo"], labels=["Atmosphere:Winds"]),
            "B": _col(single=True, proc=["Raw"], wl=None, inv=["Voyager"], labels=["Atmosphere:Winds"]),
        }
    }
    table_data = build_table_data(by_instrument, investigation_cache=investigation_cache)
    assert "Winds" in table_data.section_tables["Atmosphere"]
    assert "more than one mission" in capsys.readouterr().out

def test_entry_with_no_labels_gives_no_rows():
    """A bundle whose sorting pairs don't name an instrument sorts nothing for it."""
    by_instrument = {"Test Probe (TP)": {"TP": _col(single=True, proc=["Raw"], wl=["Ultraviolet"])}}
    table_data = build_table_data(by_instrument)
    assert "Test Probe (TP)" in table_data.name_to_abbr
    assert not table_data.section_tables

# ── _add_instrument_entries(): pairing labels to instruments ──────────────────

def test_add_instrument_entries_gives_each_instrument_only_its_own_labels():
    """Two instruments on one bundle with different pairs must each get only
    the labels paired with their own LID."""
    by_instrument = defaultdict(dict)
    labels = {
        "urn:nasa:pds:context:instrument:a.sc": ["UV"],
        "urn:nasa:pds:context:instrument:b.sc": ["Winds", "Radio"],
    }
    build._add_instrument_entries(
        by_instrument, "Bundle", {"start": "2000-01-01T00:00:00Z"},
        ["urn:nasa:pds:context:instrument:a.sc", "urn:nasa:pds:context:instrument:b.sc",
         "urn:nasa:pds:context:instrument:c.sc"],
        labels,
        {"urn:nasa:pds:context:instrument:a.sc": "Instrument A (A)",
         "urn:nasa:pds:context:instrument:b.sc": "Instrument B (B)",
         "urn:nasa:pds:context:instrument:c.sc": "Instrument C (C)"},
        set(), set(),
    )
    assert by_instrument["Instrument A (A)"]["Bundle"]["science_sorting_labels"] == ["UV"]
    assert by_instrument["Instrument B (B)"]["Bundle"]["science_sorting_labels"] == ["Winds", "Radio"]
    assert by_instrument["Instrument C (C)"]["Bundle"]["science_sorting_labels"] == []

def test_add_instrument_entries_can_limit_entries_to_the_products_own_instruments():
    """With pairs_add_instruments=False, an instrument named only in the pairs
    gets no entry - a collection of instrument A must not date instrument B."""
    by_instrument = defaultdict(dict)
    build._add_instrument_entries(
        by_instrument, "Collection", {"start": "2000-01-01T00:00:00Z"},
        ["urn:nasa:pds:context:instrument:a.sc"],
        {"urn:nasa:pds:context:instrument:a.sc": ["UV"],
         "urn:nasa:pds:context:instrument:b.sc": ["Winds"]},
        {"urn:nasa:pds:context:instrument:a.sc": "Instrument A (A)",
         "urn:nasa:pds:context:instrument:b.sc": "Instrument B (B)"},
        set(), set(), pairs_add_instruments=False,
    )
    assert list(by_instrument) == ["Instrument A (A)"]

_IRTF_FIELDS = {
    "start": "1983-02-09T00:00:00Z",
    "observing_system": ["NASA InfraRed Telescope Facility (IRTF)"],
    "investigation": ["IRTF Photometry of Io"],
    "observatory_lids": ["urn:nasa:pds:context:telescope:irtf.3m0"],
}
_IRTF_LABELS = {"urn:nasa:pds:context:telescope:irtf.3m0": ["IR"]}

def test_no_instrument_product_listed_under_its_observing_system():
    """A bundle naming no instrument goes under its observing system, sorted by
    the pair on its telescope LID - and the telescope LID is never mistaken
    for an instrument."""
    by_instrument = defaultdict(dict)
    build._add_instrument_entries(
        by_instrument, "Bundle", _IRTF_FIELDS, [], _IRTF_LABELS, {}, set(), set(),
    )
    assert list(by_instrument) == ["NASA InfraRed Telescope Facility (IRTF) (no instrument)"]
    entry = by_instrument["NASA InfraRed Telescope Facility (IRTF) (no instrument)"]["Bundle"]
    assert entry["science_sorting_labels"] == ["IR"]

def test_observatory_entry_name_falls_back_to_investigation():
    fields = dict(_IRTF_FIELDS, observing_system=None)
    assert build._observatory_entry_name(fields) == "IRTF Photometry of Io (no instrument)"
    assert build._observatory_entry_name({}) == "(no instrument)"

# ── _process_target() / main(): CLI safety behavior ────────────────────────────

def test_process_target_raises_clear_error_for_unknown_target():
    """An unknown target name must raise ValueError, not a raw AttributeError."""

    class FakeTargets:
        pass  # deliberately has no attribute matching the target name below

    class FakeContext:
        TARGETS = FakeTargets()

    with pytest.raises(ValueError, match="unknown target"):
        build._process_target(
            "MADEUPPLANET",
            FakeContext(),
            client=None,
            node_filter="PDS_ATM",
            node_suffix="",
            inst_name_cache={},
            spacecraft_ids=set(),
            facility_ids=set(),
            investigation_cache={},
            omitted_lower=set(),
            peppi_name_overrides={},
            diagnostic=False,
        )

def test_main_isolates_per_target_failures_and_exits_nonzero(monkeypatch, capsys):
    """One bad target in a multi-target run must not block the others, but the
    process must still exit non-zero so a calling script can detect the
    partial failure."""
    targets = SimpleNamespace(VENUS=None, BADTARGET=None, JUPITER=None)
    monkeypatch.setattr(build, "build_context", lambda force_refresh=False: SimpleNamespace(TARGETS=targets))
    monkeypatch.setattr(build, "build_instrument_name_cache",
                        lambda client, force_refresh=False: ({}, set(), set()))
    monkeypatch.setattr(build, "build_investigation_cache",
                        lambda client, force_refresh=False: {})
    monkeypatch.setattr(build.pep, "PDSRegistryClient", lambda: object())
    monkeypatch.setattr(build, "_apply_registry_timeout", lambda client, timeout_secs=None: None)

    processed = []

    def fake_process_target(target_name, *args, **kwargs):
        processed.append(target_name)
        if target_name == "BADTARGET":
            raise RuntimeError("simulated failure")

    monkeypatch.setattr(build, "_process_target", fake_process_target)
    monkeypatch.setattr(sys, "argv", ["build.py", "VENUS", "BADTARGET", "JUPITER"])

    with pytest.raises(SystemExit) as exc_info:
        build.main()

    assert exc_info.value.code == 1
    assert processed == ["VENUS", "BADTARGET", "JUPITER"], (
        "all targets should be attempted even though one of them fails"
    )
    assert "BADTARGET" in capsys.readouterr().err


def test_unknown_target_refreshes_the_target_catalog_once(monkeypatch):
    """A target missing from the saved catalog may be newly archived: the
    catalog is refreshed once before the name is called unknown."""
    refreshes = []

    def fake_build_context(force_refresh=False):
        refreshes.append(force_refresh)
        names = {"VENUS": None} if not force_refresh else {"VENUS": None, "NEWMOON": None}
        return SimpleNamespace(TARGETS=SimpleNamespace(**names))

    monkeypatch.setattr(build, "build_context", fake_build_context)
    monkeypatch.setattr(build, "build_instrument_name_cache",
                        lambda client, force_refresh=False: ({}, set(), set()))
    monkeypatch.setattr(build, "build_investigation_cache",
                        lambda client, force_refresh=False: {})
    monkeypatch.setattr(build.pep, "PDSRegistryClient", lambda: object())
    monkeypatch.setattr(build, "_apply_registry_timeout", lambda client, timeout_secs=None: None)
    seen = []
    monkeypatch.setattr(build, "_process_target",
                        lambda target_name, context, *args, **kwargs: seen.append(hasattr(context.TARGETS, target_name)))

    assert build.run_pipeline(["NEWMOON"]) == []
    assert refreshes == [False, True]
    assert seen == [True]
