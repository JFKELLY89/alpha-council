import sqlite3, json
from alpha_council.settings import get_settings
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT occurred_at, message, context_json FROM system_events "
                   "WHERE event_type='SCENARIO_SET_REJECTED' "
                   "ORDER BY occurred_at DESC LIMIT 3"):
    print(r["occurred_at"], r["message"])
    print(json.dumps(json.loads(r["context_json"]), indent=2))
