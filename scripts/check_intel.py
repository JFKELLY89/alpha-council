import sqlite3
c = sqlite3.connect("data/alpha_council.db")
c.row_factory = sqlite3.Row
n = c.execute("SELECT COUNT(*) FROM intelligence_items").fetchone()[0]
e = c.execute("SELECT COUNT(*) FROM intelligence_events").fetchone()[0]
print(f"intelligence_items : {n}")
print(f"intelligence_events: {e}")
print()
for r in c.execute(
        "SELECT symbol, event_type, ROUND(catalyst_score,1) cat, "
        "direction, created_at FROM intelligence_events "
        "ORDER BY catalyst_score DESC LIMIT 10"):
    print(f"  {r['symbol']:<7}{r['cat']:>6}  {r['direction']:<9}"
          f"{r['event_type']:<20}{r['created_at'][11:19]}")
print()
print("candidates with a catalyst score:")
for r in c.execute(
        "SELECT symbol, candidate_track, ROUND(catalyst_score,1) cat, "
        "scan_id FROM candidate_scores WHERE catalyst_score > 0 "
        "ORDER BY created_at DESC LIMIT 10"):
    print("  ", dict(r))
