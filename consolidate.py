import os
import re
from pathlib import Path

def consolidate():
    app_path = Path("app.py")
    api_path = Path("api/dataforseo.py")
    storage_path = Path("db/storage.py")

    app_content = app_path.read_text(encoding='utf-8')
    api_content = api_path.read_text(encoding='utf-8')
    storage_content = storage_path.read_text(encoding='utf-8')

    # Fix imports in app_content
    app_content = app_content.replace('from api.dataforseo import DataForSeoClient', '')
    app_content = app_content.replace('from db.storage import TrackerStorage', '')

    # Helper to clean individual file content of existing imports to avoid duplicates at top
    def clean_imports(content):
        lines = content.split('\n')
        cleaned = [line for line in lines if not line.strip().startswith(('import ', 'from '))]
        return '\n'.join(cleaned)

    api_clean = clean_imports(api_content)
    storage_clean = clean_imports(storage_content)

    unified = f"""import os
import json
import time
import webbrowser
import re
import sqlite3
import requests
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from flask import Flask, render_template, request, session, redirect, url_for, Response, jsonify, send_file
from requests.auth import HTTPBasicAuth

# Optional: psycopg2 for Postgres support on cloud
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

{api_clean}

{storage_clean}

{app_content}
"""
    Path("app_cloud.py").write_text(unified, encoding='utf-8')
    print("Consolidated app_cloud.py created successfully.")

if __name__ == "__main__":
    consolidate()
