"""Export the chat logs to CSV for analysis.

  python export_logs.py            -> messages.csv (one row per message)
                                      sessions.csv (one row per chat session)
Merge with your Qualtrics export on response_id (= ResponseId column).
"""
import csv
import sqlite3
import sys

db = sys.argv[1] if len(sys.argv) > 1 else "chat_logs.sqlite3"
conn = sqlite3.connect(db)

for table, out in [("messages", "messages.csv"), ("sessions", "sessions.csv")]:
    cur = conn.execute(f"SELECT * FROM {table}")
    cols = [d[0] for d in cur.description]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(cur.fetchall())
    print(f"Wrote {out}")
