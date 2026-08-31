import sqlite3
from alpha_council.settings import get_settings
c = sqlite3.connect(f"file:{get_settings().database_path}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
print("candidate_scores by scan:")
for r in c.execute(
        "SELECT scan_id, COUNT(*) n, MIN(pre_score) lo, MAX(pre_score) hi, "
        "MAX(final_opportunity_score) fmax, candidate_track track "
        "FROM candidate_scores GROUP BY scan_id "
        "ORDER BY scan_id DESC LIMIT 8"):
    print("  ", dict(r))
print()
print("gate_rejections by stage and gate:")
for r in c.execute(
        "SELECT stage, gate_id, COUNT(*) n FROM gate_rejections "
        "GROUP BY stage, gate_id ORDER BY n DESC LIMIT 20"):
    print("  ", dict(r))
