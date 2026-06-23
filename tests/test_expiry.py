"""v3 — DTE / expiry-awareness (§4): runs all 5 days, EXPIRY/PIN, no suppression.

Calendar reference (2026): Nifty expires Tue (23-Jun, 30-Jun);
Sensex expires Thu (18-Jun, 25-Jun).
"""
from datetime import date

from compute.expiry import (
    LABEL_ACTIVE,
    LABEL_LOW_WEIGHT,
    LABEL_NIFTY_ONLY,
    assess_from_dates,
    days_to_expiry,
)

NIFTY_TUE_1 = date(2026, 6, 23)
NIFTY_TUE_2 = date(2026, 6, 30)
SENSEX_THU_2 = date(2026, 6, 25)


def test_days_to_expiry():
    assert days_to_expiry(date(2026, 6, 23), date(2026, 6, 19)) == 4
    assert days_to_expiry(date(2026, 6, 18), date(2026, 6, 18)) == 0  # expiry day


def test_friday_both_clean():           # Nifty 4, Sensex 6 → active, no pin
    a = assess_from_dates(NIFTY_TUE_1, SENSEX_THU_2, date(2026, 6, 19))
    assert (a.nifty.dte, a.sensex.dte) == (4, 6)
    assert not a.nifty_pin and not a.sensex_pin and not a.low_weight
    assert not a.sensex_missing and a.label == LABEL_ACTIVE


def test_monday_nifty_near_expiry():    # Nifty 1, Sensex 3 → low weight, no pin
    a = assess_from_dates(NIFTY_TUE_1, SENSEX_THU_2, date(2026, 6, 22))
    assert (a.nifty.dte, a.sensex.dte) == (1, 3)
    assert not a.nifty_pin and a.low_weight and a.label == LABEL_LOW_WEIGHT


def test_tuesday_nifty_expiry_pins_no_suppression():   # Nifty 0 → pin, runs anyway
    a = assess_from_dates(NIFTY_TUE_1, SENSEX_THU_2, date(2026, 6, 23))
    assert (a.nifty.dte, a.sensex.dte) == (0, 2)
    assert a.nifty_pin and not a.sensex_pin and not a.sensex_missing
    assert "EXPIRY/PIN" in a.label and "Nifty" in a.label


def test_wednesday_sensex_near_expiry():      # Nifty ~6, Sensex 1 → low weight
    a = assess_from_dates(NIFTY_TUE_2, SENSEX_THU_2, date(2026, 6, 24))
    assert (a.nifty.dte, a.sensex.dte) == (6, 1)
    assert not a.sensex_pin and a.low_weight and a.label == LABEL_LOW_WEIGHT


def test_thursday_sensex_expiry_pins():        # Sensex 0 → pin Sensex
    a = assess_from_dates(NIFTY_TUE_2, SENSEX_THU_2, date(2026, 6, 25))
    assert (a.nifty.dte, a.sensex.dte) == (5, 0)
    assert a.sensex_pin and not a.nifty_pin
    assert "EXPIRY/PIN" in a.label and "Sensex" in a.label


def test_sensex_missing_is_nifty_only():
    a = assess_from_dates(NIFTY_TUE_1, None, date(2026, 6, 19))
    assert a.sensex is None and a.sensex_missing
    assert a.label == LABEL_NIFTY_ONLY
