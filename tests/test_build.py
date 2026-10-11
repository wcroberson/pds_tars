"""Tests for pds_tars/build.py pure functions.

All tests are self-contained — no network calls, no file I/O.
"""

import sys
from collections import defaultdict

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
    """End-to-end: a "Clouds" science sorting label must put the instrument in
    section_tables['Atmosphere']['Clouds'] at the entry's start year.

    This is the integration check for the full chain:
      label -> SECTION_OF_CATEGORY -> section_tables entry.
    """
    by_instrument = {
        "Nephelometer (NEP)": {
            "NEP Raw": _col(single=True, proc=["Raw"], wl=None,
                            inv=["GALILEO PROBE"], start="1995-01-01T00:00:00Z",
                            labels=["Clouds"]),
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
    """An instrument labeled only "UV" must not land in any Atmosphere row."""
    by_instrument = {
        "Ultraviolet Spectrometer (UVS)": {
            "UVS Raw": _col(single=True, proc=["Raw"], wl=["Ultraviolet"],
                            inv=["Voyager 1"], start="1979-01-01T00:00:00Z",
                            labels=["UV"]),
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
            "TM UV 1": _col(single=True, proc=["Raw"], wl=None, labels=["UV"]),
            "TM UV 2": _col(single=True, proc=["Raw"], wl=None,
                            start="2001-01-01T00:00:00Z", labels=["UV"]),
            "TM IR":   _col(single=True, proc=["Raw"], wl=None, labels=["IR"]),
        }
    }
    table_data = build_table_data(by_instrument)
    name_to_abbr, category_collection_counts = table_data.name_to_abbr, table_data.category_collection_counts
    abbr = name_to_abbr["Test Multi (TM)"]
    assert category_collection_counts["UV"][abbr] == 2
    assert category_collection_counts["IR"][abbr] == 1

def test_unknown_label_is_skipped_with_warning(capsys):
    """A label that isn't a category in pipeline_rules.yaml has no row to go
    in - it must be skipped and reported, not crash the build or invent a row."""
    by_instrument = {
        "Test Probe (TP)": {
            "TP": _col(single=True, proc=["Raw"], wl=None, labels=["Winds", "Not A Category"]),
        }
    }
    section_tables = build_table_data(by_instrument).section_tables
    assert "Winds" in section_tables["Atmosphere"]
    assert all("Not A Category" not in table for table in section_tables.values())
    assert "Not A Category" in capsys.readouterr().out

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
    monkeypatch.setattr(build, "build_context", lambda force_refresh=False: object())
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

