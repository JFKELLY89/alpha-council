import sqlite3
from alpha_council.settings import get_settings
from alpha_council.utils.ids import candidate_id
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
row = c.execute(
    "SELECT scan_id FROM scan_runs ORDER BY started_at DESC LIMIT 1").fetchone()
scan = row["scan_id"]
print("latest scan:", scan)
rows = list(c.execute(
    "SELECT candidate_id, symbol FROM candidate_scores WHERE scan_id=?",
    (scan,)))
print(f"{len(rows)} candidate_scores rows for that scan")
for r in rows[:6]:
    print("  stored:", r["candidate_id"], r["symbol"])
    print("  wanted:", candidate_id(scan, r["symbol"]))
