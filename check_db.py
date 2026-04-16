import sqlite3
try:
    conn = sqlite3.connect('data/tracker.db')
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(discovery_results)")
    columns = cursor.fetchall()
    for col in columns:
        print(col)
    conn.close()
except Exception as e:
    print(f"Error: {e}")
