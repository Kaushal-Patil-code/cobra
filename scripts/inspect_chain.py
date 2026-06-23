"""Phase 1 / task 2 — inspect the live Fyers optionchain response shape.

Triggers a real Fyers login (via auth.get_fyers_client) and pulls the option
chain for Nifty + Sensex, prints the structure, and saves the raw JSON under
tests/fixtures/ so the parser + its tests can run offline.

Run:  uv run python -m scripts.inspect_chain
"""
import json
import logging
import os

from auth import get_fyers_client
from db.db import close_pool

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("inspect_chain")

SYMBOLS = {
    "NIFTY": "NSE:NIFTY50-INDEX",
    "SENSEX": "BSE:SENSEX-INDEX",
}
STRIKECOUNT = 10
FIXTURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tests", "fixtures"
)
_SAMPLE_KEYS = (
    "symbol", "strike_price", "option_type", "ltp",
    "oi", "oich", "oichp", "prev_oi", "volume",
)


def summarize(name: str, sym: str, resp: dict) -> None:
    print("=" * 72)
    print(f"{name}  ({sym})")
    print("=" * 72)
    print("top-level keys :", list(resp.keys()))
    print("s/code/message :", resp.get("s"), "/", resp.get("code"), "/", resp.get("message"))
    data = resp.get("data") or {}
    print("data keys      :", list(data.keys()))
    print("callOi / putOi :", data.get("callOi"), "/", data.get("putOi"))
    print("indiavixData   :", data.get("indiavixData"))
    exp = data.get("expiryData")
    print("expiryData     :", json.dumps(exp)[:600] if exp else exp)
    chain = data.get("optionsChain") or []
    print(f"optionsChain   : {len(chain)} rows")
    for i, row in enumerate(chain[:4]):
        print(f"  row[{i}] keys :", list(row.keys()))
        print(f"  row[{i}]      :", {k: row.get(k) for k in _SAMPLE_KEYS})
    for ot in ("CE", "PE"):
        sample = next((r for r in chain if r.get("option_type") == ot), None)
        print(f"  first {ot}     :", {k: (sample or {}).get(k) for k in _SAMPLE_KEYS} if sample else None)


def main() -> None:
    os.makedirs(FIXTURE_DIR, exist_ok=True)
    try:
        client = get_fyers_client()  # triggers DB-cached / refresh / full TOTP login
        for name, sym in SYMBOLS.items():
            log.info("optionchain %s ...", sym)
            resp = client.optionchain(data={"symbol": sym, "strikecount": STRIKECOUNT, "timestamp": ""})
            summarize(name, sym, resp)
            path = os.path.join(FIXTURE_DIR, f"{name.lower()}_optionchain.json")
            with open(path, "w") as f:
                json.dump(resp, f, indent=2, default=str)
            print(f"  saved → {path}\n")
    finally:
        close_pool()  # avoid the pool's noisy __del__ at interpreter shutdown


if __name__ == "__main__":
    main()
