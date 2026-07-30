"""Tests for pds_tars/render_html.py pure functions.

No network calls, no file I/O.
"""

import pytest

from pds_tars.render_html import _mission_tooltip

@pytest.mark.parametrize("name, inv_cache, expected_active_line", [
    # JUICE/ExoMars 2016-style investigation stop_date placeholder
    # (3000-01-01) must render as "ongoing", not a literal future year.
    ("ExoMars 2016",
     {"exomars 2016": {"start_date": "2016-01-07", "stop_date": "3000-01-01", "description": ""}},
     "Active: 2016–ongoing"),
    # Juno/Mars 2020-style investigations leave stop_date genuinely blank for
    # a still-active mission - shows just the start year, no dash.
    ("Juno",
     {"juno": {"start_date": "2011-08-05", "stop_date": "", "description": ""}},
     "Active: 2011"),
    ("Cassini-Huygens",
     {"cassini-huygens": {"start_date": "1997-10-15", "stop_date": "2017-09-15", "description": ""}},
     "Active: 1997–2017"),
])
def test_mission_tooltip_active_line_formatting(name, inv_cache, expected_active_line):
    tooltip = _mission_tooltip(name, None, "Orbiter", inv_cache)
    assert expected_active_line in tooltip.split("\n")

def test_mission_tooltip_far_future_stop_never_shown_literally():
    inv_cache = {"exomars 2016": {"start_date": "2016-01-07", "stop_date": "3000-01-01", "description": ""}}
    tooltip = _mission_tooltip("ExoMars 2016", None, "Orbiter", inv_cache)
    assert "3000" not in tooltip

def test_mission_tooltip_no_investigation_match_omits_active_line():
    tooltip = _mission_tooltip("Radio JOVE", None, "Ground-based", {})
    assert "Active:" not in tooltip

