"""v3 — strike-selection models (§3).

Replaces the old typed-zone model (`schemas/zones.py`). There is no human zone
input anymore: each index anchors a spot-derived ladder, and a `WallSelection` is
the locked outcome for one *side* of one index — the CAP (highest CE OI) or the
FLOOR (highest PE OI) of that ladder, plus its two neighbors.
"""
from __future__ import annotations

from datetime import date
from typing import List, Literal, Optional

from .base import ApiModel
from .market import IndexName, OptionType, Side


class WallSelection(ApiModel):
    """Locked wall + monitored neighbors for one side of one index (v3 §3)."""

    side: Side                    # CAP (CE) | FLOOR (PE)
    index_name: IndexName
    option_type: OptionType       # CE for CAP, PE for FLOOR
    expiry: date
    wall_strike: int              # highest-OI strike of option_type in the ladder
    wall_oi: int                  # its OI at lock time
    monitored: List[int]          # [wall-interval, wall, wall+interval], ascending
    interval: int


class MigrationFlag(ApiModel):
    """Result of checking whether the OI peak has drifted off the locked wall (§3)."""

    shifting: bool
    from_strike: int
    to_strike: Optional[int] = None
    direction: Optional[Literal["up", "down"]] = None
    detail: str
