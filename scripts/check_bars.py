import sqlite3
from alpha_council.settings import get_settings
from alpha_council.utils.time import to_et, utc_now, clock_window_index

c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
now = utc_now()
print("now ET        :", to_et(now))
print("clock window  :", clock_window_index(now))
print()
for sym in ["SPY", "RIVN"]:
    rows = list(c.execute(
        "SELECT ts FROM market_bars WHERE symbol=? AND timeframe='5Min' "
        "ORDER BY ts DESC LIMIT 5", (sym,)))
    print(f"{sym} latest bars:")
    for r in rows:
        print("   ", r["ts"], "->", to_et(__import__("alpha_council.utils.time",
              fromlist=["parse_alpaca_ts"]).parse_alpaca_ts(r["ts"])))
