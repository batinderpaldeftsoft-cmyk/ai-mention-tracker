import sqlite3
import os

db_path = 'data/tracker.db'
if os.path.exists(db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if cross_platform_mentions column exists
        cursor.execute("PRAGMA table_info(discovery_results)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'cross_platform_mentions' not in columns:
            print("Adding cross_platform_mentions column to discovery_results...")
            cursor.execute("ALTER TABLE discovery_results ADD COLUMN cross_platform_mentions TEXT")
            print("Column added successfully.")
        else:
            print("Column cross_platform_mentions already exists.")
            
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error during migration: {e}")
else:
    print("Database file not found.")
