import os
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


class DataForSeoClient:
    BASE_URL = "https://api.dataforseo.com/v3"

    def __init__(self, login, password):
        self.login = login
        self.password = password
        self.auth = HTTPBasicAuth(login, password)
        self.session = requests.Session()

    def _post(self, endpoint, data, retries=3):
        url = f"{self.BASE_URL}{endpoint}"
        for i in range(retries):
            try:
                response = self.session.post(url, auth=self.auth, json=data, timeout=120)
                if response.status_code == 429:
                    # Rate limited: wait and retry
                    wait_time = (2 ** i) * 2
                    time.sleep(wait_time)
                    continue
                
                # DataForSEO often returns error details in JSON even for non-200 codes
                if not response.ok:
                    try:
                        err_data = response.json()
                        msg = err_data.get('status_message', response.reason)
                        raise requests.exceptions.RequestException(f"{response.status_code} {msg}")
                    except ValueError:
                        response.raise_for_status()
                
                return response.json()
            except requests.exceptions.RequestException as e:
                if i == retries - 1:
                    raise e
                time.sleep(2)
        return None

    def get_google_ai_mode(self, keyword, location_name, language_code):
        endpoint = "/serp/google/ai_mode/live/advanced"
        payload = [{
            "keyword": keyword,
            "location_name": location_name,
            "language_code": language_code
        }]
        return self._post(endpoint, payload)

    def get_llm_response(self, platform, model_name, prompt):
        # platform can be: chat_gpt, perplexity, gemini, claude
        endpoint = f"/ai_optimization/{platform}/llm_responses/live"
        payload = [{
            "user_prompt": prompt,
            "model_name": model_name,
            "web_search": True,
            "max_output_tokens": 1000
        }]
        return self._post(endpoint, payload)

    def get_llm_mentions(self, brand_name, platform="google"):
        """
        Reverse lookup: Find search terms that mention the brand.
        Platform can be "google" or "chat_gpt".
        """
        endpoint = "/ai_optimization/llm_mentions/search/live"
        payload = [{
            "target": {"keyword": brand_name},
            "platform": platform
        }]
        return self._post(endpoint, payload)

    def parse_google_ai_mode(self, response, brand_domain, brand_name, competitors):
        """
        Parses Google AI Mode response.
        """
        try:
            tasks = response.get('tasks', [])
            if not tasks: return {"mentioned": None, "position": None, "sources": [], "ai_text": "No tasks in response", "competitor_mentions": {}}
            
            result_list = tasks[0].get('result', [])
            if not result_list: return {"mentioned": False, "position": None, "sources": [], "ai_text": "No AI Overview found", "competitor_mentions": {}}
            
            items = result_list[0].get('items', [])
            if not items: return {"mentioned": False, "position": None, "sources": [], "ai_text": "No items in AI Overview", "competitor_mentions": {}}
            
            item = items[0]
            ai_text = item.get('markdown', '')
            
            # Robustly handle references being None or missing
            refs = item.get('references', []) or []
            sources = [ref.get('url') for ref in refs if ref and ref.get('url')]
            
            return self._detect_mentions(ai_text, sources, brand_domain, brand_name, competitors)
        except Exception as e:
            return {"mentioned": None, "position": None, "sources": [], "ai_text": f"Parse Error: {repr(e)}", "competitor_mentions": {}}

    def parse_llm_response(self, response, brand_domain, brand_name, competitors):
        """
        Parses LLM (ChatGPT, Gemini, etc.) response.
        """
        try:
            tasks = response.get('tasks', [])
            if not tasks: return {"mentioned": None, "position": None, "sources": [], "ai_text": "No tasks in response", "competitor_mentions": {}}
            
            result_list = tasks[0].get('result', [])
            if not result_list: return {"mentioned": False, "position": None, "sources": [], "ai_text": "No result from LLM", "competitor_mentions": {}}
            
            items = result_list[0].get('items', [])
            if not items: return {"mentioned": False, "position": None, "sources": [], "ai_text": "Empty LLM items", "competitor_mentions": {}}
            
            item = items[0]
            sections = item.get('sections', [])
            ai_text = "\n".join([s.get('text', '') for s in sections if s.get('text')])
            
            sources = []
            for s in sections:
                for ann in s.get('annotations', []) or []:
                    if ann.get('url'):
                        sources.append(ann.get('url'))
            
            if not ai_text:
                return {"mentioned": False, "position": None, "sources": [], "ai_text": "LLM returned no text", "competitor_mentions": {}}

            return self._detect_mentions(ai_text, sources, brand_domain, brand_name, competitors)
        except Exception as e:
            return {"mentioned": None, "position": None, "sources": [], "ai_text": f"Parse Error: {repr(e)}", "competitor_mentions": {}}

    def _detect_mentions(self, text, sources, brand_domain, brand_name, competitors):
        # 1. Advanced Normalization
        # Remove zero-width spaces, unusual unicode, and strip
        text_clean = re.sub(r'[^\x20-\x7E\s]', '', text).strip()
        text_lower = text_clean.lower()
        
        brand_domain_l = brand_domain.lower() if brand_domain else ""
        brand_name_l = brand_name.lower() if brand_name else ""
        
        # Build fuzzy regex patterns for brand name
        # If name is "Deftsoft", catch "Deft soft", "Deft-soft", etc.
        patterns = []
        if brand_name_l:
            # Basic word
            patterns.append(re.escape(brand_name_l))
            # Split variants (Deft soft)
            if len(brand_name_l) > 4:
                half = len(brand_name_l) // 2
                patterns.append(re.escape(brand_name_l[:half]) + r'\s*' + re.escape(brand_name_l[half:]))
            # Catch common business suffixes even if not provided
            patterns.append(re.escape(brand_name_l) + r'\s*(pvt|ltd|pvt\s+ltd|inc|corp|group|informatics|solutions)')

        # 2. Check in text with Regex
        mentioned_in_text = False
        for pattern in patterns:
            if re.search(pattern, text_lower):
                mentioned_in_text = True
                break
        
        if not mentioned_in_text and brand_domain_l and brand_domain_l in text_lower:
            mentioned_in_text = True
            
        # 3. Check in sources (referential visibility)
        mentioned_in_sources = False
        for src in (sources or []):
            src_l = src.lower()
            if brand_domain_l and brand_domain_l in src_l:
                mentioned_in_sources = True
                break
            for pattern in patterns:
                if re.search(pattern, src_l):
                    mentioned_in_sources = True
                    break
            if mentioned_in_sources: break
                
        mentioned = mentioned_in_text or mentioned_in_sources
        
        # Determine position
        position = None
        if mentioned_in_text:
            earliest = len(text_lower)
            for pattern in patterns:
                m = re.search(pattern, text_lower)
                if m and m.start() < earliest:
                    earliest = m.start()
            
            if earliest < len(text_lower):
                position = text_clean[:earliest].count('\n') + 1

        competitor_mentions = {}
        for comp in competitors:
            comp_l = comp.lower()
            # Simple count for competitors (or could use same regex logic if needed)
            count = text_lower.count(comp_l)
            for src in (sources or []):
                if comp_l in src.lower():
                    count += 1
            if count > 0:
                competitor_mentions[comp] = count

        return {
            "mentioned": mentioned,
            "position": position,
            "sources": sources,
            "ai_text": text_clean,
            "competitor_mentions": competitor_mentions
        }



# Optional: psycopg2 for Postgres support on cloud
try:
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

class TrackerStorage:
    def __init__(self, db_path=None):
        self.db_url = os.environ.get('POSTGRES_URL') # Vercel Postgres provides this
        if not self.db_url:
            self.db_path = Path(db_path or "data/tracker.db")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.mode = 'sqlite'
        else:
            self.mode = 'postgres'
        self._init_db()

    def _get_connection(self):
        if self.mode == 'postgres':
            # Vercel Postgres usually uses 'postgres://' which psycopg2 requires to be 'postgresql://'
            url = self.db_url.replace("postgres://", "postgresql://")
            conn = psycopg2.connect(url)
            return conn
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            return conn

    def _init_db(self):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            
            # Helper for table creation syntax difference
            serial_pk = "SERIAL PRIMARY KEY" if self.mode == 'postgres' else "INTEGER PRIMARY KEY AUTOINCREMENT"
            
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS runs (
                    id {serial_pk},
                    brand_domain TEXT NOT NULL,
                    brand_name TEXT NOT NULL,
                    country TEXT NOT NULL,
                    language TEXT NOT NULL,
                    run_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS mention_results (
                    id {serial_pk},
                    run_id INTEGER NOT NULL,
                    keyword TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    mentioned BOOLEAN,
                    mention_position INTEGER,
                    sources_cited TEXT,
                    competitor_mentions TEXT,
                    ai_response_text TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS competitor_metrics (
                    id {serial_pk},
                    run_id INTEGER NOT NULL,
                    domain TEXT NOT NULL,
                    total_mentions INTEGER DEFAULT 0,
                    avg_position REAL,
                    share_of_voice REAL
                )
            """)
            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS discovery_results (
                    id {serial_pk},
                    brand_name TEXT NOT NULL,
                    keyword TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    mentions_count INTEGER DEFAULT 0,
                    quoted_links TEXT,
                    cross_platform_mentions TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
        finally:
            conn.close()

    def create_run(self, brand_domain, brand_name, country, language):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            if self.mode == 'postgres':
                cur.execute(
                    "INSERT INTO runs (brand_domain, brand_name, country, language) VALUES (%s, %s, %s, %s) RETURNING id",
                    (brand_domain, brand_name, country, language)
                )
                run_id = cur.fetchone()[0]
            else:
                cur.execute(
                    "INSERT INTO runs (brand_domain, brand_name, country, language) VALUES (?, ?, ?, ?)",
                    (brand_domain, brand_name, country, language)
                )
                run_id = cur.lastrowid
            conn.commit()
            return run_id
        finally:
            conn.close()

    def save_mention_result(self, run_id, keyword, platform, result):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            sql = """
                INSERT INTO mention_results (
                    run_id, keyword, platform, mentioned, mention_position, 
                    sources_cited, competitor_mentions, ai_response_text
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """ if self.mode == 'postgres' else """
                INSERT INTO mention_results (
                    run_id, keyword, platform, mentioned, mention_position, 
                    sources_cited, competitor_mentions, ai_response_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """
            cur.execute(sql, (
                run_id, keyword, platform, 
                result.get('mentioned'), result.get('position'),
                json.dumps(result.get('sources', [])),
                json.dumps(result.get('competitor_mentions', {})),
                result.get('ai_text')
            ))
            conn.commit()
        finally:
            conn.close()

    def get_run(self, run_id):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = "SELECT * FROM runs WHERE id = %s" if self.mode == 'postgres' else "SELECT * FROM runs WHERE id = ?"
            cur.execute(sql, (run_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_results(self, run_id):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = "SELECT * FROM mention_results WHERE run_id = %s" if self.mode == 'postgres' else "SELECT * FROM mention_results WHERE run_id = ?"
            cur.execute(sql, (run_id,))
            rows = cur.fetchall()
            results = []
            for row in rows:
                res = dict(row)
                res['sources_cited'] = json.loads(row['sources_cited']) if row['sources_cited'] else []
                res['competitor_mentions'] = json.loads(row['competitor_mentions']) if row['competitor_mentions'] else {}
                results.append(res)
            return results
        finally:
            conn.close()

    def save_competitor_metrics(self, run_id, metrics):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            sql = """
                INSERT INTO competitor_metrics (run_id, domain, total_mentions, avg_position, share_of_voice)
                VALUES (%s, %s, %s, %s, %s)
            """ if self.mode == 'postgres' else """
                INSERT INTO competitor_metrics (run_id, domain, total_mentions, avg_position, share_of_voice)
                VALUES (?, ?, ?, ?, ?)
            """
            for m in metrics:
                cur.execute(sql, (run_id, m['domain'], m['total_mentions'], m['avg_position'], m['share_of_voice']))
            conn.commit()
        finally:
            conn.close()

    def get_competitor_metrics(self, run_id):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = "SELECT * FROM competitor_metrics WHERE run_id = %s" if self.mode == 'postgres' else "SELECT * FROM competitor_metrics WHERE run_id = ?"
            cur.execute(sql, (run_id,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_history(self, brand_domain):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = """
                SELECT r.id, r.run_date, SUM(CASE WHEN m.mentioned THEN 1 ELSE 0 END) as total_mentions
                FROM runs r
                LEFT JOIN mention_results m ON r.id = m.run_id
                WHERE r.brand_domain = %s
                GROUP BY r.id, r.run_date
                ORDER BY r.run_date ASC
            """ if self.mode == 'postgres' else """
                SELECT r.id, r.run_date, SUM(m.mentioned) as total_mentions
                FROM runs r
                LEFT JOIN mention_results m ON r.id = m.run_id
                WHERE r.brand_domain = ?
                GROUP BY r.id
                ORDER BY r.run_date ASC
            """
            cur.execute(sql, (brand_domain,))
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def save_discovery_results(self, brand_name, platform, results):
        conn = self._get_connection()
        try:
            cur = conn.cursor()
            sql = """
                INSERT INTO discovery_results (brand_name, platform, keyword, mentions_count, quoted_links, cross_platform_mentions)
                VALUES (%s, %s, %s, %s, %s, %s)
            """ if self.mode == 'postgres' else """
                INSERT INTO discovery_results (brand_name, platform, keyword, mentions_count, quoted_links, cross_platform_mentions)
                VALUES (?, ?, ?, ?, ?, ?)
            """
            for item in results:
                cur.execute(sql, (
                    brand_name, platform, item.get('keyword'), 
                    item.get('mentions_count'), 
                    json.dumps(item.get('quoted_links', [])),
                    json.dumps(item.get('cross_platform_mentions', {}))
                ))
            conn.commit()
        finally:
            conn.close()

    def get_discovery_results(self, brand_name):
        conn = self._get_connection()
        try:
            cur = conn.cursor(cursor_factory=RealDictCursor) if self.mode == 'postgres' else conn.cursor()
            sql = "SELECT * FROM discovery_results WHERE brand_name = %s ORDER BY timestamp DESC" if self.mode == 'postgres' else "SELECT * FROM discovery_results WHERE brand_name = ? ORDER BY timestamp DESC"
            cur.execute(sql, (brand_name,))
            rows = cur.fetchall()
            results = []
            for row in rows:
                res = dict(row)
                res['quoted_links'] = json.loads(row['quoted_links']) if row['quoted_links'] else []
                res['cross_platform_mentions'] = json.loads(row['cross_platform_mentions']) if row['cross_platform_mentions'] else {}
                results.append(res)
            return results
        finally:
            conn.close()


import os
import json
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from flask import Flask, render_template, request, session, redirect, url_for, Response, jsonify, send_file



app = Flask(__name__)
app.secret_key = os.urandom(24)

# Paths
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "tracker.db"

# Initialize Storage
storage = TrackerStorage(DB_PATH)

PLATFORMS = [
    {"id": "google", "name": "Google AI Mode"},
    {"id": "chat_gpt", "name": "ChatGPT", "model": "gpt-4.1-mini"},
    {"id": "perplexity", "name": "Perplexity", "model": "sonar"},
    {"id": "gemini", "name": "Gemini", "model": "gemini-2.0-flash"},
    {"id": "claude", "name": "Claude", "model": "claude-haiku-4-5"}
]

@app.route("/")
def index():
    active_tab = request.args.get('tab', 'monitor')
    return render_template("setup.html", active_tab=active_tab)

@app.route("/api/run", methods=["POST"])
def run_tracker():
    data = request.get_json()
    
    # Store config in session
    session['credentials'] = {
        'login': data.get('api_login'),
        'password': data.get('api_password')
    }
    
    # Extract keywords (one per line)
    high_vol = [k.strip() for k in data.get('high_volume_keywords', '').split('\n') if k.strip()]
    brand_niche = [k.strip() for k in data.get('brand_niche_keywords', '').split('\n') if k.strip()]
    keywords = list(set(high_vol + brand_niche))
    
    competitors = [c.strip() for c in data.get('competitors', []) if c.strip()]
    
    config = {
        'brand_domain': data.get('brand_domain'),
        'brand_name': data.get('brand_name'),
        'country': data.get('country', 'India'),
        'location': data.get('location', data.get('country', 'India')),
        'language': data.get('language', 'en'),
        'competitors': competitors,
        'keywords': keywords
    }
    session['tracker_config'] = config
    
    return jsonify({"status": "success", "redirect": url_for('running')})

@app.route("/api/discover", methods=["POST"])
def discover_citations():
    data = request.get_json()
    brand_name = data.get('brand_name')
    creds = {
        'login': data.get('api_login'),
        'password': data.get('api_password')
    }
    
    if not brand_name or not creds['login']:
        return jsonify({"error": "Missing brand name or credentials"}), 400

    client = DataForSeoClient(creds['login'], creds['password'])
    discovery_platforms = ["google", "chat_gpt"] # Supported by mentions search API
    
    initial_keywords = []
    keyword_map = {} # keyword -> {original_platform, mentions_count, quoted_links}
    
    try:
        # Step 1: Surface Scan (Google/ChatGPT Mentions API)
        for platform in discovery_platforms:
            response = client.get_llm_mentions(brand_name, platform)
            if response and 'tasks' in response:
                result_list = response['tasks'][0].get('result', [])
                if result_list:
                    items = result_list[0].get('items', [])
                    for item in items:
                        kw = item.get('keyword')
                        if kw and kw not in keyword_map:
                            initial_keywords.append(kw)
                            keyword_map[kw] = {
                                "platform": platform,
                                "mentions_count": item.get('mentions_count', 0),
                                "quoted_links": item.get('quoted_links', [])
                            }
        
        if not initial_keywords:
            return jsonify({"status": "empty", "message": "No citations found for this brand."})

        # Step 2: Deep Verification (Cross-check discovered keywords on ALL platforms)
        # Assuming brand_domain might be unknown, we use name-based regex.
        # But for accuracy, prompt-based lookup is best.
        all_results = []
        
        def verify_kw(kw):
            cross_mentions = {}
            for p in PLATFORMS:
                try:
                    p_id = p['id']
                    if p_id == 'google':
                        # Default to India/en for cross-check discovery
                        resp = client.get_google_ai_mode(kw, "India", "en")
                        res = client.parse_google_ai_mode(resp, "", "", [])
                    else:
                        resp = client.get_llm_response(p_id, p['model'], kw)
                        res = client.parse_llm_response(resp, "", "", [])
                    cross_mentions[p_id] = res.get('mentioned', False)
                except Exception as e:
                    print(f"Deep check error ({p_id}): {e}")
                    cross_mentions[p['id']] = False
            return (kw, cross_mentions)

        # limit to top 10 discovered keywords to avoid huge latencies
        test_kws = initial_keywords[:10]
        with ThreadPoolExecutor(max_workers=10) as executor:
            future_to_kw = {executor.submit(verify_kw, kw): kw for kw in test_kws}
            for future in as_completed(future_to_kw):
                kw, cross_mentions = future.result()
                base = keyword_map[kw]
                all_results.append({
                    "keyword": kw,
                    "platform": base["platform"],
                    "mentions_count": base["mentions_count"],
                    "quoted_links": base["quoted_links"],
                    "cross_platform_mentions": cross_mentions
                })

        if all_results:
            storage.save_discovery_results(brand_name, "Deep Aggregated", all_results)
            return jsonify({"status": "success", "count": len(all_results)})
        else:
            return jsonify({"status": "empty", "message": "No verifiable citations found."})
            
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        return jsonify({"error": str(e)}), 500

@app.route("/discovery/<brand_name>")
def discovery_results(brand_name):
    results = storage.get_discovery_results(brand_name)
    return render_template("discovery_results.html", brand_name=brand_name, results=results)

@app.route("/running")
def running():
    if 'tracker_config' not in session:
        return redirect(url_for('index'))
    return render_template("running.html")

@app.route("/stream")
def stream():
    # SSE CRITICAL: Extract session data BEFORE the generator
    config = session.get('tracker_config')
    creds = session.get('credentials')
    
    if not config or not creds:
        return Response("data: {\"error\": \"Session expired\"}\n\n", mimetype='text/event-stream')

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'X-Accel-Buffering': 'no',
        'Connection': 'keep-alive',
        'Transfer-Encoding': 'chunked'
    }

    run_id = storage.create_run(
        config['brand_domain'], 
        config['brand_name'], 
        config['country'], 
        config['language']
    )
    session['last_run_id'] = run_id

    def generate(config, creds, run_id):
        yield "data: {\"message\": \"Establishing secure connection...\", \"progress\": 2}\n\n"
        
        client = DataForSeoClient(creds['login'], creds['password'])
        keywords = config['keywords']
        brand_domain = config['brand_domain']
        brand_name = config['brand_name']
        competitors = config['competitors']
        total_steps = len(keywords) * len(PLATFORMS)
        completed_steps = 0
        db_lock = Lock()

        tasks = []
        for keyword in keywords:
            for platform in PLATFORMS:
                tasks.append((keyword, platform))

        def process_task(task):
            keyword, platform = task
            platform_id = platform['id']
            try:
                if platform_id == 'google':
                    response = client.get_google_ai_mode(keyword, config['location'], config['language'])
                    result = client.parse_google_ai_mode(response, brand_domain, brand_name, competitors)
                else:
                    response = client.get_llm_response(platform_id, platform['model'], keyword)
                    result = client.parse_llm_response(response, brand_domain, brand_name, competitors)
                
                with db_lock:
                    storage.save_mention_result(run_id, keyword, platform_id, result)
                return (keyword, platform['name'], result, None)
            except Exception as e:
                with db_lock:
                    storage.save_mention_result(run_id, keyword, platform_id, {"mentioned": None, "ai_text": str(e)})
                return (keyword, platform['name'], None, str(e))

        # Use ThreadPoolExecutor for parallel API calls (Increased to 20 workers for speed)
        with ThreadPoolExecutor(max_workers=20) as executor:
            future_to_task = {executor.submit(process_task, t): t for t in tasks}
            
            for future in as_completed(future_to_task):
                completed_steps += 1
                keyword, platform_name, result, error = future.result()
                
                progress = int((completed_steps / total_steps) * 100)
                msg = f"[{completed_steps}/{total_steps}] \"{keyword}\" → {platform_name}..."
                
                if error:
                    msg += f" ✗ error: {error}"
                else:
                    msg += f" ✓ {'mentioned' if result['mentioned'] else 'not mentioned'}"
                    if result['mentioned'] and result['position']:
                        msg += f" (pos: {result['position']})"

                payload = {
                    "progress": progress,
                    "message": msg,
                    "status": "running"
                }
                yield f"data: {json.dumps(payload)}\n\n"

        # After all checks, compute competitor share of voice
        results = storage.get_results(run_id)
        domains = [brand_domain] + competitors
        total_checks = len(keywords) * len(PLATFORMS)
        
        comp_metrics = []
        for domain in domains:
            mentions = 0
            positions = []
            for res in results:
                if domain == brand_domain:
                    if res['mentioned']:
                        mentions += 1
                        if res['mention_position']: positions.append(res['mention_position'])
                else:
                    count = res['competitor_mentions'].get(domain, 0)
                    if count > 0:
                        mentions += 1
            
            sov = (mentions / total_checks) * 100 if total_checks > 0 else 0
            avg_pos = sum(positions) / len(positions) if positions else 0
            
            comp_metrics.append({
                "domain": domain,
                "total_mentions": mentions,
                "avg_position": avg_pos,
                "share_of_voice": sov
            })
        
        storage.save_competitor_metrics(run_id, comp_metrics)

        # Final message
        yield f"data: {json.dumps({'progress': 100, 'message': 'Completed!', 'status': 'done', 'run_id': run_id})}\n\n"

    return Response(generate(config, creds, run_id), headers=headers)

@app.route("/dashboard/<int:run_id>")
def dashboard(run_id):
    run = storage.get_run(run_id)
    if not run:
        return "Run not found", 404
    
    results = storage.get_results(run_id)
    comp_metrics = storage.get_competitor_metrics(run_id)
    history = storage.get_history(run['brand_domain'])
    
    # Platform breakdown
    platform_data = {}
    for p in PLATFORMS:
        mentions = sum(1 for r in results if r['platform'] == p['id'] and r['mentioned'])
        platform_data[p['name']] = mentions

    all_competitor_mentions = {}
    for r in results:
        for comp, count in r['competitor_mentions'].items():
            all_competitor_mentions[comp] = all_competitor_mentions.get(comp, 0) + count
    top_competitors = sorted(all_competitor_mentions.items(), key=lambda x: x[1], reverse=True)[:5]

    # Convert Row objects to dicts for JSON serialization in template
    run_dict = dict(run)
    comp_metrics_list = [dict(row) for row in comp_metrics]
    history_list = [dict(row) for row in history]
    
    return render_template(
        "dashboard.html",
        run=run_dict,
        results=results,
        platforms=PLATFORMS,
        comp_metrics=comp_metrics_list,
        history=history_list,
        platform_data=platform_data,
        top_competitors=top_competitors
    )

@app.route("/download/<int:run_id>")
def download_report(run_id):
    run = storage.get_run(run_id)
    if not run:
        return "Run not found", 404
    
    results = storage.get_results(run_id)
    comp_metrics = storage.get_competitor_metrics(run_id)
    history = storage.get_history(run['brand_domain'])
    
    platform_data = {}
    for p in PLATFORMS:
        mentions = sum(1 for r in results if r['platform'] == p['id'] and r['mentioned'])
        platform_data[p['name']] = mentions
        
    all_competitor_mentions = {}
    for r in results:
        for comp, count in r['competitor_mentions'].items():
            all_competitor_mentions[comp] = all_competitor_mentions.get(comp, 0) + count
    top_competitors = sorted(all_competitor_mentions.items(), key=lambda x: x[1], reverse=True)[:5]

    # Convert for report
    run_dict = dict(run)
    comp_metrics_list = [dict(row) for row in comp_metrics]
    history_list = [dict(row) for row in history]

    # Render as static
    html_content = render_template(
        "dashboard.html",
        run=run_dict,
        results=results,
        platforms=PLATFORMS,
        comp_metrics=comp_metrics_list,
        history=history_list,
        platform_data=platform_data,
        top_competitors=top_competitors,
        is_report=True
    )
    
    report_filename = f"AI-Mention-Report-{run['brand_domain']}-{datetime.now().strftime('%Y-%m-%d')}.html"
    report_path = DATA_DIR / report_filename
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    
    return send_file(report_path, as_attachment=True)

if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    port = int(os.environ.get("PORT", 5000))
    
    print(f"\n+ AI Mention Tracker is starting on port {port}...")
    
    # Only open browser locally
    if os.environ.get("VERCEL") is None and os.environ.get("RENDER") is None:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}")
        except:
            pass
            
    app.run(host="0.0.0.0", port=port)

