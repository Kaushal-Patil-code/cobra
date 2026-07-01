"""v4 — config knobs for dynamic wall detection."""
from config.settings import Settings
from config.thresholds import WALL_SCAN_REACH_POINTS, WALL_STICKY_MARGIN


def test_sticky_margin_is_five_percent():
    assert WALL_STICKY_MARGIN == 0.05


def test_scan_reach_default_points():
    assert WALL_SCAN_REACH_POINTS == 400


def test_tick_strikecount_covers_sensex_window():
    # 15 strikes/side × 100 = ±1500 SENSEX pts ≥ the ~1280-pt scaled reach.
    assert Settings.model_fields["tick_strikecount"].default == 15
