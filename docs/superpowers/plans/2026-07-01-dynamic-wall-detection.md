# Dynamic Wall Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect CAP/FLOOR walls from a wider full-chain scan (not just the 8 displayed rungs), tune the hysteresis to 5%, and add a sticky BROKEN badge + off-ladder callout row.

**Architecture:** Wall *detection* decouples from the 8-rung *display*: `select_wall` scans full-chain strikes in a per-index spot window (`[spot, spot+reach]` CE / `[spot−reach, spot]` PE, reach scaled by the live ratio). A wall that lands off the visible ladder still carries the verdict and surfaces as a single paired callout row. Spot crossing a wall re-picks immediately and persists a sticky `broken_level` that the UI shows until spot pulls back.

**Tech Stack:** Python 3.12 / Pydantic v2 / psycopg3 / Supabase Postgres; Next.js 14 (`frontend-next`); pytest.

## Global Constraints

- Hysteresis switch threshold: **`WALL_STICKY_MARGIN = 0.05`** (switch only if challenger OI ≥ incumbent × 1.05).
- Scan reach (per index, own points): **NIFTY = `WALL_SCAN_REACH_POINTS = 400`**; **SENSEX = `400 × (sensex_spot / nifty_spot)`**, fallback **`400 × instruments[name].price_mult`** (NIFTY 1.0, SENSEX 3.20) when a spot is missing.
- Fetch width: **`tick_strikecount = 15`** (covers SENSEX's ~1280-pt window).
- Broken level tracked = the **first** level that broke; cleared only when spot pulls back past it.
- Callout = **single paired row** (off-ladder NIFTY wall ↔ off-ladder SENSEX wall, Aligned/Divergent).
- Strength/dominance stays measured against the **8 visible rungs** (deliberate).
- Migration `db/migrations/v4_wall_dynamics.sql` MUST be applied before the code deploy; `lock_walls` graceful-degrades on `psycopg.errors.UndefinedColumn` so a missed migration never refreezes walls.
- Never commit/push secrets: `.env`, `.env.*` (keep `.env.example`), `*.log`, `.venv`, `node_modules` stay gitignored.
- Run tests with: `./.venv/Scripts/python.exe -m pytest tests/ -q` (cwd `e:/Kaushal/COBRA`).
- Branch: `feat/dynamic-wall-detection` (already created; spec committed at `d140ce8`).

---

### Task 1: Config constants

**Files:**
- Modify: `config/thresholds.py:40` (`WALL_STICKY_MARGIN`) + add `WALL_SCAN_REACH_POINTS`
- Modify: `config/settings.py:99` (`tick_strikecount` default)
- Test: `tests/test_config_walls.py` (create)

**Interfaces:**
- Produces: `config.thresholds.WALL_STICKY_MARGIN = 0.05`, `config.thresholds.WALL_SCAN_REACH_POINTS = 400`, `Settings.tick_strikecount` default `15`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_walls.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_config_walls.py -q`
Expected: FAIL (`WALL_SCAN_REACH_POINTS` ImportError; margin 0.15; strikecount 10).

- [ ] **Step 3: Implement**

In `config/thresholds.py`, replace the `WALL_STICKY_MARGIN = 0.15` line (and its comment block at lines 34–40) with:

```python
# --- v3 item 3 / v4: dynamic wall detection (wide-scan + hysteresis) --------
# Detection scans the FULL chain, not just the 8 displayed rungs: CAP = max CE OI
# in [spot, spot+reach], FLOOR = max PE OI in [spot-reach, spot]. `reach` is in the
# index's own points, scaled by the live Sensex/Nifty ratio so both indices reach
# the same relative level (NIFTY 400 ≈ SENSEX 1280). Tune on logged data.
WALL_SCAN_REACH_POINTS = 400

# Hysteresis (hold-unless-beaten): keep the incumbent wall; switch only if a
# challenger's OI beats it by this margin, OR spot crosses the wall (→ re-pick).
# 5% kills flicker without lagging a real migration.
WALL_STICKY_MARGIN = 0.05
```

In `config/settings.py:99`, change:

```python
    tick_strikecount: int = Field(default=15, ge=1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_config_walls.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add config/thresholds.py config/settings.py tests/test_config_walls.py
git commit -m "feat(walls): 5% hysteresis, 400pt scan reach, strikecount 15"
```

---

### Task 2: Wide-scan wall detection (`select_wall` + `select_index_walls`)

**Files:**
- Modify: `compute/strikes.py` (`select_wall`, `select_index_walls`; module docstring)
- Test: `tests/test_strikes.py` (rewrite wall-selection tests), `tests/test_selection.py` (rewrite `select_index_walls` tests)

**Interfaces:**
- Consumes: `WALL_SCAN_REACH_POINTS` (Task 1), `schemas.strikes.WallSelection`, `schemas.market.StrikeOI/ChainSnapshot`.
- Produces:
  - `select_wall(strikes, side, index_name, expiry, interval, spot, scan_reach, incumbent=None, sticky_margin=0.0) -> Optional[WallSelection]` — candidates are full-chain strikes of the side's type within the spot window; tie-break toward spot.
  - `select_index_walls(chain, scan_reach, incumbents=None, sticky_margin=0.0) -> Dict[Side, WallSelection]` (drops the `ladder` param).

- [ ] **Step 1: Write the failing tests**

Replace the wall-selection tests in `tests/test_strikes.py` (everything from `test_select_wall_cap_picks_max_ce_oi_on_ladder` through `test_fixture_smoke_nifty_cap`, keeping the imports, `_mk`, `EXP`, `FIX`, and `test_check_migration`). New body:

```python
REACH = 400  # NIFTY scan reach in points


def test_select_wall_cap_picks_max_ce_oi_in_window():
    strikes = [
        _mk(24350, "CE", 900), _mk(24400, "CE", 300), _mk(24700, "CE", 5000),  # 24700 > spot+400 → excluded
        _mk(24350, "PE", 99999),                                               # PE ignored for CAP
    ]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24350 and sel.wall_oi == 900
    assert sel.monitored == [24300, 24350, 24400]
    assert sel.option_type == "CE" and sel.side == "CAP"


def test_select_wall_floor_picks_max_pe_oi_in_window():
    strikes = [_mk(24150, "PE", 700), _mk(24200, "PE", 1200), _mk(23800, "PE", 9000),  # 23800 < spot-400 → excluded
               _mk(24200, "CE", 99999)]
    sel = select_wall(strikes, "FLOOR", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24200 and sel.option_type == "PE" and sel.side == "FLOOR"


def test_select_wall_finds_off_ladder_wall_inside_window():
    # 24550 is beyond the 8-rung display (build_ladder(24300) tops at 24450) but
    # inside [spot, spot+400] and dominant → detection must still pick it.
    strikes = [_mk(24350, "CE", 500), _mk(24550, "CE", 4000)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24550


def test_select_wall_none_when_window_empty():
    strikes = [_mk(26000, "CE", 500)]                       # far outside spot+400
    assert select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH) is None


def test_select_wall_cap_excludes_below_spot():
    strikes = [_mk(24250, "CE", 9999), _mk(24350, "CE", 900)]   # 24250 < spot → out
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24350


def test_select_wall_tiebreak_prefers_nearest_spot():
    # 24350 and 24450 tie on OI; nearest spot (24300) wins → 24350.
    strikes = [_mk(24350, "CE", 900), _mk(24450, "CE", 900)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH)
    assert sel.wall_strike == 24350


def test_select_wall_sticky_holds_within_5pct():
    # incumbent 24350 (1000) vs challenger 24400 (1040, +4% < 5%) → hold.
    strikes = [_mk(24350, "CE", 1000), _mk(24400, "CE", 1040)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH,
                      incumbent=24350, sticky_margin=0.05)
    assert sel.wall_strike == 24350


def test_select_wall_switches_when_challenger_beats_5pct():
    # challenger 24400 (1060, +6% > 5%) is decisive → switch.
    strikes = [_mk(24350, "CE", 1000), _mk(24400, "CE", 1060)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24300, REACH,
                      incumbent=24350, sticky_margin=0.05)
    assert sel.wall_strike == 24400


def test_select_wall_forced_repick_when_spot_crossed_incumbent():
    # spot rose to 24380; incumbent 24350 is now below spot → out of window → re-pick.
    strikes = [_mk(24400, "CE", 500), _mk(24450, "CE", 900)]
    sel = select_wall(strikes, "CAP", "NIFTY", EXP, 50, 24380, REACH,
                      incumbent=24350, sticky_margin=0.05)
    assert sel.wall_strike == 24450


def test_fixture_smoke_nifty_cap():
    raw = json.load(open(os.path.join(FIX, "nifty_optionchain.json")))
    chain = parse_chain(raw, "NIFTY", datetime(2026, 6, 18, tzinfo=timezone.utc))
    assert chain.spot is not None
    sel = select_wall(chain.strikes, "CAP", "NIFTY", chain.expiry, 50, chain.spot, REACH)
    assert sel is not None
    # independently: max-OI CE strike in [spot, spot+400]
    ce = {s.strike: s.oi for s in chain.strikes
          if s.option_type == "CE" and chain.spot <= s.strike <= chain.spot + REACH}
    assert sel.wall_strike == max(ce, key=ce.get)
    assert sel.expiry == date(2026, 6, 23)
```

Also remove the now-unused `LADDER = build_ladder(...)` module line and the `build_ladder` import if nothing else uses it (the fixture test no longer needs it). Keep `select_index_walls` in the imports.

In `tests/test_selection.py`, replace `test_select_index_walls_picks_spot_side_walls` and `test_select_index_walls_holds_incumbent_within_margin` with:

```python
REACH_N = 400
REACH_S = 1280   # 400 × ~3.20


def test_select_index_walls_picks_spot_side_walls():
    n = select_index_walls(_nifty(), REACH_N)
    s = select_index_walls(_sensex(), REACH_S)
    assert n["CAP"].wall_strike == 24350 and n["FLOOR"].wall_strike == 24200
    assert s["CAP"].wall_strike == 77600 and s["FLOOR"].wall_strike == 77200


def test_select_index_walls_holds_incumbent_within_margin():
    held = select_index_walls(_nifty(), REACH_N, {"CAP": 24350}, sticky_margin=0.05)
    assert held["CAP"].wall_strike == 24350
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_strikes.py tests/test_selection.py -q`
Expected: FAIL (TypeError — `select_wall` still expects `ladder_strikes` positionally).

- [ ] **Step 3: Implement**

In `compute/strikes.py`, replace `select_wall` and `select_index_walls` with:

```python
def select_wall(
    strikes: Sequence[StrikeOI],
    side: Side,
    index_name: str,
    expiry,
    interval: int,
    spot: float,
    scan_reach: float,
    incumbent: Optional[int] = None,
    sticky_margin: float = 0.0,
) -> Optional[WallSelection]:
    """Highest-OI strike of the side's type within a spot window (v4 wide scan).

    Candidates are FULL-CHAIN strikes (never limited to the displayed ladder):
    CAP scans CE OI in [spot, spot+scan_reach]; FLOOR scans PE OI in
    [spot-scan_reach, spot]. Re-picked each tick but STICKY: the incumbent is held
    unless a challenger's OI beats it by `sticky_margin`. Spot crossing the wall
    drops the incumbent out of the window → forced re-pick. Ties break toward spot
    (nearest actionable wall). None if no strike of that type sits in the window.
    """
    option_type = SIDE_OPTION[side]
    lo, hi = (spot, spot + scan_reach) if side == "CAP" else (spot - scan_reach, spot)

    oi_by_strike: Dict[int, int] = {
        s.strike: s.oi
        for s in strikes
        if s.option_type == option_type and lo <= s.strike <= hi
    }
    if not oi_by_strike:
        return None

    # Challenger = highest OI; ties break toward spot (nearest actionable wall).
    challenger = max(oi_by_strike, key=lambda k: (oi_by_strike[k], -abs(k - spot)))

    wall = challenger
    if (
        incumbent is not None
        and incumbent in oi_by_strike
        and incumbent != challenger
        and oi_by_strike[challenger] < oi_by_strike[incumbent] * (1 + sticky_margin)
    ):
        wall = incumbent

    return WallSelection(
        side=side,
        index_name=index_name,
        option_type=option_type,
        expiry=expiry,
        wall_strike=wall,
        wall_oi=oi_by_strike[wall],
        monitored=[wall - interval, wall, wall + interval],
        interval=interval,
    )


def select_index_walls(
    chain: ChainSnapshot,
    interval: int,
    scan_reach: float,
    incumbents: Optional[Dict[Side, int]] = None,
    sticky_margin: float = 0.0,
) -> Dict[Side, WallSelection]:
    """Both walls (CAP via CE above spot, FLOOR via PE below spot) over the scan
    window, sticky. `interval` is the index's strike step (for `monitored`);
    `scan_reach` is this index's own-points reach; `incumbents` maps side → last
    tick's wall strike for the hysteresis. `ChainSnapshot` carries no interval, so
    `lock_walls` passes the instrument's `strike_interval`."""
    out: Dict[Side, WallSelection] = {}
    if chain.spot is None:
        return out
    incumbents = incumbents or {}
    for side in ("CAP", "FLOOR"):
        sel = select_wall(
            chain.strikes, side, chain.index_name, chain.expiry, interval,
            spot=chain.spot, scan_reach=scan_reach,
            incumbent=incumbents.get(side), sticky_margin=sticky_margin,
        )
        if sel is not None:
            out[side] = sel
    return out
```

Update the two `test_selection.py` calls to pass interval: `select_index_walls(_nifty(), 50, REACH_N)` and `select_index_walls(_sensex(), 100, REACH_S)` and `select_index_walls(_nifty(), 50, REACH_N, {"CAP": 24350}, sticky_margin=0.05)`. Update the `test_strikes.py` `select_index_walls` usage likewise (if any remains — the rewritten set above drops it; if you keep a `select_index_walls` test in test_strikes, pass `50`).

Update the `compute/strikes.py` module docstring: detection scans the full chain in a spot window (not the ladder); the ladder is display-only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_strikes.py tests/test_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add compute/strikes.py tests/test_strikes.py tests/test_selection.py
git commit -m "feat(walls): wide-scan detection over full chain, tie toward spot"
```

---

### Task 3: `compute_broken_level` pure helper

**Files:**
- Modify: `compute/strikes.py` (add `compute_broken_level`)
- Test: `tests/test_strikes.py` (add a `# --- v4: broken level ---` section)

**Interfaces:**
- Produces: `compute_broken_level(side: Side, prev_wall: Optional[int], prev_broken: Optional[int], spot: float) -> Optional[int]` — the former wall level spot has cleared and not yet pulled back from; keeps the FIRST break until spot returns past it.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_strikes.py`:

```python
# --- v4: sticky BROKEN level -----------------------------------------------
from compute.strikes import compute_broken_level


def test_broken_cap_sets_level_when_spot_clears_wall():
    # spot 24260 above the incumbent CAP 24200 → 24200 broke.
    assert compute_broken_level("CAP", 24200, None, 24260) == 24200


def test_broken_cap_none_when_spot_below_wall():
    assert compute_broken_level("CAP", 24200, None, 24180) is None


def test_broken_cap_sticky_keeps_first_level_through_further_run():
    # already broken at 24200; spot keeps running to 24360 → still 24200 (not overwritten).
    assert compute_broken_level("CAP", 24300, 24200, 24360) == 24200


def test_broken_cap_clears_on_pullback():
    # spot pulled back to/below the broken level → badge clears.
    assert compute_broken_level("CAP", 24300, 24200, 24200) is None
    assert compute_broken_level("CAP", 24300, 24200, 24150) is None


def test_broken_floor_mirror():
    assert compute_broken_level("FLOOR", 24000, None, 23950) == 24000   # spot below floor → broke
    assert compute_broken_level("FLOOR", 24000, None, 24050) is None
    assert compute_broken_level("FLOOR", 23900, 24000, 23850) == 24000  # sticky
    assert compute_broken_level("FLOOR", 23900, 24000, 24000) is None   # pull back → clear


def test_broken_no_incumbent_is_none():
    assert compute_broken_level("CAP", None, None, 24300) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_strikes.py -k broken -q`
Expected: FAIL (`compute_broken_level` not defined).

- [ ] **Step 3: Implement**

Add to `compute/strikes.py`:

```python
def compute_broken_level(
    side: Side, prev_wall: Optional[int], prev_broken: Optional[int], spot: float
) -> Optional[int]:
    """The former wall level spot has cleared and not yet pulled back from (v4).

    CAP breaks when spot rises ABOVE the wall; FLOOR when spot falls BELOW it. Keeps
    the FIRST level that broke (no flicker on a multi-strike run) until spot returns
    past it. Returns None while the wall is intact / after a pull-back.
    """
    above = side == "CAP"
    if prev_broken is None:
        if prev_wall is not None and ((spot > prev_wall) if above else (spot < prev_wall)):
            return prev_wall
        return None
    # active break — clear only once spot pulls back to/through the broken level.
    cleared = spot <= prev_broken if above else spot >= prev_broken
    return None if cleared else prev_broken
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_strikes.py -k broken -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add compute/strikes.py tests/test_strikes.py
git commit -m "feat(walls): compute_broken_level sticky-first helper"
```

---

### Task 4: Persist-layer reads + migration for `broken_level`

**Files:**
- Modify: `schemas/market.py:151-166` (`MonitoredStrike`)
- Modify: `compute/series.py:20-34` (`read_monitored_strikes`)
- Modify: `market/selection.py` (`read_incumbent_walls` → tuples + degrade)
- Create: `db/migrations/v4_wall_dynamics.sql`
- Modify: `db/schema.sql:116-128` (add column to `monitored_strikes`)
- Test: `tests/test_dbwrite.py` (update `read_incumbent_walls` test)

**Interfaces:**
- Produces:
  - `MonitoredStrike.broken_level: Optional[int]`.
  - `read_monitored_strikes(td)` selects `broken_level`.
  - `read_incumbent_walls(td) -> Dict[Tuple[str,str,date], Tuple[Optional[int], Optional[int]]]` — value is `(wall_strike, broken_level)`; degrades to `(wall, None)` if the column is absent.

- [ ] **Step 1: Write the failing test**

Replace `test_read_incumbent_walls_keys_by_side_index_expiry` in `tests/test_dbwrite.py` with:

```python
def test_read_incumbent_walls_keys_and_broken(monkeypatch):
    cur = FakeCursor(fetch=[
        {"side": "CAP", "index_name": "NIFTY", "expiry": EXP, "wall_strike": 24400, "broken_level": None},
        {"side": "FLOOR", "index_name": "SENSEX", "expiry": EXP, "wall_strike": 77200, "broken_level": 77300},
    ])
    _patch(monkeypatch, sel, cur)
    got = sel.read_incumbent_walls(TD)
    assert got == {("CAP", "NIFTY", EXP): (24400, None),
                   ("FLOOR", "SENSEX", EXP): (77200, 77300)}


def test_read_incumbent_walls_degrades_without_broken_column(monkeypatch):
    class DegradingCursor(FakeCursor):
        def execute(self, sql, params=None):
            if "broken_level" in sql:
                raise psycopg.errors.UndefinedColumn("no column")
            super().execute(sql, params)
    cur = DegradingCursor(fetch=[
        {"side": "CAP", "index_name": "NIFTY", "expiry": EXP, "wall_strike": 24400},
    ])
    _patch(monkeypatch, sel, cur)
    got = sel.read_incumbent_walls(TD)
    assert got == {("CAP", "NIFTY", EXP): (24400, None)}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dbwrite.py -k incumbent -q`
Expected: FAIL (values are ints, not tuples; no degrade path).

- [ ] **Step 3: Implement**

`schemas/market.py` — add to `MonitoredStrike` after `wall_oi_at_lock`:

```python
    broken_level: Optional[int] = None   # v4: former wall spot cleared (sticky badge)
```

`compute/series.py` `read_monitored_strikes` — add `broken_level` to the SELECT column list:

```python
            SELECT trading_date, side, index_name, option_type, expiry,
                   wall_strike, monitored, wall_oi_at_lock, broken_level
            FROM monitored_strikes
```

`market/selection.py` `read_incumbent_walls` — replace with the degrading, tuple-returning version:

```python
def read_incumbent_walls(
    trading_date: date,
) -> Dict[Tuple[str, str, date], Tuple[Optional[int], Optional[int]]]:
    """{(side, index_name, expiry): (wall_strike, broken_level)} — last tick's walls
    (for stickiness + the sticky BROKEN badge). Degrades to (wall, None) if the
    broken_level column isn't migrated yet."""
    with get_conn() as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "SELECT side, index_name, expiry, wall_strike, broken_level "
                "FROM monitored_strikes WHERE trading_date = %s",
                (trading_date,),
            )
            return {
                (r["side"], r["index_name"], r["expiry"]):
                    (r["wall_strike"], r["broken_level"])
                for r in cur.fetchall()
            }
        except psycopg.errors.UndefinedColumn:
            logger.warning("monitored_strikes.broken_level missing — apply "
                           "db/migrations/v4_wall_dynamics.sql; BROKEN badge dormant")
            cur.execute(
                "SELECT side, index_name, expiry, wall_strike "
                "FROM monitored_strikes WHERE trading_date = %s",
                (trading_date,),
            )
            return {
                (r["side"], r["index_name"], r["expiry"]): (r["wall_strike"], None)
                for r in cur.fetchall()
            }
```

Add `import psycopg` to `market/selection.py` if not present.

Create `db/migrations/v4_wall_dynamics.sql`:

```sql
-- v4 — sticky BROKEN badge: remember the former wall spot cleared, per side/index.
-- Idempotent. Apply BEFORE deploying the v4 code (lock_walls degrades if absent,
-- but the BROKEN badge stays dormant until this runs).
begin;
alter table monitored_strikes add column if not exists broken_level integer;
commit;
```

`db/schema.sql` — in `monitored_strikes`, add after `wall_oi_at_lock bigint,`:

```sql
    broken_level    integer,                     -- v4: former wall spot cleared (sticky badge)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dbwrite.py -k incumbent -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add schemas/market.py compute/series.py market/selection.py db/migrations/v4_wall_dynamics.sql db/schema.sql tests/test_dbwrite.py
git commit -m "feat(walls): persist broken_level column + degrading incumbent read"
```

---

### Task 5: `lock_walls` rework — per-index reach + broken wiring + graceful-degrade persist

**Files:**
- Modify: `market/selection.py` (`_UPSERT_WALL`, `_upsert_wall`, `lock_walls`; add base UPSERT + `_persist_picks`)
- Test: `tests/test_dbwrite.py` (update `_upsert_wall` tests), `tests/test_selection.py` (add a `lock_walls` reach test)

**Interfaces:**
- Consumes: `select_index_walls(chain, interval, scan_reach, incumbents, sticky_margin)` (Task 2), `compute_broken_level` (Task 3), `read_incumbent_walls` tuples (Task 4), `WALL_SCAN_REACH_POINTS`/`WALL_STICKY_MARGIN` (Task 1).
- Produces: `_upsert_wall(cur, td, side, sel, broken_level, with_broken=True) -> int`; `lock_walls(td, chains) -> List[dict]` (unchanged return shape).

- [ ] **Step 1: Write the failing tests**

In `tests/test_dbwrite.py`, replace `test_upsert_wall_passes_columns_in_order_and_returns_rowcount` and add a degrade test:

```python
def test_upsert_wall_passes_columns_in_order_with_broken(monkeypatch):
    cur = FakeCursor(rowcounts=[1])
    wsel = WallSelection(side="CAP", index_name="NIFTY", option_type="CE", expiry=EXP,
                         wall_strike=24400, wall_oi=900,
                         monitored=[24350, 24400, 24450], interval=50)
    changed = sel._upsert_wall(cur, TD, "CAP", wsel, 24350)
    assert changed == 1
    _sql, params = cur.calls[0]
    assert params == (TD, "CAP", "NIFTY", "CE", EXP, 24400,
                      [24350, 24400, 24450], 900, 24350)   # broken_level last


def test_upsert_wall_base_sql_when_column_missing(monkeypatch):
    cur = FakeCursor(rowcounts=[1])
    wsel = WallSelection(side="CAP", index_name="NIFTY", option_type="CE", expiry=EXP,
                         wall_strike=24400, wall_oi=900,
                         monitored=[24350, 24400, 24450], interval=50)
    sel._upsert_wall(cur, TD, "CAP", wsel, 24350, with_broken=False)
    _sql, params = cur.calls[0]
    assert "broken_level" not in _sql
    assert params == (TD, "CAP", "NIFTY", "CE", EXP, 24400, [24350, 24400, 24450], 900)
```

Add to `tests/test_selection.py` a reach test (uses the existing `_nifty`/`_sensex`/`INSTR` and monkeypatch of the DB out; keep it pure by asserting the reach passed into `select_index_walls`):

```python
def test_lock_walls_scales_sensex_reach_by_live_ratio(monkeypatch):
    import market.selection as selmod
    captured = []

    def fake_select(chain, interval, scan_reach, incumbents=None, sticky_margin=0.0):
        captured.append((chain.index_name, round(scan_reach)))
        return {}

    monkeypatch.setattr(selmod, "select_index_walls", fake_select)
    monkeypatch.setattr(selmod, "upsert_ladders", lambda td, lads: 0)
    monkeypatch.setattr(selmod, "read_incumbent_walls", lambda td: {})
    monkeypatch.setattr(selmod, "all_instruments", lambda: INSTR)
    # no DB writes happen (select returns {}), so get_conn is only opened for the loop.
    monkeypatch.setattr(selmod, "get_conn", lambda: _NullConn())
    selmod.lock_walls(NIFTY_EXP, {"NIFTY": _nifty(), "SENSEX": _sensex()})
    reach = dict(captured)
    assert reach["NIFTY"] == 400
    # 400 × (77400 / 24300) ≈ 1274
    assert 1260 <= reach["SENSEX"] <= 1290
```

Add a tiny null-conn helper near the top of `tests/test_selection.py`:

```python
class _NullCur:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    rowcount = 0
class _NullConn:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return _NullCur()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dbwrite.py tests/test_selection.py -k "upsert_wall or reach" -q`
Expected: FAIL (`_upsert_wall` takes no `broken_level`; `lock_walls` still passes a ladder to `select_index_walls`).

- [ ] **Step 3: Implement**

In `market/selection.py`, replace `_UPSERT_WALL`, `_upsert_wall`, and `lock_walls`. Add a base UPSERT and split persistence:

```python
_UPSERT_WALL = """
INSERT INTO monitored_strikes
    (trading_date, side, index_name, option_type, expiry,
     wall_strike, monitored, wall_oi_at_lock, broken_level)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trading_date, side, index_name, expiry) DO UPDATE
    SET option_type     = EXCLUDED.option_type,
        wall_strike     = EXCLUDED.wall_strike,
        monitored       = EXCLUDED.monitored,
        wall_oi_at_lock = EXCLUDED.wall_oi_at_lock,
        broken_level    = EXCLUDED.broken_level,
        locked_at       = now()
    WHERE monitored_strikes.wall_strike IS DISTINCT FROM EXCLUDED.wall_strike
       OR monitored_strikes.broken_level IS DISTINCT FROM EXCLUDED.broken_level
"""

_UPSERT_WALL_BASE = """
INSERT INTO monitored_strikes
    (trading_date, side, index_name, option_type, expiry,
     wall_strike, monitored, wall_oi_at_lock)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (trading_date, side, index_name, expiry) DO UPDATE
    SET option_type     = EXCLUDED.option_type,
        wall_strike     = EXCLUDED.wall_strike,
        monitored       = EXCLUDED.monitored,
        wall_oi_at_lock = EXCLUDED.wall_oi_at_lock,
        locked_at       = now()
    WHERE monitored_strikes.wall_strike IS DISTINCT FROM EXCLUDED.wall_strike
"""


def _upsert_wall(cur, trading_date, side, sel, broken_level, with_broken=True):
    if with_broken:
        cur.execute(_UPSERT_WALL, (
            trading_date, side, sel.index_name, sel.option_type, sel.expiry,
            sel.wall_strike, sel.monitored, sel.wall_oi, broken_level))
    else:
        cur.execute(_UPSERT_WALL_BASE, (
            trading_date, side, sel.index_name, sel.option_type, sel.expiry,
            sel.wall_strike, sel.monitored, sel.wall_oi))
    return cur.rowcount
```

Replace `lock_walls`:

```python
def lock_walls(trading_date: date, chains: Dict[str, ChainSnapshot]) -> List[dict]:
    """Re-center display ladders + wide-scan re-pick CAP/FLOOR walls (sticky), this
    tick. Detection scans the full chain in a per-index spot window; the ladder is
    display-only. Returns the walls that CHANGED (new pick, move, or badge change)."""
    instruments = all_instruments()
    ladders = plan_ladders(chains, instruments)   # display only
    if not ladders:
        logger.warning("no ladders to refresh for %s (no live spot?)", trading_date)
        return []
    upsert_ladders(trading_date, list(ladders.values()))

    incumbents = read_incumbent_walls(trading_date)   # {key: (wall, broken)}
    nspot = chains["NIFTY"].spot if "NIFTY" in chains and chains["NIFTY"].spot else None

    # Build all picks first (pure), then persist (so a missing column degrades cleanly).
    picks: List[Tuple[str, object, Optional[int]]] = []
    for name, chain in chains.items():
        inst = instruments.get(name)
        if inst is None or chain.spot is None:
            logger.warning("no instrument/spot for %s — cannot pick walls", name)
            continue
        reach = (WALL_SCAN_REACH_POINTS * (chain.spot / nspot) if nspot
                 else WALL_SCAN_REACH_POINTS * float(inst.price_mult))
        inc = {s: incumbents.get((s, name, chain.expiry), (None, None)) for s in SIDES}
        inc_wall = {s: inc[s][0] for s in SIDES}
        sels = select_index_walls(chain, inst.strike_interval, reach, inc_wall,
                                  WALL_STICKY_MARGIN)
        for side in SIDES:
            selc = sels.get(side)
            if selc is None:
                logger.warning(
                    "could not pick %s %s on expiry %s — no %s OI in the %s-pt window",
                    name, side, chain.expiry, "CE" if side == "CAP" else "PE", round(reach))
                continue
            broken = compute_broken_level(side, inc_wall[side], inc[side][1], chain.spot)
            picks.append((side, selc, broken))

    return _persist_picks(trading_date, picks)


def _persist_picks(trading_date: date, picks) -> List[dict]:
    """UPSERT all picks; retry the whole batch without broken_level if the column
    isn't migrated (never refreeze walls on a missed migration)."""
    for with_broken in (True, False):
        changed: List[dict] = []
        try:
            with get_conn() as conn, conn.cursor() as cur:
                for side, selc, broken in picks:
                    if _upsert_wall(cur, trading_date, side, selc, broken, with_broken):
                        changed.append({
                            "side": side, "index": selc.index_name,
                            "option_type": selc.option_type, "wall": selc.wall_strike,
                            "monitored": selc.monitored, "expiry": str(selc.expiry),
                            "broken_level": broken if with_broken else None,
                        })
                        logger.info("refreshed %s %s wall=%s broken=%s (expiry %s)",
                                    selc.index_name, side, selc.wall_strike,
                                    broken if with_broken else "n/a", selc.expiry)
            return changed
        except psycopg.errors.UndefinedColumn:
            logger.warning("monitored_strikes.broken_level missing — persisting walls "
                           "without the BROKEN badge; apply db/migrations/v4_wall_dynamics.sql")
            continue
    return []
```

Add imports to `market/selection.py`: `from compute.strikes import compute_broken_level, plan_ladders, select_index_walls` and `from config.thresholds import WALL_SCAN_REACH_POINTS, WALL_STICKY_MARGIN`. Ensure `Tuple` is imported from `typing`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_dbwrite.py tests/test_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add market/selection.py tests/test_dbwrite.py tests/test_selection.py
git commit -m "feat(walls): per-index scan reach + sticky broken persist w/ degrade"
```

---

### Task 6: Engine + schema wiring — off-ladder flag, dominance fix, callout row

**Files:**
- Modify: `schemas/verdict.py` (`WallSignal`: `wall_off_ladder`, `broken_level`; `SideVerdict`: `wall_callout`)
- Modify: `compute/pairing.py` (add `build_wall_callout`)
- Modify: `compute/engine.py` (`_index_wall`, `build_state`)
- Test: `tests/test_pairing.py` (callout), `tests/test_verdict.py` (engine off-ladder/broken)

**Interfaces:**
- Consumes: `MonitoredStrike.broken_level` (Task 4), `pairing._agree`.
- Produces:
  - `WallSignal.wall_off_ladder: bool`, `WallSignal.broken_level: Optional[int]`.
  - `SideVerdict.wall_callout: Optional[PairedRung]`.
  - `build_wall_callout(nifty_ws, sensex_ws, ratio) -> Optional[PairedRung]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_pairing.py`:

```python
from compute.pairing import build_wall_callout
from schemas.verdict import StrikeSignal, WallSignal


def _ws(index, strike, direction, off_ladder):
    sig = StrikeSignal(index_name=index, option_type="CE", strike=strike,
                       expiry=EXP, is_wall=True, direction=direction, oi_latest=1000)
    return WallSignal(index_name=index, state="building", wall=sig,
                      wall_off_ladder=off_ladder)


def test_callout_none_when_both_on_ladder():
    n = _ws("NIFTY", 24300, "up", False)
    s = _ws("SENSEX", 77800, "up", False)
    assert build_wall_callout(n, s, 3.2) is None


def test_callout_pairs_off_ladder_walls_with_agree():
    n = _ws("NIFTY", 24550, "up", True)
    s = _ws("SENSEX", 78560, "up", True)      # 24550×3.2 = 78560
    row = build_wall_callout(n, s, 3.2)
    assert row.nifty.strike == 24550 and row.sensex.strike == 78560
    assert row.agree == "ALIGNED" and row.is_wall and row.level_gap == 0


def test_callout_one_leg_when_only_nifty_off_ladder():
    n = _ws("NIFTY", 24550, "up", True)
    s = _ws("SENSEX", 77800, "down", False)
    row = build_wall_callout(n, s, 3.2)
    assert row.nifty.strike == 24550 and row.sensex is None and row.agree is None
```

(`EXP` already exists in `tests/test_pairing.py`; if not, add `from datetime import date; EXP = date(2026, 6, 24)`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pairing.py -k callout -q`
Expected: FAIL (`build_wall_callout` not defined; `WallSignal` has no `wall_off_ladder`).

- [ ] **Step 3: Implement**

`schemas/verdict.py` — in `WallSignal`, after `ladder: List[StrikeSignal] = []`:

```python
    # v4: the detected wall sits outside the visible 8-rung ladder → render it as a
    # highlighted callout row (verdict still computes on it).
    wall_off_ladder: bool = False
    # v4: former wall level spot cleared, held until spot pulls back (sticky BROKEN).
    broken_level: Optional[int] = None
```

`schemas/verdict.py` — in `SideVerdict`, after `paired: List[PairedRung] = []`:

```python
    # v4: single paired callout row for an off-ladder wall (Aligned/Divergent). None
    # when both indices' walls are on their visible ladders.
    wall_callout: Optional[PairedRung] = None
```

`compute/pairing.py` — add:

```python
def build_wall_callout(nifty_ws, sensex_ws, ratio):
    """A single paired callout row for a wall that sits off its 8-rung ladder (v4).

    A leg is filled only when THAT index's wall is off-ladder (else it's already in
    the visible table). None when both walls are on-ladder."""
    n_off = bool(nifty_ws and nifty_ws.wall_off_ladder)
    s_off = bool(sensex_ws and sensex_ws.wall_off_ladder)
    if not n_off and not s_off:
        return None
    n_leg = nifty_ws.wall if n_off else None
    s_leg = sensex_ws.wall if s_off else None
    gap = None
    if ratio and n_leg is not None and s_leg is not None:
        gap = round(abs(s_leg.strike - n_leg.strike * ratio))
    return PairedRung(nifty=n_leg, sensex=s_leg, agree=_agree(n_leg, s_leg),
                      is_wall=True, level_gap=gap)
```

`compute/engine.py` `_index_wall` — after `ws = build_wall_signal(ms, signals)` add the off-ladder flag and broken passthrough, and fix dominance:

```python
    ws.broken_level = ms.broken_level
    if ladder is not None and ladder.strikes:
        ladder_set = set(ladder.strikes)
        ws.wall_off_ladder = ms.wall_strike not in ladder_set
        ws.ladder = [signals[s] for s in sorted(ladder.strikes, reverse=True)]
        # Strength: size the wall against the 8 VISIBLE rungs. Take wall OI from the
        # scored wall signal so an OFF-LADDER wall still gets a strength.
        wall_oi = ws.wall.oi_latest
        others = [signals[s].oi_latest for s in ladder.strikes
                  if s != ms.wall_strike and signals[s].oi_latest is not None]
        ws.dominance, ws.strength = dominance_strength(wall_oi, others)
    else:
        ws.wall_off_ladder = False
    return ws
```

(Remove the old `oi_by_strike`/`wall_oi = oi_by_strike.get(...)` block this replaces.)

`compute/engine.py` — import the callout builder: change the pairing import to `from compute.pairing import build_wall_callout, pair_ladders_by_level`.

`compute/engine.py` `build_state` — after `sv.paired = pair_ladders_by_level(...)`:

```python
        sv.wall_callout = build_wall_callout(nifty_wall, sensex_wall, live_ratio)
```

Add to `tests/test_verdict.py` (near the existing engine test) a check that an off-ladder wall sets the flag and produces a callout. Use the existing engine-test scaffolding/fixtures in that file; assert `state.sides[0].nifty.wall_off_ladder is True` and `state.sides[0].wall_callout is not None` for a monitored wall placed off the ladder. (Follow the existing engine test's monkeypatch of `read_monitored_strikes`/`read_oi_series`/`get_ladders`; set the NIFTY `wall_strike` to a strike not in the ladder.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_pairing.py tests/test_verdict.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add schemas/verdict.py compute/pairing.py compute/engine.py tests/test_pairing.py tests/test_verdict.py
git commit -m "feat(walls): off-ladder flag, strength fix, paired callout row"
```

---

### Task 7: Frontend — BROKEN badge + callout row

**Files:**
- Modify: `frontend-next/app/components/SideCard.jsx` (BROKEN badge; pass callout)
- Modify: `frontend-next/app/components/PairedWalls.jsx` (callout row)

**Interfaces:**
- Consumes (from `/state`): `side.nifty.broken_level`, `side.sensex.broken_level`, `side.wall_callout` (a `PairedRung`), `side.nifty.wall_off_ladder`.

- [ ] **Step 1: Implement the BROKEN badge (SideCard.jsx)**

In `SideCard.jsx`, destructure `wall_callout` from `side` (add to the existing destructure). After the `{dist && ...}` span in the header `<h2>`, add:

```jsx
          {nifty?.broken_level != null && (
            <span className="broken-badge" title="spot cleared this former wall">
              ⚠ BROKEN — cleared {nifty.broken_level}
            </span>
          )}
```

Change the `PairedWalls` usage at the bottom to pass the callout:

```jsx
      <PairedWalls paired={paired} callout={wall_callout} ratio={ratio} window={win} optionType={option_type} />
```

- [ ] **Step 2: Implement the callout row (PairedWalls.jsx)**

In `PairedWalls.jsx`, accept `callout` in the props and render it as a highlighted row after the mapped rows (before the empty-state row). Update the signature to `export default function PairedWalls({ paired, callout, ratio, window: win, optionType })` and add inside `<tbody>` after the `{rows.map(...)}`:

```jsx
        {callout && (
          <tr className="row-callout" title="wall detected outside the visible 8 rungs">
            <td className="callout-label" colSpan={0} />
            <Leg sig={callout.nifty} />
            <Leg sig={callout.sensex} />
            <td className={`col-agree agree-${(callout.agree || 'none').toLowerCase()}`}>
              {callout.agree || '—'}
            </td>
          </tr>
        )}
```

Remove the stray `<td className="callout-label" colSpan={0} />` (it was illustrative) — the row must have exactly the 7 columns the table uses (3 NIFTY + 3 SENSEX + 1 agree), so the final callout row is:

```jsx
        {callout && (
          <tr className="row-callout" title="wall detected outside the visible 8 rungs">
            <Leg sig={callout.nifty} />
            <Leg sig={callout.sensex} />
            <td className={`col-agree agree-${(callout.agree || 'none').toLowerCase()}`}>
              {callout.agree || '—'}
            </td>
          </tr>
        )}
```

Add minimal styles (find the stylesheet that defines `.row-wall` — likely `frontend-next/app/globals.css` or a co-located CSS; grep for `row-wall`). Append:

```css
.row-callout { background: #fff7ed; outline: 1px solid #fb923c; }
.broken-badge { margin-left: .5rem; color: #b91c1c; font-weight: 600; font-size: .8rem; }
```

- [ ] **Step 3: Lint (frontend has no unit-test harness — validate via ESLint)**

Run (from `frontend-next/`): `node node_modules/eslint/bin/eslint.js app/components/SideCard.jsx app/components/PairedWalls.jsx`
Expected: no errors (warnings about existing rules are acceptable if pre-existing).

- [ ] **Step 4: Commit**

```bash
git add frontend-next/app/components/SideCard.jsx frontend-next/app/components/PairedWalls.jsx
git add frontend-next/app/globals.css   # or whichever stylesheet you edited
git commit -m "feat(walls): BROKEN badge + off-ladder callout row"
```

---

### Task 8: Full suite green + integration verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole backend suite**

Run: `./.venv/Scripts/python.exe -m pytest tests/ -q`
Expected: all pass (the prior 139 + the new tests). Fix any breakage from signature changes (search for other `select_wall(`/`select_index_walls(`/`_upsert_wall(`/`read_incumbent_walls(` callers).

- [ ] **Step 2: Grep for stragglers**

Run: `git grep -n "ladder_strikes\|select_index_walls(\|read_incumbent_walls("` — confirm no caller still passes the old ladder arg or expects an int value from incumbents.

- [ ] **Step 3: Reproduce the screenshot scenario (sanity)**

Reuse the `scratchpad/repro_wall.py` idea but for the new signature: assert that with spot 24060, reach 400, a CE cluster at 24100 (2.19 Cr) beats an incumbent 24200 and is chosen; and that an off-ladder wall at 24400 is returned + flagged. Run it with `PYTHONPATH=e:/Kaushal/COBRA`.

- [ ] **Step 4: Final commit / push (only when the user asks)**

Do NOT push automatically. When approved:

```bash
git push -u origin feat/dynamic-wall-detection
```

Then remind: apply `db/migrations/v4_wall_dynamics.sql` on Supabase BEFORE deploying, redeploy the backend, confirm the tick cron runs, then deploy `frontend-next`.

---

## Self-Review

- **Spec coverage:** A=Task 2; per-index reach (B)=Task 5; fetch width (C)=Task 1; hysteresis (D)=Task 1; broken helper (E)=Task 3, persist=Tasks 4–5, surface=Task 6, UI=Task 7; off-ladder callout (F)=Tasks 6–7; migration/safety (H)=Tasks 4–5; tests (all tasks). All spec sections mapped.
- **Placeholder scan:** none — Task 6's `test_verdict.py` step references the file's existing engine-test scaffolding rather than pasting it; the assertions are explicit.
- **Type consistency:** `select_wall`/`select_index_walls` signatures are consistent between Tasks 2 and 5; `read_incumbent_walls` returns `(wall, broken)` tuples in Task 4 and is consumed as tuples in Task 5; `_upsert_wall(cur, td, side, sel, broken_level, with_broken=True)` consistent between Tasks 4-guarded and 5; `build_wall_callout(nifty_ws, sensex_ws, ratio)` consistent between Tasks 6 (def) and its call in `build_state`.
