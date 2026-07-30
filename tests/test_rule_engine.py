"""Tests for pds_tars/rule_engine.py — categories_for_section() and rule evaluation.

All tests are self-contained; no network calls, no file I/O. Cover every
section (Atmosphere, Electromagnetic Spectrum, Particles & Fields) through
the one shared entry point, categories_for_section(), which reads row/rule
definitions from pipeline_rules.yaml rather than having per-section logic
hardcoded into the function itself.
"""

from pds_tars.rule_engine import categories_for_section, make_attrs
import pytest

# ── Category assignment across sections ────────────────────────────────────────

@pytest.mark.parametrize("section, attrs_kwargs, expected", [
    # Atmosphere section, title-keyword rules.
    ("Atmosphere", dict(title="Nephelometer (NEP)"), ["Clouds"]),
    ("Atmosphere", dict(title="Doppler Wind Experiment (DWE)"), ["Winds"]),
    ("Atmosphere", dict(title="Isotopic Mass Spectrometer (IMS)"), ["Isotopic Ratios"]),
    # Non-matching instrument returns empty.
    ("Atmosphere", dict(title="Imaging Camera (IC)"), []),
    # Existing sections still work through the same shared entry point.
    ("Particles & Fields", dict(discipline=["Fields"], facet1=["Magnetic"]), ["Magnetic Fields"]),
    # Section isolation: Atmosphere matches don't bleed into other sections.
    ("Electromagnetic Spectrum", dict(title="Nephelometer (NEP)"), []),
    ("Particles & Fields", dict(title="Nephelometer (NEP)"), []),
    ("Electromagnetic Spectrum", dict(title="Doppler Wind Experiment (DWE)"), []),
])
def test_categories_for_section_cases(section, attrs_kwargs, expected):
    attrs = make_attrs(**attrs_kwargs)
    assert categories_for_section(section, attrs) == expected

def test_cloud_in_title_maps_to_clouds():
    # "in" rather than equality: covers a title match returning a category
    # among possibly others, not just the single-category exact-list cases above.
    attrs = make_attrs(title="Cloud Particle Imager (CPI)")
    assert "Clouds" in categories_for_section("Atmosphere", attrs)

def test_ultraviolet_wavelength_maps_to_uv_in_em():
    """attribute_keyword on wavelength_range must still assign UV in EM section."""
    attrs = make_attrs(wavelength_range=["Ultraviolet"])
    assert "UV" in categories_for_section("Electromagnetic Spectrum", attrs)

# ── description field: attribute_keyword can search free-text descriptions ────

@pytest.mark.parametrize("title, description, expected", [
    ("Orbiter Atmospheric Drag (OAD) for Pioneer Venus",
     "The observational data include altitude, density, scale height.",
     ["Density"]),
    ("Gas Chromatograph for Pioneer Venus Large Probe",
     "This collection contains atmospheric composition data from the Gas Chromatograph.",
     ["Atmospheric Composition"]),
    # exclude_keyword must suppress Density for a solar-wind proton density
    # description. Title deliberately omits "wind" (the real OPA title
    # triggers the unrelated Winds title-keyword rule) so this isolates the
    # Density exclusion.
    ("Solar Plasma Analyzer (OPA) for Pioneer Venus",
     "Reduced solar wind flow speed and proton number density observed by the analyzer.",
     []),
    ("Net Flux Radiometer for Pioneer Venus Small Probe (Day)",
     "This collection contains lower atmospheric data from the Net Flux Radiometers.",
     []),
])
def test_description_keyword_cases(title, description, expected):
    attrs = make_attrs(title=title, description=description)
    assert categories_for_section("Atmosphere", attrs) == expected

# ── hardcoded_instruments: exact-name exception, not a substring match ────────

@pytest.mark.parametrize("title, expected", [
    ("Solar Flux Radiometer for Pioneer Venus Large Probe", ["IR"]),
    ("Backup Solar Flux Radiometer for Pioneer Venus Large Probe", []),
])
def test_hardcoded_instruments_requires_exact_name(title, expected):
    attrs = make_attrs(title=title)
    assert categories_for_section("Electromagnetic Spectrum", attrs) == expected

