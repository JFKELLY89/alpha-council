import sqlite3, json
from alpha_council.settings import get_settings
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = list(c.execute(
    "SELECT agent_name, error, output_json FROM agent_runs "
    "WHERE status='INVALID_SCHEMA' ORDER BY started_at DESC LIMIT 3"))
for r in rows:
    print("=" * 70)
    print(r["agent_name"], "|", r["error"])
    print(r["output_json"][:1200])
