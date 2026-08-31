import sqlite3
from alpha_council.settings import get_settings
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print("scan_runs:")
for r in c.execute("SELECT scan_id, started_at, universe_size, candidate_count,"
                   " status FROM scan_runs ORDER BY started_at DESC LIMIT 5"):
    print("  ", dict(r))
print()
print("discovery_candidates by scan:")
for r in c.execute("SELECT scan_id, COUNT(*) n FROM discovery_candidates "
                   "GROUP BY scan_id ORDER BY scan_id DESC LIMIT 5"):
    print("  ", dict(r))
print()
print("funnel_snapshots:")
for r in c.execute("SELECT * FROM funnel_snapshots ORDER BY as_of DESC LIMIT 3"):
    print("  ", dict(r))
