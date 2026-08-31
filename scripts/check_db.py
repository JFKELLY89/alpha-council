import sqlite3, os
from pathlib import Path
from alpha_council.settings import get_settings

s = get_settings()
print("settings path :", s.database_path)
print("exists        :", Path(s.database_path).exists())
print("cwd           :", os.getcwd())
print()

print("stray .db files under the repo:")
for p in Path(".").rglob("*.db"):
    print(f"   {p.resolve()}  {p.stat().st_size:,} bytes")
print()

for label, path in [("settings", s.database_path), ("relative", "data/alpha_council.db")]:
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        counts = {}
        for t in ["orders", "fills", "execution_calibrations", "trade_journal",
                  "decisions", "system_events", "agent_runs"]:
            counts[t] = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"{label:<10} {path}")
        print("          ", counts)
    except Exception as e:
        print(f"{label:<10} {path} -> {e}")
