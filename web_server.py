#!/usr/bin/env python3
"""
Wetzel CRM - Web Interface
Run with: python3 web_server.py
Then open: http://localhost:8000
"""

import http.server
import socket
import socketserver
import sqlite3
import json
import urllib.parse
from datetime import date, datetime, timedelta
import threading
import time
import hashlib
import secrets
import os
from http.cookies import SimpleCookie

PORT = 8000
DB_PATH = "recruiter_contacts.db"
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

def migrate_database():
    """Add new columns for Asana-style To Do's if they don't exist"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    columns = [row[1] for row in cursor.execute("PRAGMA table_info(followup_tasks)").fetchall()]
    if 'status' not in columns:
        cursor.execute("ALTER TABLE followup_tasks ADD COLUMN status TEXT DEFAULT 'on_track'")
    if 'section' not in columns:
        cursor.execute("ALTER TABLE followup_tasks ADD COLUMN section TEXT DEFAULT 'todo'")
        cursor.execute("UPDATE followup_tasks SET section = 'done' WHERE completed = 1")
        cursor.execute("UPDATE followup_tasks SET section = 'todo' WHERE completed = 0 OR completed IS NULL")
    if 'assignee' not in columns:
        cursor.execute("ALTER TABLE followup_tasks ADD COLUMN assignee TEXT DEFAULT NULL")

    # Replace grid criteria with new 250-pt framework
    cursor.execute("SELECT COUNT(*) FROM grid_criteria WHERE grid_id = 1")
    current_count = cursor.fetchone()[0]
    new_criteria = [
        # (criteria_name, max_score, row_order, category)
        # ROLE FIT — 70 pts
        ('Commercial underperformance problem (broken/missing revenue engine)', 25, 10, 'ROLE FIT'),
        ('Revenue ownership scope (sales + success + partnerships, not just sales)', 20, 11, 'ROLE FIT'),
        ('Stage match (Builder $5-200M Series B-D / Scaler $200-500M Late-Stage)', 15, 12, 'ROLE FIT'),
        ('C-suite seat with real authority (not functional VP inside a larger structure)', 10, 13, 'ROLE FIT'),
        # COMPANY QUALITY — 50 pts
        ('CEO GTM alignment (growth advocate, trusts the commercial leader)', 20, 20, 'COMPANY QUALITY'),
        ('PMF + product signal (NRR >120, retention, customer love)', 15, 21, 'COMPANY QUALITY'),
        ('Market size & trajectory', 15, 22, 'COMPANY QUALITY'),
        # CULTURE — 25 pts
        ('Results-accountable, fast-moving (not consensus/process-first)', 15, 30, 'CULTURE'),
        ('Board + leadership alignment', 10, 31, 'CULTURE'),
        # COMPENSATION — 30 pts
        ('Cash comp (base + target bonus)', 15, 40, 'COMPENSATION'),
        ('Equity upside (meaningful at stage)', 15, 41, 'COMPENSATION'),
        # PASSION / GUT — 25 pts
        ('Genuine excitement about the product and market', 15, 50, 'PASSION / GUT'),
        ('Gut feeling (override factor — weighted high so a strong gut reaction actually moves the needle)', 10, 51, 'PASSION / GUT'),
        # METRICS — 50 pts
        ('YoY Growth %', 20, 60, 'METRICS'),
        ('NRR', 20, 61, 'METRICS'),
        ('GRR', 10, 62, 'METRICS'),
    ]
    expected_names = {c[0] for c in new_criteria}
    cursor.execute("SELECT criteria_name FROM grid_criteria WHERE grid_id = 1")
    existing_names = {row[0] for row in cursor.fetchall()}
    if existing_names != expected_names:
        cursor.execute("DELETE FROM grid_criteria WHERE grid_id = 1")
        for name, score, order, cat in new_criteria:
            cursor.execute(
                "INSERT INTO grid_criteria (grid_id, criteria_name, max_score, row_order, category) VALUES (?, ?, ?, ?, ?)",
                (1, name, score, order, cat)
            )
        print("✅ Grid criteria updated to 250-pt framework")

    conn.commit()
    conn.close()
    print("✅ Database migration check complete")

# Session storage: {token: {"username": str, "expires": datetime}}
sessions = {}

def load_config():
    """Load authentication config"""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"username": "admin", "password_hash": hashlib.sha256(b"admin").hexdigest()}

def verify_credentials(username, password):
    """Check username and password against config"""
    config = load_config()
    password_hash = hashlib.sha256(password.encode()).hexdigest()
    return username == config["username"] and password_hash == config["password_hash"]

def create_session(username):
    """Create a new session token"""
    token = secrets.token_hex(32)
    sessions[token] = {
        "username": username,
        "expires": datetime.now() + timedelta(hours=24)
    }
    return token

def validate_session(token):
    """Check if a session token is valid"""
    if token not in sessions:
        return False
    session = sessions[token]
    if datetime.now() > session["expires"]:
        del sessions[token]
        return False
    # Refresh expiry on activity
    session["expires"] = datetime.now() + timedelta(hours=24)
    return True

def get_session_token(cookie_header):
    """Extract session token from Cookie header"""
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    if "session" in cookie:
        return cookie["session"].value
    return None


class DatabaseHandler(http.server.SimpleHTTPRequestHandler):
    """Custom HTTP handler for database operations"""

    def is_authenticated(self):
        """Check if the current request has a valid session"""
        cookie_header = self.headers.get('Cookie')
        token = get_session_token(cookie_header)
        return token and validate_session(token)

    def require_auth(self):
        """Check auth and send 401 if not authenticated. Returns True if blocked."""
        if not self.is_authenticated():
            self.send_response(401)
            self.send_header('Content-type', 'application/json')
            origin = self.headers.get('Origin', '')
            if origin:
                self.send_header('Access-Control-Allow-Origin', origin)
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Unauthorized"}).encode())
            return True
        return False

    def do_GET(self):
        """Handle GET requests"""
        # Public routes (no auth required)
        if self.path == '/login':
            self.send_login_page()
            return
        elif self.path == '/static/logo.png':
            self.serve_logo()
            return
        elif self.path == '/logout':
            cookie_header = self.headers.get('Cookie')
            token = get_session_token(cookie_header)
            if token and token in sessions:
                del sessions[token]
            self.send_response(302)
            self.send_header('Location', '/login')
            self.send_header('Set-Cookie', 'session=; Path=/; Max-Age=0')
            self.end_headers()
            return

        # All other routes require authentication
        if not self.is_authenticated():
            self.send_response(302)
            self.send_header('Location', '/login')
            self.end_headers()
            return

        if self.path == '/' or self.path == '/index.html':
            self.send_html_page()
        elif self.path == '/api/contacts':
            self.get_all_contacts()
        elif self.path == '/api/companies':
            self.get_companies()
        elif self.path == '/api/relationship_types':
            self.get_relationship_types()
        elif self.path.startswith('/api/search?'):
            self.search_contacts()
        elif self.path.startswith('/api/contacts_by_relationship?'):
            self.get_contacts_by_relationship()
        elif self.path == '/api/stats':
            self.get_statistics()
        elif self.path == '/api/followups':
            self.get_all_followups()
        elif self.path == '/api/followups/active':
            self.get_active_followups()
        elif self.path == '/api/followups/completed':
            self.get_completed_followups()
        elif self.path == '/api/opportunity-grid':
            self.get_opportunity_grid()
        elif self.path.startswith('/api/opportunity/'):
            self.get_opportunity_detail()
        elif self.path == '/api/grid-companies':
            self.get_grid_companies()
        elif self.path == '/api/grid-criteria':
            self.get_grid_criteria()
        elif self.path == '/api/okrs/annual':
            self.get_annual_okrs()
        elif self.path.startswith('/api/okrs/quarterly?'):
            self.get_quarterly_okrs()
        elif self.path == '/api/okrs/categories':
            self.get_okr_categories()
        elif self.path == '/api/okrs/goal-types':
            self.get_okr_goal_types()
        elif self.path.startswith('/api/okrs/get?'):
            self.get_single_okr()
        else:
            self.send_error(404)

    def do_POST(self):
        """Handle POST requests"""
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length).decode('utf-8')
        data = json.loads(post_data) if post_data else {}

        # Login endpoint is public
        if self.path == '/api/login':
            self.handle_login(data)
            return

        # All other POST routes require authentication
        if self.require_auth():
            return

        if self.path == '/api/contacts':
            self.add_contact_from_extension(data)
        elif self.path == '/api/add_contact':
            self.add_contact(data)
        elif self.path == '/api/add_relationship':
            self.add_relationship(data)
        elif self.path == '/api/update_contact':
            self.update_contact(data)
        elif self.path == '/api/append_note':
            self.append_note(data)
        elif self.path == '/api/add_followup':
            self.add_followup(data)
        elif self.path == '/api/update_followup':
            self.update_followup(data)
        elif self.path == '/api/delete_followup':
            self.delete_followup(data)
        elif self.path == '/api/complete_followup':
            self.complete_followup(data)
        elif self.path == '/api/add-grid-company':
            self.add_grid_company(data)
        elif self.path == '/api/update-grid-company':
            self.update_grid_company(data)
        elif self.path == '/api/delete-grid-company':
            self.delete_grid_company(data)
        elif self.path == '/api/add-grid-criteria':
            self.add_grid_criteria(data)
        elif self.path == '/api/update-grid-criteria':
            self.update_grid_criteria(data)
        elif self.path == '/api/delete-grid-criteria':
            self.delete_grid_criteria(data)
        elif self.path == '/api/toggle-grid-score':
            self.toggle_grid_score(data)
        elif self.path == '/api/set-grid-score':
            self.set_grid_score(data)
        elif self.path == '/api/okrs/add':
            self.add_okr(data)
        elif self.path == '/api/okrs/update':
            self.update_okr(data)
        elif self.path == '/api/okrs/delete':
            self.delete_okr(data)
        elif self.path == '/api/okrs/add-progress':
            self.add_okr_progress(data)
        elif self.path == '/api/okrs/update-annual-progress':
            self.update_annual_progress(data)
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """Handle OPTIONS requests for CORS preflight"""
        self.send_response(200)
        origin = self.headers.get('Origin', '*')
        self.send_header('Access-Control-Allow-Origin', origin)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Credentials', 'true')
        self.end_headers()

    def handle_login(self, data):
        """Handle login POST request"""
        username = data.get('username', '')
        password = data.get('password', '')

        if verify_credentials(username, password):
            token = create_session(username)
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Set-Cookie', f'session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=86400')
            self.send_header('Access-Control-Allow-Origin', self.headers.get('Origin', '*'))
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode())
        else:
            self.send_response(401)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', self.headers.get('Origin', '*'))
            self.send_header('Access-Control-Allow-Credentials', 'true')
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid username or password"}).encode())

    def send_login_page(self):
        """Send the login page"""
        html = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Wetzel CRM - Login</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #0F172A;
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            -webkit-font-smoothing: antialiased;
        }
        .login-container {
            background: #FFFFFF;
            padding: 2rem;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.35), 0 4px 16px rgba(0,0,0,0.2);
            width: 100%;
            max-width: 400px;
            text-align: center;
            border: 1px solid rgba(226, 232, 240, 0.6);
        }
        .login-logo {
            width: 150px;
            height: auto;
            margin-bottom: 1.5rem;
        }
        .login-card-header {
            margin-bottom: 1.5rem;
        }
        .login-card-header h1 {
            font-size: 1.2rem;
            font-weight: 700;
            color: #0F172A;
            letter-spacing: -0.02em;
            margin-bottom: 0.25rem;
        }
        .login-card-header p {
            font-size: 0.82rem;
            color: #64748B;
        }
        .login-form { text-align: left; }
        .form-group {
            margin-bottom: 1rem;
        }
        .form-group label {
            display: block;
            margin-bottom: 0.4rem;
            font-weight: 600;
            color: #0F172A;
            font-size: 0.8rem;
            letter-spacing: 0.01em;
        }
        .form-group input {
            width: 100%;
            padding: 0.65rem 0.9rem;
            border: 1.5px solid #E2E8F0;
            border-radius: 10px;
            font-size: 0.9rem;
            font-family: inherit;
            transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
            background: #F4F6F8;
            color: #0F172A;
        }
        .form-group input::placeholder { color: #94A3B8; }
        .form-group input:focus {
            outline: none;
            border-color: #3B82F6;
            background: #FFFFFF;
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.12);
        }
        .login-btn {
            width: 100%;
            padding: 0.75rem 1rem;
            background: #3B82F6;
            color: white;
            border: none;
            border-radius: 10px;
            font-size: 0.9rem;
            font-weight: 600;
            font-family: inherit;
            cursor: pointer;
            margin-top: 0.5rem;
            transition: background 0.2s, transform 0.15s, box-shadow 0.2s;
            box-shadow: 0 4px 12px rgba(59, 130, 246, 0.3);
        }
        .login-btn:hover { background: #2563EB; transform: translateY(-1px); box-shadow: 0 6px 16px rgba(59, 130, 246, 0.38); }
        .login-btn:active { transform: translateY(0); }
        .login-btn:disabled { background: #94A3B8; cursor: not-allowed; transform: none; box-shadow: none; }
        .error-msg {
            display: none;
            background: rgba(239, 68, 68, 0.08);
            color: #B91C1C;
            padding: 0.65rem 0.9rem;
            border-radius: 8px;
            margin-bottom: 1rem;
            font-size: 0.82rem;
            font-weight: 500;
            border: 1px solid rgba(239, 68, 68, 0.18);
        }
    </style>
</head>
<body>
    <div class="login-container">
        <img src="/static/logo.png" alt="Wetzel CRM" class="login-logo">
        <div class="login-card-header">
            <h1>Welcome back</h1>
            <p>Sign in to your CRM workspace</p>
        </div>
        <form class="login-form" onsubmit="handleLogin(event)">
            <div id="errorMsg" class="error-msg"></div>
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autocomplete="username" placeholder="your username">
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required autocomplete="current-password" placeholder="••••••••">
            </div>
            <button type="submit" class="login-btn" id="loginBtn">Sign In</button>
        </form>
    </div>
    <script>
        async function handleLogin(e) {
            e.preventDefault();
            const btn = document.getElementById('loginBtn');
            const errDiv = document.getElementById('errorMsg');
            btn.disabled = true;
            btn.textContent = 'Signing in...';
            errDiv.style.display = 'none';

            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'include',
                    body: JSON.stringify({
                        username: document.getElementById('username').value,
                        password: document.getElementById('password').value
                    })
                });

                const data = await response.json();

                if (response.ok && data.success) {
                    window.location.href = '/';
                } else {
                    errDiv.textContent = data.error || 'Invalid credentials';
                    errDiv.style.display = 'block';
                }
            } catch (error) {
                errDiv.textContent = 'Connection error. Please try again.';
                errDiv.style.display = 'block';
            }

            btn.disabled = false;
            btn.textContent = 'Sign In';
        }
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.end_headers()
        self.wfile.write(html.encode())

    def send_html_page(self):
        """Send the main HTML page"""
        html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Wetzel CRM</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            /* ── Wetzel CRM Brand Kit v2.0 ── */

            /* Primary Accent */
            --wz-accent: #3B82F6;
            --wz-accent-hover: #2563EB;
            --wz-accent-active: #1D4ED8;
            --wz-accent-light: rgba(59, 130, 246, 0.10);

            /* Neutrals */
            --wz-black: #0F172A;
            --wz-dark-gray: #1E293B;
            --wz-mid-gray: #64748B;
            --wz-light-gray: #E2E8F0;
            --wz-pale-gray: #F4F6F8;
            --wz-white: #FFFFFF;

            /* Semantic */
            --wz-error: #EF4444;
            --wz-error-bg: rgba(239, 68, 68, 0.08);
            --wz-warning: #F59E0B;
            --wz-warning-bg: rgba(245, 158, 11, 0.08);
            --wz-success: #10B981;
            --wz-success-bg: rgba(16, 185, 129, 0.08);

            /* Typography */
            --wz-font: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;

            /* Spacing (4px base) */
            --wz-space-2xs: 4px;
            --wz-space-xs: 8px;
            --wz-space-sm: 12px;
            --wz-space-md: 16px;
            --wz-space-lg: 24px;
            --wz-space-xl: 32px;
            --wz-space-2xl: 48px;

            /* Border Radii */
            --wz-radius-xs: 2px;
            --wz-radius-sm: 8px;
            --wz-radius-md: 10px;
            --wz-radius-lg: 16px;
            --wz-radius-xl: 20px;
            --wz-radius-pill: 9999px;

            /* Shadows */
            --wz-shadow-1: 0 4px 6px -1px rgba(15, 23, 42, 0.04), 0 2px 4px -2px rgba(15, 23, 42, 0.03);
            --wz-shadow-2: 0 8px 16px -4px rgba(15, 23, 42, 0.06), 0 4px 8px -4px rgba(15, 23, 42, 0.04);
            --wz-shadow-3: 0 16px 32px -8px rgba(15, 23, 42, 0.10);
            --wz-shadow-4: 0 24px 48px -12px rgba(15, 23, 42, 0.16);

            /* ── Legacy aliases (used throughout existing CSS) ── */
            --bg-primary: var(--wz-pale-gray);
            --bg-secondary: var(--wz-white);
            --bg-tertiary: var(--wz-pale-gray);
            --bg-sidebar: #0F172A;
            --bg-hover: rgba(255,255,255,0.06);
            --text-primary: var(--wz-black);
            --text-secondary: var(--wz-mid-gray);
            --text-tertiary: var(--wz-mid-gray);
            --border-primary: var(--wz-light-gray);
            --border-secondary: var(--wz-light-gray);
            --accent-primary: var(--wz-accent);
            --accent-hover: var(--wz-accent-hover);
            --accent-purple: #7c5cbf;

            /* Priority tags (light theme) */
            --priority-high-bg: var(--wz-error-bg);
            --priority-high-text: var(--wz-error);
            --priority-medium-bg: var(--wz-warning-bg);
            --priority-medium-text: var(--wz-warning);
            --priority-low-bg: var(--wz-accent-light);
            --priority-low-text: var(--wz-accent);

            /* Status tags (light theme) */
            --status-on-track-bg: var(--wz-success-bg);
            --status-on-track-text: #1a7a38;
            --status-at-risk-bg: var(--wz-warning-bg);
            --status-at-risk-text: var(--wz-warning);
            --status-off-track-bg: var(--wz-error-bg);
            --status-off-track-text: var(--wz-error);

            --shadow: rgba(0, 0, 0, 0.08);
            --overlay: rgba(0, 0, 0, 0.5);
        }

        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: var(--wz-font);
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
            -webkit-font-smoothing: antialiased;
            font-size: 13px;
            line-height: 1.55;
        }

        /* Sidebar Navigation */
        .sidebar {
            width: 250px;
            background: var(--bg-sidebar);
            color: #94A3B8;
            position: fixed;
            height: 100vh;
            left: 0;
            top: 0;
            overflow-y: auto;
            z-index: 1000;
            border-right: 1px solid rgba(255,255,255,0.06);
        }

        .sidebar-header {
            padding: 20px 10px;
            background: var(--bg-sidebar);
            border-bottom: 1px solid rgba(255,255,255,0.06);
            display: flex;
            justify-content: center;
            align-items: center;
        }

        .sidebar-header a {
            text-decoration: none;
            display: block;
            width: 100%;
        }

        .sidebar-header img {
            width: 100%;
            height: auto;
            cursor: pointer;
            transition: opacity 0.2s;
        }

        .sidebar-header img:hover {
            opacity: 0.85;
        }

        .nav-menu {
            padding: 8px 10px;
        }

        .nav-item {
            padding: 9px 12px;
            color: #94A3B8;
            cursor: pointer;
            transition: background 0.15s, color 0.15s;
            border-radius: var(--wz-radius-sm);
            margin: 1px 0;
            display: flex;
            align-items: center;
            gap: 9px;
            font-size: 13px;
            font-weight: 400;
            letter-spacing: 0.01em;
        }

        .nav-item:hover {
            background: rgba(255,255,255,0.07);
            color: #CBD5E1;
        }

        .nav-item.active {
            background: rgba(59, 130, 246, 0.18);
            color: #FFFFFF;
            font-weight: 500;
        }

        .nav-icon {
            font-size: 15px;
            width: 18px;
            text-align: center;
            flex-shrink: 0;
        }

        /* Main Content Area */
        .main-wrapper {
            margin-left: 250px;
            flex: 1;
            min-height: 100vh;
            background: var(--wz-pale-gray);
        }

        .top-bar {
            background: var(--wz-white);
            padding: 0 var(--wz-space-lg);
            height: 56px;
            border-bottom: 1px solid var(--wz-light-gray);
            display: flex;
            align-items: center;
            box-shadow: 0 1px 0 rgba(15, 23, 42, 0.06);
        }

        .top-bar h2 {
            color: var(--text-primary);
            font-size: 18px;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .content-area {
            padding: 30px;
        }

        /* Page View Containers */
        .page-view {
            display: none;
        }

        .page-view.active {
            display: block;
        }

        /* Dashboard Specific Styles */
        .quick-stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }

        .stat-card {
            background: var(--wz-white);
            padding: 28px var(--wz-space-lg);
            border-radius: var(--wz-radius-lg);
            box-shadow: var(--wz-shadow-1);
            transition: box-shadow 0.2s, transform 0.15s;
            cursor: pointer;
            text-align: center;
            text-decoration: none;
            display: block;
            color: inherit;
            border: 1px solid rgba(226, 232, 240, 0.8);
        }

        .stat-card:hover {
            box-shadow: var(--wz-shadow-2);
            transform: translateY(-2px);
        }

        .stat-number {
            font-size: 32px;
            font-weight: 700;
            color: var(--wz-accent);
            margin-bottom: 8px;
            letter-spacing: -0.03em;
            font-variant-numeric: tabular-nums;
        }

        .stat-label {
            color: var(--text-secondary);
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            font-weight: 600;
        }

        /* Dashboard Grid Layout */
        .dashboard-grid {
            display: grid;
            grid-template-columns: 60% 40%;
            gap: 20px;
            margin-bottom: 30px;
        }

        .dashboard-section {
            background: var(--wz-white);
            border-radius: var(--wz-radius-lg);
            box-shadow: var(--wz-shadow-1);
            padding: var(--wz-space-lg);
            border: 1px solid rgba(226, 232, 240, 0.8);
            overflow: hidden;
        }

        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 0;
            border-bottom: none;
        }

        .section-title {
            font-size: 15px;
            font-weight: 700;
            color: var(--text-primary);
            letter-spacing: -0.01em;
        }

        .section-action {
            color: var(--accent-primary);
            font-size: 12px;
            font-weight: 500;
            cursor: pointer;
            text-decoration: none;
            padding: 4px 10px;
            border-radius: var(--wz-radius-pill);
            background: var(--wz-accent-light);
            transition: background 0.15s;
        }

        .section-action:hover {
            background: rgba(59, 130, 246, 0.18);
            text-decoration: none;
        }

        /* To-Do Item */
        .todo-item {
            padding: 15px;
            border-bottom: 1px solid var(--border-secondary);
            display: flex;
            align-items: center;
            gap: 15px;
            transition: background 0.2s;
        }

        .todo-item:hover {
            background: var(--bg-tertiary);
        }

        .todo-item:last-child {
            border-bottom: none;
        }

        .todo-checkbox {
            width: 20px;
            height: 20px;
            cursor: pointer;
        }

        .todo-content {
            flex: 1;
        }

        .todo-title {
            font-size: 14px;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .todo-meta {
            font-size: 12px;
            color: var(--text-tertiary);
        }

        .todo-priority {
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 500;
        }

        .priority-high {
            background: var(--priority-high-bg);
            color: var(--priority-high-text);
        }

        .priority-medium {
            background: var(--priority-medium-bg);
            color: var(--priority-medium-text);
        }

        .priority-low {
            background: var(--priority-low-bg);
            color: var(--priority-low-text);
        }

        /* Opportunity Item */
        .opp-item {
            padding: 15px;
            border-bottom: 1px solid var(--border-secondary);
            transition: background 0.2s;
            cursor: pointer;
        }

        .opp-item:hover {
            background: var(--bg-tertiary);
        }

        .opp-item:last-child {
            border-bottom: none;
        }

        .opp-company {
            font-size: 15px;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 6px;
        }

        .opp-score {
            display: inline-block;
            padding: 3px 12px;
            border-radius: var(--wz-radius-pill);
            font-size: 11px;
            font-weight: 700;
            background: var(--wz-accent-light);
            color: var(--wz-accent-hover);
            letter-spacing: 0.02em;
        }

        .opp-score.high {
            background: rgba(16, 185, 129, 0.10);
            color: #059669;
        }

        .opp-score.critical {
            background: rgba(239, 68, 68, 0.08);
            color: #DC2626;
        }

        /* Contact Item */
        .contact-item {
            padding: 15px;
            border-bottom: 1px solid var(--border-secondary);
            display: flex;
            justify-content: space-between;
            align-items: center;
            transition: background 0.2s;
        }

        .contact-item:hover {
            background: var(--bg-tertiary);
        }

        .contact-item:last-child {
            border-bottom: none;
        }

        .contact-info {
            flex: 1;
        }

        .contact-name {
            font-size: 15px;
            font-weight: 500;
            color: var(--text-primary);
            margin-bottom: 4px;
        }

        .contact-role {
            font-size: 13px;
            color: var(--text-secondary);
        }

        .contact-actions {
            display: flex;
            gap: 10px;
        }

        .btn-sm {
            padding: 5px 12px;
            font-size: 12px;
            font-weight: 500;
            border: 1px solid var(--wz-light-gray);
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
            border-radius: var(--wz-radius-pill);
            cursor: pointer;
            transition: all 0.15s;
            font-family: var(--wz-font);
        }

        .btn-sm:hover {
            background: var(--wz-accent);
            color: white;
            border-color: var(--wz-accent);
        }

        /* Form Elements from Original */
        .form-group {
            margin-bottom: 20px;
        }

        label {
            display: block;
            margin-bottom: 5px;
            color: var(--text-secondary);
            font-weight: 500;
        }

        input[type="text"],
        input[type="email"],
        input[type="tel"],
        input[type="url"],
        input[type="number"],
        input[type="datetime-local"],
        select,
        textarea {
            width: 100%;
            padding: 10px 14px;
            border: 1.5px solid var(--wz-light-gray);
            border-radius: var(--wz-radius-md);
            font-size: 13px;
            font-family: var(--wz-font);
            transition: border-color 0.2s, box-shadow 0.2s, background 0.2s;
            background: var(--wz-pale-gray);
            color: var(--text-primary);
        }

        input:focus,
        select:focus,
        textarea:focus {
            outline: none;
            border-color: var(--wz-accent);
            background: var(--wz-white);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.10);
        }

        textarea {
            min-height: 80px;
            resize: vertical;
        }

        .checkbox-group {
            display: flex;
            flex-wrap: wrap;
            gap: 15px;
        }

        .checkbox-label {
            display: flex;
            align-items: center;
            gap: 8px;
            cursor: pointer;
        }

        .checkbox-label input {
            width: auto;
        }

        .btn {
            padding: 0 var(--wz-space-md);
            border: none;
            border-radius: var(--wz-radius-pill);
            height: 34px;
            font-size: 13px;
            font-family: var(--wz-font);
            cursor: pointer;
            transition: background 0.15s, box-shadow 0.15s, transform 0.15s;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .btn-primary {
            background: var(--wz-accent);
            color: white;
            border: none;
            box-shadow: 0 3px 10px rgba(59, 130, 246, 0.28);
        }

        .btn-primary:hover {
            background: var(--wz-accent-hover);
            box-shadow: 0 4px 14px rgba(59, 130, 246, 0.38);
            transform: translateY(-1px);
        }

        .btn-secondary {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
            border: 1px solid var(--wz-light-gray);
        }

        .btn-secondary:hover {
            background: var(--wz-light-gray);
            color: var(--wz-black);
        }

        .search-box {
            width: 100%;
            padding: 15px;
            border: 1px solid var(--border-primary);
            border-radius: 10px;
            font-size: 16px;
            margin-bottom: 20px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
        }

        .contacts-grid {
            display: grid;
            gap: 15px;
        }

        .contact-card {
            background: var(--wz-white);
            padding: 20px;
            border-radius: var(--wz-radius-lg);
            transition: box-shadow 0.2s, transform 0.15s;
            border: 1px solid rgba(226, 232, 240, 0.9);
            box-shadow: var(--wz-shadow-1);
        }

        .contact-card:hover {
            transform: translateY(-2px);
            box-shadow: var(--wz-shadow-2);
            border-color: rgba(59, 130, 246, 0.2);
        }

        .contact-company {
            color: var(--accent-primary);
            margin-bottom: 8px;
        }

        .contact-details {
            font-size: 14px;
            color: var(--text-secondary);
            margin-bottom: 4px;
        }

        .relationship-badges {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-top: 10px;
        }

        .badge {
            display: inline-block;
            padding: 3px 10px;
            background: var(--wz-accent-light);
            color: var(--wz-accent);
            border-radius: var(--wz-radius-pill);
            font-size: 11px;
            font-weight: 500;
        }

        .filter-section {
            margin-bottom: 20px;
        }

        .filter-buttons {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
        }

        .filter-btn {
            padding: 6px 16px;
            border: 1px solid var(--wz-light-gray);
            background: var(--wz-white);
            color: var(--wz-mid-gray);
            border-radius: var(--wz-radius-pill);
            cursor: pointer;
            font-size: 12px;
            font-weight: 500;
            font-family: var(--wz-font);
            transition: all 0.15s;
        }

        .filter-btn.active {
            background: var(--wz-accent);
            color: white;
            border-color: var(--wz-accent);
            box-shadow: 0 2px 8px rgba(59, 130, 246, 0.25);
        }

        .filter-btn:hover:not(.active) {
            background: var(--wz-pale-gray);
            color: var(--wz-black);
        }

        .message {
            padding: 15px;
            border-radius: 5px;
            margin-bottom: 20px;
        }

        .message.success {
            background: var(--wz-success-bg);
            color: #1a7a38;
            border: 1px solid var(--wz-success);
        }

        .message.error {
            background: var(--wz-error-bg);
            color: var(--wz-error);
            border: 1px solid var(--wz-error);
        }

        .loading {
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
        }

        /* Modal Styles */
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            background-color: var(--overlay);
        }

        .modal-content {
            background-color: var(--wz-white);
            margin: 5% auto;
            padding: var(--wz-space-xl);
            border-radius: var(--wz-radius-xl);
            width: 90%;
            max-width: 600px;
            box-shadow: 0 24px 64px rgba(15, 23, 42, 0.18), 0 8px 24px rgba(15, 23, 42, 0.10);
            max-height: 85vh;
            overflow-y: auto;
            border: 1px solid rgba(226, 232, 240, 0.7);
        }

        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 24px;
            padding-bottom: 16px;
            border-bottom: 1px solid var(--wz-light-gray);
        }

        .modal-header h2 {
            margin: 0;
            color: var(--wz-black);
            font-size: 16px;
            font-weight: 700;
            letter-spacing: -0.02em;
        }

        .close-btn {
            font-size: 28px;
            font-weight: bold;
            color: var(--text-tertiary);
            cursor: pointer;
            border: none;
            background: none;
            padding: 0;
            width: 30px;
            height: 30px;
            line-height: 30px;
            text-align: center;
        }

        .close-btn:hover {
            color: var(--text-primary);
        }

        .form-row {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 15px;
        }

        .form-group-full {
            grid-column: 1 / -1;
        }

        .modal-buttons {
            display: flex;
            gap: 10px;
            justify-content: flex-end;
            margin-top: 25px;
            padding-top: 20px;
            border-top: 1px solid var(--border-primary);
        }

        /* Opportunity Grid Styles */
        .grid-table {
            width: 100%;
            border-collapse: collapse;
            background: var(--bg-secondary);
            box-shadow: 0 2px 8px var(--shadow);
        }

        .grid-table th,
        .grid-table td {
            border: 1px solid var(--border-primary);
            padding: 12px;
            text-align: left;
            color: var(--text-primary);
        }

        .grid-table th {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
            font-weight: 600;
            font-size: 11px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            position: sticky;
            top: 0;
            z-index: 10;
            border-bottom: 1px solid var(--wz-light-gray);
        }

        .grid-table th.criteria-col {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            font-weight: 500;
            position: sticky;
            left: 0;
            z-index: 11;
            min-width: 250px;
        }

        .grid-table td.criteria-label {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            font-weight: 500;
            position: sticky;
            left: 0;
            z-index: 5;
        }

        .grid-table td.score-cell {
            text-align: center;
            padding: 8px 10px;
            vertical-align: middle;
        }

        .grid-table .company-header {
            text-align: center;
            padding: 15px;
        }

        .grid-table .company-name {
            font-size: 16px;
            font-weight: bold;
            margin-bottom: 5px;
        }

        .grid-table .total-score {
            font-size: 24px;
            font-weight: 600;
            color: black;
            margin: 10px 0;
        }

        .grid-table .criteria-section-header td {
            background: #e8eaf0;
            font-weight: 700;
            font-size: 12px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #444;
            padding: 8px 14px;
            border-top: 2px solid #c5c8d6;
        }

        .grid-table .company-role {
            font-size: 13px;
            opacity: 0.9;
        }

        .checkbox-icon {
            font-size: 20px;
            color: var(--wz-success);
        }

        .grid-score-input {
            width: 56px;
            height: 32px;
            border: 1.5px solid var(--wz-light-gray);
            border-radius: var(--wz-radius-sm);
            background: var(--wz-pale-gray);
            color: var(--wz-black);
            font-size: 13px;
            font-weight: 600;
            font-family: var(--wz-font);
            text-align: center;
            padding: 0 4px;
            transition: border-color 0.2s, background 0.2s, box-shadow 0.2s;
            -moz-appearance: textfield;
        }

        .grid-score-input::-webkit-inner-spin-button,
        .grid-score-input::-webkit-outer-spin-button {
            -webkit-appearance: none;
            margin: 0;
        }

        .grid-score-input:focus {
            outline: none;
            border-color: var(--wz-accent);
            background: var(--wz-white);
            box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.12);
        }

        .grid-score-input:not(:placeholder-shown) {
            background: var(--wz-white);
            border-color: rgba(59, 130, 246, 0.3);
            color: var(--wz-accent-hover);
        }

        .criteria-with-score {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .criteria-score-badge {
            background: var(--bg-tertiary);
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 12px;
            color: var(--text-secondary);
        }

        /* Opportunity Detail Page */
        .opportunity-detail-header {
            background: var(--wz-accent);
            color: white;
            padding: var(--wz-space-lg);
            border-radius: var(--wz-radius-lg);
            margin-bottom: var(--wz-space-lg);
        }

        .opportunity-detail-header h2 {
            margin: 0 0 10px 0;
            font-size: 28px;
        }

        .opportunity-detail-header .role {
            font-size: 18px;
            opacity: 0.9;
        }

        .detail-section {
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 25px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px var(--shadow);
            border: 1px solid var(--border-secondary);
        }

        .detail-section h3 {
            margin: 0 0 20px 0;
            color: var(--text-primary);
            font-size: 20px;
            border-bottom: 2px solid var(--accent-primary);
            padding-bottom: 10px;
        }

        .detail-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
        }

        .detail-field {
            margin-bottom: 15px;
        }

        .detail-field label {
            display: block;
            font-weight: 600;
            color: var(--text-tertiary);
            margin-bottom: 5px;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }

        .detail-field .value {
            color: var(--text-primary);
            font-size: 15px;
            line-height: 1.6;
        }

        .detail-field .value a {
            color: var(--accent-primary);
            text-decoration: none;
        }

        .detail-field .value a:hover {
            text-decoration: underline;
        }

        .detail-field .empty-value {
            color: var(--text-tertiary);
            font-style: italic;
        }

        .detail-field .value ul {
            margin: 8px 0;
            padding-left: 20px;
        }

        .detail-field .value li {
            margin: 4px 0;
            line-height: 1.5;
        }

        .detail-field .value strong {
            font-weight: 600;
            color: var(--text-primary);
        }

        .stage-badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 20px;
            font-size: 13px;
            font-weight: 600;
        }

        .stage-inquiry { background: var(--wz-pale-gray); color: var(--wz-mid-gray); }
        .stage-screening { background: var(--wz-accent-light); color: var(--wz-accent); }
        .stage-interviewing { background: var(--wz-warning-bg); color: var(--wz-warning); }
        .stage-offer { background: var(--wz-success-bg); color: #1a7a38; }
        .stage-accepted { background: #e8f5e9; color: #1a6e2e; }
        .stage-declined { background: var(--wz-error-bg); color: var(--wz-error); }
        .stage-withdrawn { background: #f3e8ff; color: #6d28d9; }

        .claude-chat-btn {
            background: var(--wz-accent);
            color: white;
            border: none;
            padding: 0 var(--wz-space-lg);
            height: 40px;
            border-radius: var(--wz-radius-md);
            font-size: 13px;
            font-weight: 600;
            font-family: var(--wz-font);
            cursor: pointer;
            transition: background 0.15s;
            margin-top: 20px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }

        .claude-chat-btn:hover {
            background: var(--wz-accent-hover);
        }

        /* Form controls for editing */
        .form-control {
            width: 100%;
            padding: 10px;
            border: 1px solid var(--border-primary);
            border-radius: 4px;
            font-size: 14px;
            font-family: inherit;
            transition: border-color 0.3s;
            background: var(--bg-tertiary);
            color: var(--text-primary);
        }

        .form-control:focus {
            outline: none;
            border-color: var(--wz-accent);
            box-shadow: 0 0 0 3px var(--wz-accent-light);
        }

        textarea.form-control {
            resize: vertical;
            min-height: 60px;
        }

        select.form-control {
            cursor: pointer;
        }

        /* Sub-tabs for Contacts page */
        .sub-tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 30px;
            border-bottom: 1px solid var(--border-primary);
        }

        .sub-tab {
            padding: 12px 24px;
            background: none;
            border: none;
            cursor: pointer;
            font-size: 16px;
            color: var(--text-secondary);
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
        }

        .sub-tab:hover {
            color: var(--accent-primary);
        }

        .sub-tab.active {
            color: var(--accent-primary);
            border-bottom-color: var(--accent-primary);
        }

        .sub-tab-content {
            display: none;
        }

        .sub-tab-content.active {
            display: block;
        }

        /* Empty State */
        .empty-state {
            text-align: center;
            padding: 60px 40px;
            color: var(--text-tertiary);
        }

        .empty-state-icon {
            font-size: 64px;
            margin-bottom: 20px;
            opacity: 0.5;
        }

        /* Responsive */
        @media (max-width: 968px) {
            .sidebar {
                width: 200px;
            }

            .main-wrapper {
                margin-left: 200px;
            }

            .dashboard-grid {
                grid-template-columns: 1fr;
            }

            .form-row {
                grid-template-columns: 1fr;
            }

            .modal-content {
                width: 95%;
                margin: 10% auto;
                padding: 20px;
            }
        }

        @media (max-width: 768px) {
            .sidebar {
                width: 60px;
            }

            .main-wrapper {
                margin-left: 60px;
            }

            .sidebar-header h1,
            .sidebar-header .subtitle,
            .nav-item span:not(.nav-icon) {
                display: none;
            }

            .nav-item {
                justify-content: center;
                padding: 15px 10px;
            }
        }

        /* ==================== OKR STYLES ==================== */
        .okr-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding: 20px;
            background: var(--bg-secondary);
            border-radius: 8px;
            box-shadow: 0 2px 4px var(--shadow);
            border: 1px solid var(--border-secondary);
        }

        .okr-tabs {
            display: flex;
            gap: 10px;
        }

        .okr-tab {
            padding: 10px 20px;
            border: none;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
            transition: all 0.3s;
        }

        .okr-tab:hover {
            background: var(--bg-hover);
        }

        .okr-tab.active {
            background: var(--accent-primary);
            color: white;
        }

        .okr-view {
            margin-top: 20px;
        }

        .quarter-selector {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }

        .quarter-btn {
            padding: 8px 16px;
            border: 1px solid var(--accent-primary);
            background: transparent;
            color: var(--accent-primary);
            border-radius: 6px;
            cursor: pointer;
            font-weight: 500;
            transition: all 0.3s;
        }

        .quarter-btn:hover {
            background: var(--wz-accent-light);
        }

        .quarter-btn.active {
            background: var(--accent-primary);
            color: white;
        }

        .okr-table {
            width: 100%;
            background: var(--bg-secondary);
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px var(--shadow);
        }

        .okr-table table {
            width: 100%;
            border-collapse: collapse;
        }

        .okr-table th {
            background: var(--bg-tertiary);
            color: var(--text-primary);
            padding: 12px 10px;
            text-align: center;
            font-weight: 600;
            font-size: 13px;
            border: 1px solid var(--border-primary);
        }

        .okr-table td {
            padding: 10px;
            border: 1px solid var(--border-primary);
            font-size: 13px;
            vertical-align: top;
            color: var(--text-primary);
        }

        .okr-table tr:hover {
            background: var(--bg-tertiary);
        }

        .okr-priority {
            display: inline-block;
            padding: 3px 8px;
            border-radius: 4px;
            font-weight: 600;
            font-size: 11px;
        }

        .okr-priority.p0 {
            background: var(--wz-error-bg);
            color: var(--wz-error);
        }

        .okr-priority.p1 {
            background: var(--wz-warning-bg);
            color: var(--wz-warning);
        }

        .okr-priority.p2 {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
        }

        .okr-status {
            padding: 3px 10px;
            border-radius: var(--wz-radius-pill);
            font-weight: 500;
            font-size: 11px;
            display: inline-block;
        }

        .okr-status.on-track {
            background: var(--wz-success-bg);
            color: #1a7a38;
        }

        .okr-status.behind {
            background: var(--wz-warning-bg);
            color: var(--wz-warning);
        }

        .okr-status.at-risk {
            background: var(--wz-error-bg);
            color: var(--wz-error);
        }

        .okr-status.completed {
            background: var(--wz-accent-light);
            color: var(--wz-accent-active);
        }

        .okr-status.not-priority {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
        }

        /* Status badges for dashboard OKR table */
        .status-badge {
            padding: 3px 10px;
            border-radius: var(--wz-radius-pill);
            font-weight: 500;
            font-size: 11px;
            display: inline-block;
        }

        .status-badge.status-on-track {
            background: var(--wz-success-bg);
            color: #1a7a38;
        }

        .status-badge.status-behind {
            background: var(--wz-warning-bg);
            color: var(--wz-warning);
        }

        .status-badge.status-at-risk {
            background: var(--wz-error-bg);
            color: var(--wz-error);
        }

        .status-badge.status-completed {
            background: var(--wz-accent-light);
            color: var(--wz-accent-active);
        }

        .status-badge.status-not-priority {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
        }

        /* Priority badges for dashboard OKR table */
        .priority-badge {
            display: inline-block;
            padding: 3px 8px;
            border-radius: var(--wz-radius-xs);
            font-weight: 600;
            font-size: 11px;
        }

        .priority-badge.priority-p0 {
            background: var(--wz-error-bg);
            color: var(--wz-error);
        }

        .priority-badge.priority-p1 {
            background: var(--wz-warning-bg);
            color: var(--wz-warning);
        }

        .priority-badge.priority-p2 {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
        }

        .progress-cell {
            min-width: 150px;
        }

        .progress-note {
            font-size: 12px;
            color: var(--text-secondary);
            margin-top: 5px;
        }

        .okr-actions {
            display: flex;
            gap: 5px;
        }

        .okr-actions button {
            padding: 4px 8px;
            font-size: 11px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .okr-actions .btn-edit {
            background: var(--wz-accent-light);
            color: var(--wz-accent);
        }

        .okr-actions .btn-edit:hover {
            background: var(--wz-accent);
            color: white;
        }

        .okr-actions .btn-progress {
            background: var(--wz-success-bg);
            color: #1a7a38;
        }

        .okr-actions .btn-progress:hover {
            background: var(--wz-success);
            color: white;
        }

        .okr-actions .btn-delete {
            background: var(--wz-error-bg);
            color: var(--wz-error);
        }

        .okr-actions .btn-delete:hover {
            background: var(--wz-error);
            color: white;
        }

        .category-tag {
            display: inline-block;
            padding: 3px 8px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            border-radius: 4px;
            font-size: 11px;
            font-weight: 500;
        }

        /* Opportunity Stage Summary */
        .stage-summary {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }

        .stage-stat {
            background: var(--bg-secondary);
            border-radius: 8px;
            padding: 15px 20px;
            cursor: pointer;
            transition: all 0.3s;
            border: 1px solid var(--border-secondary);
            flex: 1;
            min-width: 150px;
        }

        .stage-stat:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 12px var(--shadow);
        }

        .stage-stat.active {
            border-color: var(--wz-accent);
            background: var(--wz-accent-light);
        }

        .stage-stat-label {
            font-size: 12px;
            color: var(--text-secondary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 5px;
        }

        .stage-stat-number {
            font-size: 24px;
            font-weight: bold;
            color: var(--text-primary);
            margin-bottom: 5px;
        }

        .stage-stat-percent {
            font-size: 11px;
            color: var(--text-tertiary);
        }

        .toggle-closed-lost {
            background: var(--wz-error-bg);
            color: var(--wz-error);
            border: 1px solid var(--wz-error);
            padding: 0 var(--wz-space-md);
            height: 34px;
            border-radius: var(--wz-radius-md);
            cursor: pointer;
            font-size: 13px;
            font-family: var(--wz-font);
            font-weight: 600;
            transition: background 0.15s;
        }

        .toggle-closed-lost:hover {
            background: var(--wz-error);
            color: white;
        }

        .toggle-closed-lost.showing {
            background: var(--wz-pale-gray);
            color: var(--wz-mid-gray);
            border-color: var(--wz-light-gray);
        }

        /* Add To Do Button (top bar) */
        .btn-add-todo {
            background: var(--accent-primary);
            color: #fff;
            border: none;
            border-radius: 6px;
            padding: 8px 18px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.15s;
        }

        .btn-add-todo:hover {
            background: var(--accent-hover);
        }

        /* ==================== ASANA-STYLE TO DO LIST ==================== */
        .todos-list-container {
            background: var(--bg-secondary);
            border-radius: 8px;
            border: 1px solid var(--border-secondary);
            overflow: hidden;
        }

        .todos-column-header {
            display: grid;
            grid-template-columns: 44px 1fr 130px 110px 90px 100px;
            padding: 10px 16px;
            border-bottom: 1px solid var(--border-primary);
            font-size: 11px;
            font-weight: 600;
            color: var(--text-tertiary);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            background: var(--bg-secondary);
            position: sticky;
            top: 0;
            z-index: 5;
        }

        .todo-section-header {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px 16px;
            cursor: pointer;
            font-weight: 600;
            font-size: 14px;
            color: var(--text-primary);
            border-top: 1px solid var(--border-primary);
            user-select: none;
            transition: background 0.15s;
        }

        .todo-section-header:hover {
            background: var(--bg-tertiary);
        }

        .section-collapse-icon {
            font-size: 10px;
            transition: transform 0.2s;
            color: var(--text-secondary);
        }

        .todo-section.collapsed .section-collapse-icon {
            transform: rotate(-90deg);
        }

        .todo-section.collapsed .todo-section-body {
            display: none;
        }

        .section-count {
            font-size: 12px;
            color: var(--text-tertiary);
            font-weight: 400;
            margin-left: 4px;
        }

        /* Task Rows */
        .todo-row {
            display: grid;
            grid-template-columns: 44px 1fr 130px 110px 90px 100px;
            padding: 0 16px;
            border-bottom: 1px solid var(--border-secondary);
            cursor: pointer;
            align-items: center;
            font-size: 13px;
            color: var(--text-primary);
            transition: background 0.12s;
            min-height: 42px;
        }

        .todo-row:hover {
            background: var(--bg-tertiary);
        }

        .todo-row.selected {
            background: var(--wz-accent-light);
        }

        .todo-row .col-name {
            padding: 8px 0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .completed-text {
            text-decoration: line-through;
            color: var(--text-tertiary);
        }

        .overdue-text {
            color: var(--priority-high-text);
        }

        /* Circular Checkbox */
        .row-checkbox {
            width: 18px;
            height: 18px;
            border: 2px solid var(--text-tertiary);
            border-radius: 50%;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            transition: all 0.15s;
            flex-shrink: 0;
        }

        .row-checkbox:hover {
            border-color: var(--wz-success);
            background: var(--wz-success-bg);
        }

        .row-checkbox.checked {
            background: var(--wz-success);
            border-color: var(--wz-success);
        }

        .row-checkbox.checked::after {
            content: '✓';
            font-size: 11px;
            color: var(--wz-white);
            font-weight: bold;
        }

        /* Priority & Status Tags */
        .priority-tag {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 500;
        }

        .priority-tag.high {
            background: var(--priority-high-bg);
            color: var(--priority-high-text);
        }

        .priority-tag.medium {
            background: var(--priority-medium-bg);
            color: var(--priority-medium-text);
        }

        .priority-tag.low {
            background: var(--priority-low-bg);
            color: var(--priority-low-text);
        }

        .status-tag {
            display: inline-block;
            padding: 2px 10px;
            border-radius: 10px;
            font-size: 11px;
            font-weight: 500;
        }

        .status-tag.on-track {
            background: var(--status-on-track-bg);
            color: var(--status-on-track-text);
        }

        .status-tag.at-risk {
            background: var(--status-at-risk-bg);
            color: var(--status-at-risk-text);
        }

        .status-tag.off-track {
            background: var(--status-off-track-bg);
            color: var(--status-off-track-text);
        }

        .status-tag.done-tag {
            background: var(--status-on-track-bg);
            color: var(--status-on-track-text);
        }

        /* Add Task Row */
        .todo-add-row {
            display: grid;
            grid-template-columns: 44px 1fr;
            padding: 8px 16px;
            cursor: pointer;
            color: var(--text-tertiary);
            font-size: 13px;
            transition: background 0.15s;
            min-height: 38px;
            align-items: center;
        }

        .todo-add-row:hover {
            background: var(--bg-tertiary);
            color: var(--text-secondary);
        }

        /* Inline Add Form */
        .inline-add-form {
            display: grid;
            grid-template-columns: 44px 1fr 130px 110px 90px 100px;
            padding: 0 16px;
            border-bottom: 1px solid var(--border-secondary);
            background: var(--bg-tertiary);
            align-items: center;
            min-height: 42px;
        }

        .inline-add-input {
            background: transparent !important;
            border: none !important;
            color: var(--text-primary) !important;
            font-size: 13px !important;
            padding: 8px 4px !important;
            outline: none !important;
            width: 100% !important;
        }

        .inline-add-input::placeholder {
            color: var(--text-tertiary);
        }

        /* Task Detail Panel */
        .task-detail-panel {
            position: fixed;
            top: 0;
            right: -520px;
            width: 520px;
            height: 100vh;
            background: var(--bg-secondary);
            border-left: 1px solid var(--border-primary);
            z-index: 1002;
            transition: right 0.25s ease;
            overflow-y: auto;
            box-shadow: -4px 0 20px var(--shadow);
        }

        .task-detail-panel.open {
            right: 0;
        }

        .task-detail-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.3);
            z-index: 1001;
        }

        .task-detail-overlay.open {
            display: block;
        }

        .task-detail-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 16px 24px;
            border-bottom: 1px solid var(--border-primary);
        }

        .task-detail-close {
            background: none;
            border: none;
            color: var(--text-secondary);
            font-size: 24px;
            cursor: pointer;
            padding: 4px 8px;
            border-radius: 4px;
            line-height: 1;
        }

        .task-detail-close:hover {
            background: var(--bg-tertiary);
            color: var(--text-primary);
        }

        .btn-mark-complete {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 14px;
            border: 1px solid var(--wz-success);
            background: transparent;
            color: #1a7a38;
            border-radius: var(--wz-radius-md);
            cursor: pointer;
            font-size: 13px;
            font-family: var(--wz-font);
            font-weight: 500;
            transition: all 0.15s;
        }

        .btn-mark-complete:hover {
            background: var(--wz-success-bg);
        }

        .checkmark-circle-btn {
            width: 16px;
            height: 16px;
            border: 2px solid var(--wz-success);
            border-radius: 50%;
            display: inline-block;
        }

        .task-detail-title {
            width: 100%;
            font-size: 22px;
            font-weight: 600;
            background: transparent;
            border: none;
            color: var(--text-primary);
            padding: 24px 24px 16px;
            outline: none;
            font-family: inherit;
        }

        .task-detail-title::placeholder {
            color: var(--text-tertiary);
        }

        .task-detail-fields {
            padding: 0 24px;
        }

        .detail-field-row {
            display: grid;
            grid-template-columns: 100px 1fr;
            padding: 10px 0;
            border-bottom: 1px solid var(--border-secondary);
            align-items: center;
        }

        .detail-field-label {
            font-size: 13px;
            color: var(--text-secondary);
        }

        .detail-field-input {
            font-size: 13px;
            color: var(--text-primary);
            background: transparent;
            border: 1px solid transparent;
            padding: 6px 8px;
            border-radius: 4px;
            font-family: inherit;
            width: 100%;
        }

        .detail-field-input:hover {
            border-color: var(--border-primary);
        }

        .detail-field-input:focus {
            outline: none;
            border-color: var(--accent-primary);
            background: var(--bg-tertiary);
        }

        .detail-field-value-text {
            font-size: 13px;
            color: var(--text-secondary);
            padding: 6px 8px;
        }

        .task-detail-description {
            padding: 20px 24px;
        }

        .task-detail-description label {
            font-size: 13px;
            color: var(--text-secondary);
            margin-bottom: 8px;
        }

        .task-detail-description textarea {
            width: 100%;
            min-height: 100px;
            background: var(--bg-tertiary);
            border: 1px solid var(--border-primary);
            color: var(--text-primary);
            padding: 12px;
            border-radius: 6px;
            font-family: inherit;
            font-size: 13px;
            resize: vertical;
        }

        .task-detail-actions {
            padding: 16px 24px;
            display: flex;
            gap: 12px;
            border-top: 1px solid var(--border-primary);
        }

        .btn-danger-outline {
            padding: 0 var(--wz-space-md);
            height: 34px;
            background: transparent;
            color: var(--wz-error);
            border: 1px solid var(--wz-error);
            border-radius: var(--wz-radius-md);
            cursor: pointer;
            font-size: 13px;
            font-weight: 500;
            transition: all 0.2s;
        }

        .btn-danger-outline:hover {
            background: var(--priority-high-bg);
        }
    </style>
</head>
<body>
    <script>
        // Define navigateTo early so onclick handlers can use it
        window.navigateTo = function(page) {
            // This will be properly implemented once the full script loads
            console.log('Early navigateTo called for:', page);
            // Store the requested page to navigate after DOM loads
            window._pendingNavigation = page;
        };
    </script>
    <!-- Left Sidebar Navigation -->
    <div class="sidebar">
        <div class="sidebar-header">
            <a href="#" onclick="navigateTo('dashboard'); return false;">
                <img src="/static/logo.png" alt="Wetzel CRM" id="logo">
            </a>
        </div>
        <nav class="nav-menu">
            <div class="nav-item active" onclick="navigateTo('dashboard')">
                <span class="nav-icon">📊</span>
                <span>Dashboard</span>
            </div>
            <div class="nav-item" onclick="navigateTo('agents')">
                <span class="nav-icon">🤖</span>
                <span>Agents</span>
            </div>
            <div class="nav-item" onclick="navigateTo('opportunities')">
                <span class="nav-icon">🎯</span>
                <span>Opportunities</span>
            </div>
            <div class="nav-item" onclick="navigateTo('todos')">
                <span class="nav-icon">✅</span>
                <span>To Do's</span>
            </div>
            <div class="nav-item" onclick="navigateTo('contacts')">
                <span class="nav-icon">👥</span>
                <span>Contacts</span>
            </div>
            <div class="nav-item" onclick="navigateTo('okrs')">
                <span class="nav-icon">🎯</span>
                <span>OKRs</span>
            </div>
            <div class="nav-item" onclick="window.location.href='/logout'" style="margin-top: auto; border-top: 1px solid var(--wz-light-gray); opacity: 0.8;">
                <span class="nav-icon">🚪</span>
                <span>Logout</span>
            </div>
        </nav>
    </div>

    <!-- Main Content Area -->
    <div class="main-wrapper">
        <div class="top-bar" style="justify-content: space-between;">
            <h2 id="page-title">Dashboard</h2>
            <button id="add-todo-btn" class="btn-add-todo" onclick="openNewTaskDetail()" style="display: none;">+ Add To Do</button>
        </div>

        <div class="content-area">
            <!-- ==================== DASHBOARD PAGE ==================== -->
            <div id="dashboard-page" class="page-view active">
                <!-- Quick Stats Row -->
                <div class="quick-stats">
                    <div class="stat-card" onclick="navigateTo('opportunities')">
                        <div class="stat-number" id="stat-open-opps">0</div>
                        <div class="stat-label">Open Opportunities</div>
                    </div>
                    <div class="stat-card" onclick="navigateTo('todos')">
                        <div class="stat-number" id="stat-active-todos">0</div>
                        <div class="stat-label">Active To-Do's</div>
                    </div>
                    <div class="stat-card" onclick="navigateToQ3OKRs()">
                        <div class="stat-number" id="stat-q1-okrs">0</div>
                        <div class="stat-label">Q3 OKRs (P0+P1)</div>
                    </div>
                    <a href="https://www.linkedin.com/in/joshwetzel/" target="_blank" class="stat-card">
                        <div class="stat-number" id="stat-linkedin-followers">11,068</div>
                        <div class="stat-label">LinkedIn Followers</div>
                    </a>
                </div>

                <!-- Main Dashboard Grid: Top 10 To-Do's (60%) + Top 5 Opportunities (40%) -->
                <div class="dashboard-grid">
                    <!-- Top 10 To-Do's Section -->
                    <div class="dashboard-section">
                        <div class="section-header">
                            <h3 class="section-title">📋 Top 10 To-Do's</h3>
                            <a href="#" class="section-action" onclick="navigateTo('todos'); return false;">View All</a>
                        </div>
                        <div id="top-todos-list" class="loading">Loading...</div>
                    </div>

                    <!-- Top 5 Opportunities Section -->
                    <div class="dashboard-section">
                        <div class="section-header">
                            <h3 class="section-title">🎯 Top 5 Opportunities</h3>
                            <a href="#" class="section-action" onclick="navigateTo('opportunities'); return false;">View All</a>
                        </div>
                        <div id="top-opps-list" class="loading">Loading...</div>
                    </div>
                </div>

                <!-- Q2 2026 OKRs Section -->
                <div class="dashboard-section" style="margin-bottom: 30px;">
                    <div class="section-header">
                        <h3 class="section-title">🎯 Q3 2026 OKRs (P0 + P1)</h3>
                        <a href="#" class="section-action" onclick="navigateToQ3OKRs(); return false;">View All</a>
                    </div>
                    <div id="q1-okrs-list" class="loading">Loading...</div>
                </div>

                <!-- Next Actions & Recent Activity -->
                <div class="dashboard-grid">
                    <div class="dashboard-section">
                        <div class="section-header">
                            <h3 class="section-title">⚡ Next Actions</h3>
                        </div>
                        <div id="next-actions-list" class="loading">Loading...</div>
                    </div>

                    <div class="dashboard-section">
                        <div class="section-header">
                            <h3 class="section-title">📅 Recent Activity</h3>
                        </div>
                        <div id="recent-activity-list" class="loading">Loading...</div>
                    </div>
                </div>

                <!-- Recent Contacts Section -->
                <div class="dashboard-section" style="margin-top: 20px;">
                    <div class="section-header">
                        <h3 class="section-title">👥 Recent Contacts</h3>
                        <a href="#" class="section-action" onclick="navigateTo('contacts'); return false;">View All</a>
                    </div>
                    <div id="recent-contacts-list" class="loading">Loading...</div>
                </div>
            </div>

            <!-- ==================== OPPORTUNITIES PAGE ==================== -->
            <div id="opportunities-page" class="page-view">
                <div style="margin-bottom: 20px; display: flex; gap: 10px; justify-content: space-between; align-items: center;">
                    <div style="display: flex; gap: 10px;">
                        <button onclick="showAddCompanyModal()" class="btn btn-primary">+ Add Company</button>
                        <button onclick="showAddCriteriaModal()" class="btn" style="background: #28a745; color: white;">+ Add Criteria</button>
                    </div>
                    <button onclick="toggleClosedLost()" id="toggle-closed-lost-btn" class="toggle-closed-lost">
                        Show Closed Lost
                    </button>
                </div>

                <!-- Stage Summary Bar -->
                <div class="stage-summary" id="stage-summary">
                    <!-- Will be populated by JavaScript -->
                </div>

                <div id="grid-container" style="overflow-x: auto;">
                    <!-- Grid will be loaded here -->
                </div>
            </div>

            <!-- ==================== OPPORTUNITY DETAIL PAGE ==================== -->
            <div id="opportunity-detail-page" class="page-view">
                <div style="margin-bottom: 20px;">
                    <button onclick="navigateTo('opportunities')" class="btn btn-secondary">← Back to Opportunities</button>
                </div>

                <div id="opportunity-detail-content" class="loading">Loading opportunity details...</div>
            </div>

            <!-- ==================== TO DO'S PAGE ==================== -->
            <div id="todos-page" class="page-view">
                <!-- Task Detail Panel (slide-in from right) -->
                <div id="task-detail-panel" class="task-detail-panel">
                    <div class="task-detail-header">
                        <button class="btn-mark-complete" id="detail-complete-btn" onclick="completeFromDetail()">
                            <span class="checkmark-circle-btn"></span> Mark complete
                        </button>
                        <button class="task-detail-close" onclick="closeTaskDetail()">&times;</button>
                    </div>
                    <div class="task-detail-body">
                        <input type="text" class="task-detail-title" id="detail-title" placeholder="Task name">
                        <div class="task-detail-fields">
                            <div class="detail-field-row">
                                <span class="detail-field-label">Assignee</span>
                                <input type="text" class="detail-field-input" id="detail-assignee" placeholder="Add assignee">
                            </div>
                            <div class="detail-field-row">
                                <span class="detail-field-label">Due date</span>
                                <input type="datetime-local" class="detail-field-input" id="detail-due-date">
                            </div>
                            <div class="detail-field-row">
                                <span class="detail-field-label">Priority</span>
                                <select class="detail-field-input" id="detail-priority">
                                    <option value="low">Low</option>
                                    <option value="medium">Medium</option>
                                    <option value="high">High</option>
                                </select>
                            </div>
                            <div class="detail-field-row">
                                <span class="detail-field-label">Status</span>
                                <select class="detail-field-input" id="detail-status">
                                    <option value="on_track">On track</option>
                                    <option value="at_risk">At risk</option>
                                    <option value="off_track">Off track</option>
                                </select>
                            </div>
                            <div class="detail-field-row">
                                <span class="detail-field-label">Section</span>
                                <select class="detail-field-input" id="detail-section">
                                    <option value="todo">To do</option>
                                    <option value="doing">Doing</option>
                                    <option value="done">Done</option>
                                </select>
                            </div>
                            <div class="detail-field-row">
                                <span class="detail-field-label">Contact</span>
                                <span class="detail-field-value-text" id="detail-contact">--</span>
                            </div>
                            <div class="detail-field-row">
                                <span class="detail-field-label">Company</span>
                                <span class="detail-field-value-text" id="detail-company">--</span>
                            </div>
                        </div>
                        <div class="task-detail-description">
                            <label>Description</label>
                            <textarea id="detail-description" placeholder="Add a description..."></textarea>
                        </div>
                        <div class="task-detail-actions">
                            <button class="btn btn-primary" onclick="saveTaskDetail()" style="padding: 10px 24px; font-size: 14px;">Save</button>
                            <button class="btn-danger-outline" onclick="deleteFromDetail()">Delete task</button>
                        </div>
                    </div>
                </div>

                <!-- Overlay for detail panel -->
                <div id="task-detail-overlay" class="task-detail-overlay" onclick="closeTaskDetail()"></div>

                <div id="followup-message"></div>

                <!-- Main list area -->
                <div class="todos-list-container">
                    <!-- Column headers -->
                    <div class="todos-column-header">
                        <div class="col-checkbox"></div>
                        <div class="col-name">Task name</div>
                        <div class="col-assignee">Assignee</div>
                        <div class="col-due-date">Due date</div>
                        <div class="col-priority">Priority</div>
                        <div class="col-status">Status</div>
                    </div>

                    <!-- Sections rendered by JavaScript -->
                    <div id="todos-sections"></div>
                </div>
            </div>

            <!-- ==================== CONTACTS PAGE ==================== -->
            <div id="contacts-page" class="page-view">
                <div class="sub-tabs">
                    <button class="sub-tab active" onclick="switchContactTab('view')">View Contacts</button>
                    <button class="sub-tab" onclick="switchContactTab('add')">Add Contact</button>
                    <button class="sub-tab" onclick="switchContactTab('relationships')">Manage Relationships</button>
                </div>

                <!-- View Contacts Sub-Tab -->
                <div id="view-subtab" class="sub-tab-content active">
                    <input type="text" id="search-input" class="search-box" placeholder="Search by name, company, or email...">

                    <div class="filter-section">
                        <label>Filter by relationship:</label>
                        <div class="filter-buttons">
                            <button class="filter-btn active" onclick="filterByRelationship('all')">All</button>
                            <button class="filter-btn" onclick="filterByRelationship('recruiter')">Recruiters</button>
                            <button class="filter-btn" onclick="filterByRelationship('executive')">Executives</button>
                            <button class="filter-btn" onclick="filterByRelationship('co-worker')">Co-workers</button>
                            <button class="filter-btn" onclick="filterByRelationship('friend')">Friends</button>
                            <button class="filter-btn" onclick="filterByRelationship('prospective')">Prospective</button>
                        </div>
                    </div>

                    <div id="contacts-list" class="contacts-grid">
                        <div class="loading">Loading contacts...</div>
                    </div>
                </div>

                <!-- Add Contact Sub-Tab -->
                <div id="add-subtab" class="sub-tab-content">
                    <div id="add-message"></div>
                    <form id="add-contact-form">
                        <div class="form-group">
                            <label>Name *</label>
                            <input type="text" id="name" required>
                        </div>

                        <div class="form-group">
                            <label>Company *</label>
                            <input type="text" id="company" list="companies-list" required placeholder="Type to select existing or add new company">
                            <small style="color: #666; font-size: 12px;">💡 Select from list or type a new company name</small>
                        </div>

                        <div class="form-group">
                            <label>Role</label>
                            <input type="text" id="role">
                        </div>

                        <div class="form-group">
                            <label>Email</label>
                            <input type="email" id="email">
                        </div>

                        <div class="form-group">
                            <label>Phone</label>
                            <input type="tel" id="phone">
                        </div>

                        <div class="form-group">
                            <label>Opportunity</label>
                            <input type="text" id="opportunity">
                        </div>

                        <div class="form-group">
                            <label>Notes</label>
                            <textarea id="notes"></textarea>
                        </div>

                        <div class="form-group">
                            <label>Relationship Types</label>
                            <div class="checkbox-group">
                                <label class="checkbox-label">
                                    <input type="checkbox" name="relationship" value="recruiter"> Recruiter
                                </label>
                                <label class="checkbox-label">
                                    <input type="checkbox" name="relationship" value="executive"> Executive
                                </label>
                                <label class="checkbox-label">
                                    <input type="checkbox" name="relationship" value="co-worker"> Co-worker
                                </label>
                                <label class="checkbox-label">
                                    <input type="checkbox" name="relationship" value="friend"> Friend
                                </label>
                                <label class="checkbox-label">
                                    <input type="checkbox" name="relationship" value="prospective"> Prospective
                                </label>
                            </div>
                        </div>

                        <button type="submit" class="btn btn-primary">Add Contact</button>
                        <button type="reset" class="btn btn-secondary">Clear Form</button>
                    </form>
                </div>

                <!-- Manage Relationships Sub-Tab -->
                <div id="relationships-subtab" class="sub-tab-content">
                    <div id="rel-message"></div>
                    <form id="add-relationship-form">
                        <div class="form-group">
                            <label>Contact Name *</label>
                            <input type="text" id="rel-contact-name" list="contacts-names-list" required>
                        </div>

                        <div class="form-group">
                            <label>Add Relationship Type *</label>
                            <select id="rel-type" required>
                                <option value="">Select a relationship type...</option>
                                <option value="recruiter">Recruiter</option>
                                <option value="executive">Executive</option>
                                <option value="co-worker">Co-worker</option>
                                <option value="friend">Friend</option>
                                <option value="prospective">Prospective</option>
                            </select>
                        </div>

                        <button type="submit" class="btn btn-primary">Add Relationship</button>
                    </form>
                </div>
            </div>

            <!-- ==================== COMPANIES PAGE ==================== -->
            <div id="companies-page" class="page-view">
                <div id="companies-list-container">
                    <div class="loading">Loading companies...</div>
                </div>
            </div>

            <!-- ==================== OKRs PAGE ==================== -->
            <div id="okrs-page" class="page-view">
                <div class="okr-header">
                    <div class="okr-tabs">
                        <button class="okr-tab active" onclick="switchOKRView('annual')">Annual (5 Year Plan)</button>
                        <button class="okr-tab" onclick="switchOKRView('quarterly')">Quarterly</button>
                    </div>
                    <button class="btn btn-primary" onclick="openAddOKRModal()">+ Add OKR</button>
                </div>

                <!-- Annual View -->
                <div id="annual-okr-view" class="okr-view">
                    <div class="loading">Loading annual OKRs...</div>
                </div>

                <!-- Quarterly View -->
                <div id="quarterly-okr-view" class="okr-view" style="display: none;">
                    <div class="quarter-selector">
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2025, 1)">Q1 2025</button>
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2025, 2)">Q2 2025</button>
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2025, 3)">Q3 2025</button>
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2025, 4)">Q4 2025</button>
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2026, 1)">Q1 2026</button>
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2026, 2)">Q2 2026</button>
                        <button class="quarter-btn active" onclick="loadQuarterlyOKRs(2026, 3)">Q3 2026</button>
                        <button class="quarter-btn" onclick="loadQuarterlyOKRs(2026, 4)">Q4 2026</button>
                    </div>
                    <div id="quarterly-okr-list" class="loading">Loading quarterly OKRs...</div>
                </div>
            </div>

            <!-- ==================== AGENTS PAGE ==================== -->
            <div id="agents-page" class="page-view">
              <style>
                #agents-page { padding: 0; }
                #agents-page .gtm-app { max-width: 860px; margin: 0 auto; padding: 1.5rem 1.5rem 4rem; }
                #agents-page .gtm-header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 1rem; }
                #agents-page .gtm-header h2 { font-size: 18px; font-weight: 500; letter-spacing: -0.01em; margin: 0; }
                #agents-page .gtm-header-right { display: flex; align-items: center; gap: 14px; }
                #agents-page .gtm-clock { font-size: 13px; color: var(--wz-dark-gray, #6b6b66); }
                #agents-page .gtm-refresh-note { font-size: 11px; color: #9e9e99; background: #f5f5f3; border: 0.5px solid rgba(0,0,0,0.08); border-radius: 20px; padding: 3px 10px; white-space: nowrap; }
                #agents-page .gtm-banner { background: #e6f1fb; border: 0.5px solid rgba(24,95,165,0.2); border-radius: 8px; padding: 10px 14px; font-size: 12px; color: #185fa5; margin-bottom: 1.25rem; display: flex; align-items: center; gap: 10px; }
                #agents-page .gtm-banner strong { font-weight: 500; }
                #agents-page .gtm-banner .gtm-dismiss { margin-left: auto; cursor: pointer; opacity: 0.6; background: none; border: none; font-size: 16px; color: #185fa5; line-height: 1; padding: 0; flex-shrink: 0; }
                #agents-page .gtm-banner .gtm-dismiss:hover { opacity: 1; }
                #agents-page .gtm-nav-tabs { display: flex; gap: 0; margin-bottom: 1.5rem; border-bottom: 0.5px solid rgba(0,0,0,0.08); }
                #agents-page .gtm-nav-tab { font-size: 13px; padding: 9px 18px; cursor: pointer; border: none; background: transparent; color: #6b6b66; border-bottom: 2px solid transparent; margin-bottom: -1px; font-family: inherit; transition: color 0.15s; }
                #agents-page .gtm-nav-tab:hover { color: #1a1a18; }
                #agents-page .gtm-nav-tab.active { color: #1a1a18; border-bottom-color: #1a1a18; font-weight: 500; }
                #agents-page .gtm-count-chip { display: inline-block; font-size: 11px; background: #f5f5f3; padding: 1px 6px; border-radius: 10px; margin-left: 4px; }
                #agents-page .gtm-view { display: none; }
                #agents-page .gtm-view.active { display: block; }
                #agents-page .gtm-day-section { margin-bottom: 1.75rem; }
                #agents-page .gtm-day-label { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; color: #9e9e99; margin-bottom: 10px; }
                #agents-page .gtm-mtg-card { background: #fff; border: 0.5px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 1rem 1.125rem; margin-bottom: 8px; transition: box-shadow 0.15s; }
                #agents-page .gtm-mtg-card:hover { box-shadow: 0 2px 12px rgba(0,0,0,0.06); }
                #agents-page .gtm-mtg-row1 { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
                #agents-page .gtm-mtg-time { font-size: 12px; color: #6b6b66; white-space: nowrap; padding-top: 1px; min-width: 58px; }
                #agents-page .gtm-mtg-info { flex: 1; min-width: 0; }
                #agents-page .gtm-mtg-title { font-size: 14px; font-weight: 500; margin-bottom: 2px; }
                #agents-page .gtm-mtg-sub { font-size: 12px; color: #6b6b66; }
                #agents-page .gtm-mtg-att { font-size: 11px; color: #9e9e99; margin-top: 3px; }
                #agents-page .gtm-mtg-status { font-size: 11px; padding: 2px 8px; border-radius: 20px; background: #fef9ec; color: #7a5c00; border: 0.5px solid rgba(122,92,0,0.2); white-space: nowrap; flex-shrink: 0; }
                #agents-page .gtm-mtg-actions { display: flex; gap: 6px; flex-wrap: wrap; }
                #agents-page .gtm-action-btn { font-size: 12px; padding: 5px 12px; border-radius: 6px; cursor: pointer; border: 0.5px solid rgba(0,0,0,0.15); background: transparent; color: #6b6b66; font-family: inherit; transition: all 0.15s; display: flex; align-items: center; gap: 5px; }
                #agents-page .gtm-action-btn:hover { background: #f5f5f3; color: #1a1a18; }
                #agents-page .gtm-action-btn.copied { background: #eaf3de; color: #3b6d11; border-color: rgba(59,109,17,0.25); }
                #agents-page .gtm-action-btn.log-btn { margin-left: auto; }
                #agents-page .gtm-stat-row { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; margin-bottom: 1.5rem; }
                #agents-page .gtm-stat-card { background: #f5f5f3; border-radius: 8px; padding: 1rem; }
                #agents-page .gtm-stat-label { font-size: 12px; color: #6b6b66; margin-bottom: 5px; }
                #agents-page .gtm-stat-val { font-size: 26px; font-weight: 500; }
                #agents-page .gtm-hist-empty { font-size: 13px; color: #6b6b66; padding: 3rem 0; text-align: center; line-height: 1.8; }
                #agents-page .gtm-hist-card { background: #fff; border: 0.5px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 1rem 1.125rem; margin-bottom: 8px; cursor: pointer; }
                #agents-page .gtm-hist-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,0.05); }
                #agents-page .gtm-hist-meta { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
                #agents-page .gtm-hist-title { font-size: 13px; font-weight: 500; }
                #agents-page .gtm-hist-chip { font-size: 11px; padding: 2px 8px; border-radius: 20px; }
                #agents-page .chip-prep { background: #e6f1fb; color: #185fa5; }
                #agents-page .chip-followup { background: #eaf3de; color: #3b6d11; }
                #agents-page .gtm-hist-time { font-size: 11px; color: #9e9e99; margin-left: auto; }
                #agents-page .gtm-hist-preview { font-size: 12px; color: #6b6b66; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
                #agents-page .gtm-result-panel { margin-top: 12px; background: #f5f5f3; border: 0.5px solid rgba(0,0,0,0.08); border-radius: 12px; padding: 1.25rem; }
                #agents-page .gtm-rp-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
                #agents-page .gtm-rp-title { font-size: 14px; font-weight: 500; }
                #agents-page .gtm-rp-close { font-size: 20px; cursor: pointer; background: none; border: none; color: #6b6b66; line-height: 1; }
                #agents-page .gtm-rp-content { font-size: 13px; line-height: 1.75; white-space: pre-wrap; color: #1a1a18; }
                /* GTM Modal */
                #gtm-modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.35); display: flex; align-items: center; justify-content: center; z-index: 200; opacity: 0; pointer-events: none; transition: opacity 0.2s; }
                #gtm-modal-overlay.open { opacity: 1; pointer-events: all; }
                #gtm-modal { background: #fff; border-radius: 12px; padding: 1.5rem; width: 560px; max-width: calc(100vw - 2rem); box-shadow: 0 20px 60px rgba(0,0,0,0.2); transform: translateY(10px); transition: transform 0.2s; }
                #gtm-modal-overlay.open #gtm-modal { transform: translateY(0); }
                #gtm-modal .gtm-modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
                #gtm-modal .gtm-modal-title { font-size: 15px; font-weight: 500; }
                #gtm-modal .gtm-modal-close { font-size: 22px; cursor: pointer; background: none; border: none; color: #6b6b66; line-height: 1; }
                #gtm-modal .gtm-modal-lbl { font-size: 12px; color: #6b6b66; margin-bottom: 6px; }
                #gtm-modal .gtm-modal-type-row { display: flex; gap: 6px; margin-bottom: 14px; }
                #gtm-modal .gtm-type-btn { font-size: 12px; padding: 5px 14px; border-radius: 20px; cursor: pointer; border: 0.5px solid rgba(0,0,0,0.15); background: transparent; color: #6b6b66; font-family: inherit; transition: all 0.15s; }
                #gtm-modal .gtm-type-btn.active { background: #f5f5f3; color: #1a1a18; border-color: rgba(0,0,0,0.25); }
                #gtm-modal .gtm-modal-textarea { width: 100%; height: 200px; padding: 10px 12px; font-size: 13px; font-family: inherit; border: 0.5px solid rgba(0,0,0,0.15); border-radius: 8px; background: #f5f5f3; color: #1a1a18; resize: vertical; outline: none; line-height: 1.65; margin-bottom: 14px; }
                #gtm-modal .gtm-modal-textarea:focus { border-color: rgba(0,0,0,0.25); }
                #gtm-modal .gtm-modal-footer { display: flex; gap: 8px; justify-content: flex-end; }
                #gtm-modal .gtm-btn-cancel { font-size: 13px; padding: 7px 16px; border-radius: 8px; cursor: pointer; border: 0.5px solid rgba(0,0,0,0.15); background: transparent; color: #6b6b66; font-family: inherit; }
                #gtm-modal .gtm-btn-save { font-size: 13px; padding: 7px 20px; border-radius: 8px; cursor: pointer; border: none; background: #1a1a18; color: #fff; font-family: inherit; font-weight: 500; }
                #gtm-modal .gtm-btn-save:hover { opacity: 0.85; }
                #agents-page .gtm-toast { position: fixed; bottom: 28px; left: 50%; transform: translateX(-50%) translateY(20px); background: #1a1a18; color: #fff; font-size: 13px; padding: 9px 20px; border-radius: 30px; opacity: 0; transition: all 0.25s; pointer-events: none; z-index: 999; white-space: nowrap; }
                #agents-page .gtm-toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
              </style>

              <div class="gtm-app">
                <div class="gtm-header">
                  <h2>GTM agent hub</h2>
                  <div class="gtm-header-right">
                    <span class="gtm-refresh-note">Refreshed Mar 26 · Ask Claude to refresh</span>
                    <span class="gtm-clock" id="gtm-clock"></span>
                  </div>
                </div>

                <div class="gtm-banner" id="gtm-how-banner">
                  <span>⚡</span>
                  <span><strong>How this works:</strong> Click <em>Copy prep prompt</em> or <em>Copy follow-up prompt</em>, paste into Cowork, and Claude runs the agent using your Gmail, Calendar &amp; Granola. Paste the result back with <em>Log result</em> to build your archive here.</span>
                  <button class="gtm-dismiss" onclick="gtmDismissBanner()">×</button>
                </div>

                <div class="gtm-nav-tabs">
                  <button class="gtm-nav-tab active" onclick="gtmSwitchView('meetings',this)">Meetings</button>
                  <button class="gtm-nav-tab" onclick="gtmSwitchView('history',this)">History <span class="gtm-count-chip" id="gtm-hist-count">0</span></button>
                </div>

                <!-- Meetings View -->
                <div id="gtm-view-meetings" class="gtm-view active">

                  <div class="gtm-day-section">
                    <div class="gtm-day-label">Today — Thursday, March 26</div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">9:00 AM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">Carley Case — Enterpret VP Revenue</div>
                          <div class="gtm-mtg-sub">Swing Search recruiter screen · Zoom</div>
                          <div class="gtm-mtg-att">Carley Case · carley@swingsearch.com</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','Carley Case — Enterpret VP Revenue','Carley Case (carley@swingsearch.com)','Today 9:00 AM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','Carley Case — Enterpret VP Revenue','Carley Case (carley@swingsearch.com)','Today 9:00 AM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('Carley Case — Enterpret VP Revenue')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">9:30 AM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">Jaimie Buss — 30 Minute Chat</div>
                          <div class="gtm-mtg-sub">Pavilion connection · Deputy</div>
                          <div class="gtm-mtg-att">Jaimie Buss · jbuss@deputy.com</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','Jaimie Buss — 30 Minute Chat','Jaimie Buss (jbuss@deputy.com)','Today 9:30 AM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','Jaimie Buss — 30 Minute Chat','Jaimie Buss (jbuss@deputy.com)','Today 9:30 AM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('Jaimie Buss — 30 Minute Chat')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">12:00 PM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">CMO School Class</div>
                          <div class="gtm-mtg-sub">Pavilion CMO School · Zoom</div>
                          <div class="gtm-mtg-att">Pavilion cohort</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','CMO School Class','Pavilion CMO School cohort','Today 12:00 PM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','CMO School Class','Pavilion CMO School cohort','Today 12:00 PM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('CMO School Class')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">1:00 PM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">Pavilion Gold Investor Syndicate — Kick Off</div>
                          <div class="gtm-mtg-sub">FOG Ventures · Zoom</div>
                          <div class="gtm-mtg-att">Sam (Pavilion) · Casey &amp; John (FOG Ventures)</div>
                        </div>
                        <span class="gtm-mtg-status">tentative</span>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','Pavilion Gold Investor Syndicate Kick Off','Sam (sam@joinpavilion.com), Casey and John (FOG Ventures)','Today 1:00 PM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','Pavilion Gold Investor Syndicate Kick Off','Sam (sam@joinpavilion.com), Casey and John (FOG Ventures)','Today 1:00 PM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('Pavilion Gold Investor Syndicate Kick Off')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">2:30 PM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">Eric Guajardo — Qualio Search</div>
                          <div class="gtm-mtg-sub">Sterling Strand · 30 minute meeting · Zoom</div>
                          <div class="gtm-mtg-att">Eric Guajardo · eric@sterlingstrand.com</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','Eric Guajardo — Qualio Search','Eric Guajardo (eric@sterlingstrand.com)','Today 2:30 PM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','Eric Guajardo — Qualio Search','Eric Guajardo (eric@sterlingstrand.com)','Today 2:30 PM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('Eric Guajardo — Qualio Search')">+ Log result</button>
                      </div>
                    </div>
                  </div>

                  <div class="gtm-day-section">
                    <div class="gtm-day-label">Tomorrow — Friday, March 27</div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">7:00 AM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">AI in GTM Monthly RoundTable — March</div>
                          <div class="gtm-mtg-sub">Topic: Claude Code · Jonathan Moss &amp; Andy Jolls · Pavilion</div>
                          <div class="gtm-mtg-att">Pavilion AI in GTM group · Chatham House rules</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','AI in GTM Monthly RoundTable — March','Pavilion AI in GTM group, Jonathan Moss, Andy Jolls','Tomorrow 7:00 AM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','AI in GTM Monthly RoundTable — March','Pavilion AI in GTM group, Jonathan Moss, Andy Jolls','Tomorrow 7:00 AM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('AI in GTM Monthly RoundTable — March')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">7:50 AM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">Alpine Screening Call — Juliette Callam</div>
                          <div class="gtm-mtg-sub">Alpine Solves · Phone call</div>
                          <div class="gtm-mtg-att">Juliette Callam · juliette.callam@alpinesolves.com</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','Alpine Screening Call','Juliette Callam (juliette.callam@alpinesolves.com)','Tomorrow 7:50 AM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','Alpine Screening Call','Juliette Callam (juliette.callam@alpinesolves.com)','Tomorrow 7:50 AM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('Alpine Screening Call — Juliette Callam')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">8:00 AM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">Pavilion Gold: Follow Up with Tim Rutten</div>
                          <div class="gtm-mtg-sub">Building an AI-Native Marketing &amp; Sales Org · Backbase</div>
                          <div class="gtm-mtg-att">Tim Rutten · tim@backbase.com</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','Pavilion Gold Follow Up with Tim Rutten','Tim Rutten (tim@backbase.com)','Tomorrow 8:00 AM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','Pavilion Gold Follow Up with Tim Rutten','Tim Rutten (tim@backbase.com)','Tomorrow 8:00 AM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('Pavilion Gold: Follow Up with Tim Rutten')">+ Log result</button>
                      </div>
                    </div>

                    <div class="gtm-mtg-card">
                      <div class="gtm-mtg-row1">
                        <div class="gtm-mtg-time">10:00 AM</div>
                        <div class="gtm-mtg-info">
                          <div class="gtm-mtg-title">CMO School Cohort 1</div>
                          <div class="gtm-mtg-sub">Pavilion · Fridays at 10am PT · Zoom</div>
                          <div class="gtm-mtg-att">Pavilion CMO School cohort</div>
                        </div>
                      </div>
                      <div class="gtm-mtg-actions">
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'prep','CMO School Cohort 1','Pavilion CMO School cohort','Tomorrow 10:00 AM')">📋 Copy prep prompt</button>
                        <button class="gtm-action-btn" onclick="gtmCopyPrompt(this,'followup','CMO School Cohort 1','Pavilion CMO School cohort','Tomorrow 10:00 AM')">📋 Copy follow-up prompt</button>
                        <button class="gtm-action-btn log-btn" onclick="gtmOpenLog('CMO School Cohort 1')">+ Log result</button>
                      </div>
                    </div>
                  </div>
                </div>

                <!-- History View -->
                <div id="gtm-view-history" class="gtm-view">
                  <div class="gtm-stat-row">
                    <div class="gtm-stat-card"><div class="gtm-stat-label">Total logged</div><div class="gtm-stat-val" id="gtm-stat-total">0</div></div>
                    <div class="gtm-stat-card"><div class="gtm-stat-label">Prep runs</div><div class="gtm-stat-val" id="gtm-stat-prep">0</div></div>
                    <div class="gtm-stat-card"><div class="gtm-stat-label">Follow-up runs</div><div class="gtm-stat-val" id="gtm-stat-fu">0</div></div>
                  </div>
                  <div id="gtm-hist-list">
                    <div class="gtm-hist-empty">No results logged yet.<br>Run an agent in Cowork and click "Log result" to archive outputs here.</div>
                  </div>
                  <div id="gtm-hist-detail"></div>
                </div>

                <div class="gtm-toast" id="gtm-toast"></div>
              </div>
            </div>
        </div>
    </div>
</div>

<!-- GTM Log Result Modal -->
<div id="gtm-modal-overlay" onclick="gtmHandleOverlayClick(event)">
  <div id="gtm-modal">
    <div class="gtm-modal-head">
      <span class="gtm-modal-title" id="gtm-modal-title">Log result</span>
      <button class="gtm-modal-close" onclick="gtmCloseModal()">×</button>
    </div>
    <div class="gtm-modal-lbl">Type</div>
    <div class="gtm-modal-type-row">
      <button class="gtm-type-btn active" id="gtm-type-prep" onclick="gtmSetType('prep')">Meeting prep</button>
      <button class="gtm-type-btn" id="gtm-type-followup" onclick="gtmSetType('followup')">Post-meeting follow-up</button>
    </div>
    <div class="gtm-modal-lbl">Paste Claude's output below</div>
    <textarea class="gtm-modal-textarea" id="gtm-modal-text" placeholder="Paste the result from Cowork here..."></textarea>
    <div class="gtm-modal-footer">
      <button class="gtm-btn-cancel" onclick="gtmCloseModal()">Cancel</button>
      <button class="gtm-btn-save" onclick="gtmSaveResult()">Save to history</button>
    </div>
  </div>
</div>

    <!-- ==================== MODALS ==================== -->

    <!-- Edit Contact Modal -->
    <div id="editModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>✏️ Edit Contact</h2>
                <button class="close-btn" onclick="closeEditModal()">&times;</button>
            </div>
            <form onsubmit="event.preventDefault(); saveContactEdit();">
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>Name</label>
                        <input type="text" id="edit-name" readonly style="background: #f0f0f0; cursor: not-allowed;">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>Company</label>
                        <input type="text" id="edit-company" list="edit-companies-list" placeholder="Type to select existing or add new company" style="width: 100%;">
                        <datalist id="edit-companies-list"></datalist>
                        <small style="color: #666; font-size: 12px;">💡 Select from list or type a new company name</small>
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Email</label>
                        <input type="email" id="edit-email" placeholder="email@example.com">
                    </div>
                    <div class="form-group">
                        <label>Phone</label>
                        <input type="tel" id="edit-phone" placeholder="(555) 123-4567">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>Title/Role</label>
                        <input type="text" id="edit-role" placeholder="e.g., Senior Developer">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>LinkedIn Profile URL</label>
                        <input type="url" id="edit-linkedin" placeholder="https://linkedin.com/in/username">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>Opportunity</label>
                        <input type="text" id="edit-opportunity" placeholder="e.g., Senior Frontend Engineer">
                    </div>
                </div>
                <div class="modal-buttons">
                    <button type="button" class="btn" style="background: #28a745; color: white; margin-right: auto;" onclick="createTodoFromEditModal()">✅ Add To Do</button>
                    <button type="button" class="btn btn-secondary" onclick="closeEditModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">💾 Save Changes</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Add Company Modal for Grid -->
    <div id="addCompanyGridModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Add Company to Grid</h2>
                <button class="close-btn" onclick="closeGridModal('addCompanyGridModal')">&times;</button>
            </div>
            <form onsubmit="saveGridCompany(event)">
                <div class="form-group">
                    <label>Company Name *</label>
                    <input type="text" id="grid-company-name" required>
                </div>
                <div class="form-group">
                    <label>Opportunity Stage 🎯</label>
                    <select id="grid-company-opportunity-stage" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px;">
                        <option value="Inquiry">Inquiry</option>
                        <option value="Screening">Screening</option>
                        <option value="Interviewing">Interviewing</option>
                        <option value="Offer">Offer</option>
                        <option value="Accepted">Accepted</option>
                        <option value="Declined">Closed Lost</option>
                        <option value="Withdrawn">Withdrawn</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>LinkedIn Profile</label>
                    <input type="url" id="grid-company-linkedin" placeholder="https://linkedin.com/company/...">
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Location</label>
                        <input type="text" id="grid-company-location" placeholder="Sydney/SF">
                    </div>
                    <div class="form-group">
                        <label>Role</label>
                        <input type="text" id="grid-company-role" placeholder="CRO">
                    </div>
                </div>
                <div class="form-row">
                    <div class="form-group">
                        <label>Stage</label>
                        <input type="text" id="grid-company-stage" placeholder="Series B">
                    </div>
                    <div class="form-group">
                        <label>Employees</label>
                        <input type="number" id="grid-company-employees">
                    </div>
                </div>
                <div class="modal-buttons">
                    <button type="button" class="btn btn-secondary" onclick="closeGridModal('addCompanyGridModal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">Add Company</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Add Criteria Modal for Grid -->
    <div id="addCriteriaGridModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Add Scoring Criteria</h2>
                <button class="close-btn" onclick="closeGridModal('addCriteriaGridModal')">&times;</button>
            </div>
            <form onsubmit="saveGridCriteria(event)">
                <div class="form-group">
                    <label>Criteria Name *</label>
                    <input type="text" id="grid-criteria-name" required placeholder="e.g., Cash Comp (Base + Bonus)">
                </div>
                <div class="form-group">
                    <label>Max Score *</label>
                    <input type="number" id="grid-criteria-score" value="10" min="1" max="100" required>
                </div>
                <div class="form-group">
                    <label>Category</label>
                    <select id="grid-criteria-category">
                        <option>Personal Fit</option>
                        <option>Compensation</option>
                        <option>Company</option>
                        <option>Work Life</option>
                        <option>Career Growth</option>
                    </select>
                </div>
                <div class="modal-buttons">
                    <button type="button" class="btn btn-secondary" onclick="closeGridModal('addCriteriaGridModal')">Cancel</button>
                    <button type="submit" class="btn" style="background: #28a745; color: white;">Add Criteria</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Edit Criteria Modal for Grid -->
    <div id="editCriteriaGridModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <h2>Edit Scoring Criteria</h2>
                <button class="close-btn" onclick="closeGridModal('editCriteriaGridModal')">&times;</button>
            </div>
            <form onsubmit="updateGridCriteria(event)">
                <input type="hidden" id="edit-grid-criteria-id">
                <div class="form-group">
                    <label>Criteria Name *</label>
                    <input type="text" id="edit-grid-criteria-name" required placeholder="e.g., Cash Comp (Base + Bonus)">
                </div>
                <div class="form-group">
                    <label>Max Score *</label>
                    <input type="number" id="edit-grid-criteria-score" value="10" min="1" max="100" required>
                </div>
                <div class="modal-buttons">
                    <button type="button" class="btn" style="background: #dc3545; color: white; margin-right: auto;" onclick="confirmDeleteCriteria()">Delete</button>
                    <button type="button" class="btn btn-secondary" onclick="closeGridModal('editCriteriaGridModal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">Save Changes</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Company Detail/Edit Modal -->
    <div id="companyDetailModal" class="modal">
        <div class="modal-content" style="max-width: 700px;">
            <div class="modal-header">
                <h2>📊 Company Details</h2>
                <button class="close-btn" onclick="closeGridModal('companyDetailModal')">&times;</button>
            </div>
            <form onsubmit="updateGridCompanyDetails(event)">
                <input type="hidden" id="company-detail-id">

                <div class="form-group">
                    <label>Company Name *</label>
                    <input type="text" id="company-detail-name" required>
                </div>

                <div class="form-group">
                    <label>Opportunity Stage 🎯</label>
                    <select id="company-detail-opportunity-stage" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 5px;">
                        <option value="Inquiry">Inquiry</option>
                        <option value="Screening">Screening</option>
                        <option value="Interviewing">Interviewing</option>
                        <option value="Offer">Offer</option>
                        <option value="Accepted">Accepted</option>
                        <option value="Declined">Closed Lost</option>
                        <option value="Withdrawn">Withdrawn</option>
                    </select>
                </div>

                <div class="form-group">
                    <label>LinkedIn Profile</label>
                    <input type="url" id="company-detail-linkedin" placeholder="https://linkedin.com/company/...">
                    <div id="linkedin-link-display" style="margin-top: 5px;"></div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Location</label>
                        <input type="text" id="company-detail-location" placeholder="Sydney/SF">
                    </div>
                    <div class="form-group">
                        <label>Role</label>
                        <input type="text" id="company-detail-role" placeholder="CRO">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Stage</label>
                        <input type="text" id="company-detail-stage" placeholder="Series B, Series C">
                    </div>
                    <div class="form-group">
                        <label>Employees</label>
                        <input type="number" id="company-detail-employees">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Raised</label>
                        <input type="text" id="company-detail-raised" placeholder="$69M">
                    </div>
                    <div class="form-group">
                        <label>Revenue</label>
                        <input type="text" id="company-detail-revenue" placeholder="$23M">
                    </div>
                </div>

                <div class="form-group">
                    <label>Next Step</label>
                    <input type="text" id="company-detail-next-step" placeholder="e.g., Final Interview">
                </div>

                <div class="form-group">
                    <label>Benefits</label>
                    <input type="text" id="company-detail-benefits" placeholder="Good, Solid, Excellent">
                </div>

                <h3 style="margin: 20px 0 10px 0; color: #667eea;">💰 Compensation</h3>

                <div class="form-row">
                    <div class="form-group">
                        <label>Cash Comp</label>
                        <input type="number" id="company-detail-cash" placeholder="600000">
                    </div>
                    <div class="form-group">
                        <label>Base Salary</label>
                        <input type="number" id="company-detail-base" placeholder="300000">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Incentive/Bonus</label>
                        <input type="number" id="company-detail-incentive" placeholder="300000">
                    </div>
                    <div class="form-group">
                        <label>Equity</label>
                        <input type="number" id="company-detail-equity" placeholder="3800000">
                    </div>
                </div>

                <div class="form-row">
                    <div class="form-group">
                        <label>Targeted Annual Comp</label>
                        <input type="number" id="company-detail-targeted" placeholder="1550000">
                    </div>
                    <div class="form-group">
                        <label>Total 4-Year Comp</label>
                        <input type="number" id="company-detail-total4year" placeholder="6200000">
                    </div>
                </div>

                <div class="modal-buttons">
                    <button type="button" class="btn" style="background: #dc3545; color: white; margin-right: auto;" onclick="confirmDeleteGridCompany()">Delete Company</button>
                    <button type="button" class="btn btn-secondary" onclick="closeGridModal('companyDetailModal')">Cancel</button>
                    <button type="submit" class="btn btn-primary">💾 Save Changes</button>
                </div>
            </form>
        </div>
    </div>

    <!-- ==================== OKR MODALS ==================== -->

    <!-- Add/Edit OKR Modal -->
    <div id="okrModal" class="modal">
        <div class="modal-content" style="max-width: 700px;">
            <div class="modal-header">
                <h2 id="okr-modal-title">➕ Add New OKR</h2>
                <button class="close-btn" onclick="closeOKRModal()">&times;</button>
            </div>
            <form onsubmit="event.preventDefault(); saveOKR();">
                <input type="hidden" id="okr-id">
                <input type="hidden" id="okr-edit-mode">

                <!-- Period Type Selection -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>📅 Period Type *</label>
                        <select id="okr-period-type" onchange="togglePeriodFields()" required>
                            <option value="">Select period type...</option>
                            <option value="annual">Annual (5 Year Plan)</option>
                            <option value="quarterly">Quarterly</option>
                        </select>
                    </div>
                </div>

                <!-- Year and Quarter Selection -->
                <div class="form-row" id="period-fields" style="display: none;">
                    <div class="form-group">
                        <label id="year-label">Year *</label>
                        <select id="okr-year" required>
                            <option value="">Select year...</option>
                            <option value="2024">2024</option>
                            <option value="2025">2025</option>
                            <option value="2026">2026</option>
                            <option value="2027">2027</option>
                            <option value="2028">2028</option>
                            <option value="2029">2029</option>
                        </select>
                    </div>
                    <div class="form-group" id="quarter-field" style="display: none;">
                        <label>Quarter *</label>
                        <select id="okr-quarter">
                            <option value="">Select quarter...</option>
                            <option value="1">Q1</option>
                            <option value="2">Q2</option>
                            <option value="3">Q3</option>
                            <option value="4">Q4</option>
                        </select>
                    </div>
                </div>

                <!-- Category and Priority -->
                <div class="form-row">
                    <div class="form-group">
                        <label>🏷️ Category *</label>
                        <select id="okr-category" required>
                            <option value="">Select category...</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>⚡ Priority *</label>
                        <select id="okr-priority" required>
                            <option value="">Select priority...</option>
                            <option value="P0">P0 - Critical</option>
                            <option value="P1">P1 - High</option>
                            <option value="P2">P2 - Medium</option>
                        </select>
                    </div>
                </div>

                <!-- Objective -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>🎯 Objective *</label>
                        <textarea id="okr-objective" rows="3" placeholder="Describe your objective..." required></textarea>
                    </div>
                </div>

                <!-- Key Result -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>📊 Key Result(s) *</label>
                        <textarea id="okr-key-result" rows="3" placeholder="KR: Measurable, objective ways to define and track progress..." required></textarea>
                        <small style="color: #666; font-size: 12px;">💡 Start with "KR:" and describe measurable outcomes</small>
                    </div>
                </div>

                <!-- Done Date and Status -->
                <div class="form-row">
                    <div class="form-group">
                        <label>📅 Done Date</label>
                        <input type="date" id="okr-done-date">
                    </div>
                    <div class="form-group">
                        <label>🎨 Status *</label>
                        <select id="okr-status" required>
                            <option value="On Track">🟢 On Track</option>
                            <option value="Behind Schedule">🟡 Behind Schedule</option>
                            <option value="At Risk">🔴 At Risk</option>
                            <option value="Completed">🔵 Completed</option>
                            <option value="Not a Priority">🟠 Not a Priority</option>
                        </select>
                    </div>
                </div>

                <!-- Goal Type IDs (Optional) -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>🎯 Ties to Goals (Optional)</label>
                        <div style="display: flex; flex-wrap: wrap; gap: 10px; margin-top: 8px;">
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="1"> 1. Personal Development
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="2"> 2. Family
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="3"> 3. Health/Happiness
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="4"> 4. Career Development
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="5"> 5. Wealth
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="6"> 6. Friendship
                            </label>
                            <label style="display: flex; align-items: center; gap: 5px; cursor: pointer;">
                                <input type="checkbox" class="goal-checkbox" value="7"> 7. Other
                            </label>
                        </div>
                    </div>
                </div>

                <!-- Key People (Optional) -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>👥 Key People Involved (Optional)</label>
                        <input type="text" id="okr-key-people" placeholder="Names of people involved...">
                    </div>
                </div>

                <div class="modal-buttons">
                    <button type="button" class="btn btn-secondary" onclick="closeOKRModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">💾 Save OKR</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Monthly Progress Modal -->
    <div id="progressModal" class="modal">
        <div class="modal-content" style="max-width: 600px;">
            <div class="modal-header">
                <h2>📊 Add Monthly Progress</h2>
                <button class="close-btn" onclick="closeProgressModal()">&times;</button>
            </div>
            <form onsubmit="event.preventDefault(); saveMonthlyProgress();">
                <input type="hidden" id="progress-okr-id">

                <!-- OKR Info Display -->
                <div style="background: #f5f5f5; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                    <div style="font-weight: 600; margin-bottom: 5px;">📌 OKR:</div>
                    <div id="progress-okr-objective" style="color: #555;"></div>
                </div>

                <!-- Progress Date -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>📅 Progress Date *</label>
                        <input type="date" id="progress-date" required>
                        <small style="color: #666; font-size: 12px;">💡 Typically the 1st of the month</small>
                    </div>
                </div>

                <!-- Status -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>🎨 Status *</label>
                        <select id="progress-status" required>
                            <option value="">Select status...</option>
                            <option value="On Track">🟢 On Track</option>
                            <option value="Behind Schedule">🟡 Behind Schedule</option>
                            <option value="At Risk">🔴 At Risk</option>
                            <option value="Completed">🔵 Completed</option>
                            <option value="Not a Priority">🟠 Not a Priority</option>
                        </select>
                    </div>
                </div>

                <!-- Progress Percentage -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>📈 Progress Percentage (Optional)</label>
                        <input type="number" id="progress-percentage" min="0" max="100" placeholder="0-100">
                        <small style="color: #666; font-size: 12px;">💡 How complete is this OKR? (0-100%)</small>
                    </div>
                </div>

                <!-- Progress Note -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>📝 Progress Details *</label>
                        <textarea id="progress-note" rows="4" placeholder="Describe progress, challenges, wins, next steps..." required></textarea>
                        <small style="color: #666; font-size: 12px;">💡 What happened this month? What's working? What needs attention?</small>
                    </div>
                </div>

                <div class="modal-buttons">
                    <button type="button" class="btn btn-secondary" onclick="closeProgressModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">💾 Save Progress</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Annual Progress Modal -->
    <div id="annualProgressModal" class="modal">
        <div class="modal-content" style="max-width: 600px;">
            <div class="modal-header">
                <h2>📊 Update Annual Progress</h2>
                <button class="close-btn" onclick="closeAnnualProgressModal()">&times;</button>
            </div>
            <form onsubmit="event.preventDefault(); saveAnnualProgress();">
                <input type="hidden" id="annual-progress-okr-id">
                <input type="hidden" id="annual-progress-year">

                <!-- OKR Info Display -->
                <div style="background: #f5f5f5; padding: 15px; border-radius: 8px; margin-bottom: 20px;">
                    <div style="font-weight: 600; margin-bottom: 5px;">📌 OKR:</div>
                    <div id="annual-progress-okr-objective" style="color: #555;"></div>
                    <div style="margin-top: 8px; font-weight: 600; color: #666;">
                        📅 Year: <span id="annual-progress-year-display"></span>
                    </div>
                </div>

                <!-- Status -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>🎨 Status *</label>
                        <select id="annual-progress-status" required>
                            <option value="">Select status...</option>
                            <option value="On Track">🟢 On Track</option>
                            <option value="Behind Schedule">🟡 Behind Schedule</option>
                            <option value="At Risk">🔴 At Risk</option>
                            <option value="Completed">🔵 Completed</option>
                            <option value="Not a Priority">🟠 Not a Priority</option>
                        </select>
                    </div>
                </div>

                <!-- Progress Note -->
                <div class="form-row">
                    <div class="form-group form-group-full">
                        <label>📝 Progress Note (Optional)</label>
                        <textarea id="annual-progress-note" rows="4" placeholder="Describe progress, challenges, wins, next steps..."></textarea>
                        <small style="color: #666; font-size: 12px;">💡 What's the current status? Any updates or changes?</small>
                    </div>
                </div>

                <div class="modal-buttons">
                    <button type="button" class="btn btn-secondary" onclick="closeAnnualProgressModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">💾 Save Progress</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        console.log('JavaScript started loading...');

        // Global variables
        let allContacts = [];
        let currentFilter = 'all';
        let allFollowups = [];
        let allTodos = [];
        let selectedTaskId = null;
        let isNewTask = false;
        let collapsedSections = {};
        let currentFollowupFilter = 'active';
        let opportunityGridData = { companies: [], criteria: [], scores: {} };
        let currentEditingContact = null;
        let currentEditingCriteriaId = null;
        let currentEditingCompanyId = null;

        console.log('Global variables initialized');

        // ==================== NAVIGATION ====================
        window.navigateTo = function(page) {
            console.log('Full navigateTo called for:', page);
            // Update nav items
            document.querySelectorAll('.nav-item').forEach(item => {
                item.classList.remove('active');
            });
            // Find and activate the nav item for this page
            document.querySelectorAll('.nav-item').forEach(item => {
                const onclick = item.getAttribute('onclick');
                if (onclick && onclick.includes(`'${page}'`)) {
                    item.classList.add('active');
                }
            });

            // Update page views
            document.querySelectorAll('.page-view').forEach(view => {
                view.classList.remove('active');
            });
            document.getElementById(page + '-page').classList.add('active');

            // Update page title
            const titles = {
                'dashboard': 'Dashboard',
                'agents': 'Agents',
                'opportunities': 'Opportunities',
                'todos': "To Do's",
                'contacts': 'Contacts',
                'companies': 'Companies',
                'okrs': 'OKRs'
            };
            document.getElementById('page-title').textContent = titles[page];

            // Show/hide the Add To Do button
            const addTodoBtn = document.getElementById('add-todo-btn');
            if (addTodoBtn) {
                addTodoBtn.style.display = (page === 'todos') ? 'inline-block' : 'none';
            }

            // Load data for the page
            if (page === 'dashboard') {
                loadDashboard();
            } else if (page === 'agents') {
                gtmInitPage();
            } else if (page === 'opportunities') {
                loadOpportunityGrid();
            } else if (page === 'todos') {
                loadFollowups();
            } else if (page === 'contacts') {
                loadContacts();
            } else if (page === 'companies') {
                loadCompaniesPage();
            } else if (page === 'okrs') {
                // Default to Q3 2026 quarterly view
                document.querySelectorAll('.okr-tab').forEach(tab => tab.classList.remove('active'));
                const quarterlyTab = document.querySelector('.okr-tab[onclick*="quarterly"]');
                if (quarterlyTab) quarterlyTab.classList.add('active');
                document.getElementById('annual-okr-view').style.display = 'none';
                document.getElementById('quarterly-okr-view').style.display = 'block';
                loadQuarterlyOKRs(2026, 3);
            }
        }

        function navigateToQ2OKRs() { navigateToQ3OKRs(); }
        function navigateToQ3OKRs() {
            navigateTo('okrs');
            setTimeout(() => {
                document.querySelectorAll('.okr-tab').forEach(tab => tab.classList.remove('active'));
                const quarterlyTab = document.querySelector('.okr-tab[onclick*="quarterly"]');
                if (quarterlyTab) quarterlyTab.classList.add('active');
                document.getElementById('annual-okr-view').style.display = 'none';
                document.getElementById('quarterly-okr-view').style.display = 'block';
                loadQuarterlyOKRs(2026, 3);
            }, 100);
        }

        // ==================== DASHBOARD FUNCTIONS ====================
        async function loadDashboard() {
            console.log('loadDashboard() called!');
            try {
                console.log('Fetching /api/stats...');

                // Load opportunities count and active followups
                const oppsRes = await fetch('/api/opportunity-grid');
                const opps = await oppsRes.json();
                const inactiveStages = ['Declined', 'Closed Lost', 'Withdrawn'];
                const activeOpps = (opps.companies || []).filter(c => !inactiveStages.includes(c.opportunity_stage));
                document.getElementById('stat-open-opps').textContent = activeOpps.length;

                const followupsRes = await fetch('/api/followups/active');
                const followups = await followupsRes.json();
                document.getElementById('stat-active-todos').textContent = followups.length || 0;

                // Load Q3 OKRs P0+P1 count
                const okrsRes = await fetch('/api/okrs/quarterly?year=2026&quarter=3');
                const okrs = await okrsRes.json();
                const activeP0P1Count = okrs.filter(okr =>
                    (okr.priority === 'P0' || okr.priority === 'P1') &&
                    okr.status !== 'Completed' &&
                    okr.status !== 'Not a Priority'
                ).length;
                document.getElementById('stat-q1-okrs').textContent = activeP0P1Count;

                // Load dashboard sections
                console.log('Loading top todos...');
                await loadTopTodos();
                console.log('Loading top opportunities...');
                await loadTopOpportunities();
                console.log('Loading Q3 OKRs...');
                await loadQ1OKRs();
                console.log('Loading next actions...');
                await loadNextActions();
                console.log('Loading recent activity...');
                await loadRecentActivity();
                console.log('Loading recent contacts...');
                await loadRecentContacts();
                console.log('Dashboard loaded successfully!');

            } catch (error) {
                console.error('Error loading dashboard:', error);
                alert('Error loading dashboard: ' + error.message);
            }
        }

        async function loadTopTodos() {
            try {
                const response = await fetch('/api/followups/active');
                const todos = await response.json();

                const container = document.getElementById('top-todos-list');

                if (todos.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">✅</div><p>No active to-dos</p></div>';
                    return;
                }

                const top10 = todos.slice(0, 10);
                container.innerHTML = top10.map(todo => `
                    <div class="todo-item">
                        <input type="checkbox" class="todo-checkbox" onclick="completeDashboardTodo(${todo.task_id})">
                        <div class="todo-content">
                            <div class="todo-title">${todo.task_title || 'Untitled'}</div>
                            <div class="todo-meta">
                                ${todo.person_name || 'No contact'} • Due: ${formatDueDate(todo.due_date)}
                            </div>
                        </div>
                        <span class="todo-priority priority-${todo.priority || 'low'}">${(todo.priority || 'low').toUpperCase()}</span>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Error loading todos:', error);
                document.getElementById('top-todos-list').innerHTML = '<div class="empty-state">Error loading to-dos</div>';
            }
        }

        async function loadTopOpportunities() {
            try {
                const response = await fetch('/api/opportunity-grid');
                const data = await response.json();

                const container = document.getElementById('top-opps-list');

                if (!data.companies || data.companies.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🎯</div><p>No opportunities yet</p></div>';
                    return;
                }

                // Filter out inactive stages
                const inactiveStages = ['Declined', 'Closed Lost', 'Withdrawn'];
                const activeCompanies = data.companies.filter(c => !inactiveStages.includes(c.opportunity_stage));

                // Calculate scores for each active company
                const companiesWithScores = activeCompanies.map(company => {
                    const score = calculateCompanyScore(String(company.grid_company_id), data);
                    return { ...company, totalScore: score };
                });

                // Sort by most recently added (highest grid_company_id) and take top 5
                const sorted = companiesWithScores.sort((a, b) => b.grid_company_id - a.grid_company_id);
                const top10 = sorted.slice(0, 5);

                container.innerHTML = top10.map(opp => `
                    <div class="opp-item" onclick="navigateTo('opportunities')">
                        <div class="opp-company">${opp.company_name}</div>
                        <div>
                            <span class="opp-score">${opp.totalScore || 0} pts</span>
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Error loading opportunities:', error);
                document.getElementById('top-opps-list').innerHTML = '<div class="empty-state">Error loading opportunities</div>';
            }
        }

        async function loadQ1OKRs() {
            try {
                const response = await fetch('/api/okrs/quarterly?year=2026&quarter=3');
                const okrs = await response.json();

                const container = document.getElementById('q1-okrs-list');

                // Filter for P0 and P1 OKRs that are active
                const priorityOkrs = okrs.filter(okr =>
                    (okr.priority === 'P0' || okr.priority === 'P1') &&
                    okr.status !== 'Completed' &&
                    okr.status !== 'Not a Priority'
                );

                if (priorityOkrs.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🎯</div><p>No active P0/P1 OKRs</p></div>';
                    return;
                }

                container.innerHTML = `
                    <table class="okr-table" style="width: 100%; font-size: 13px;">
                        <thead>
                            <tr>
                                <th style="width: 8%;">Category</th>
                                <th style="width: 4%;">P</th>
                                <th style="width: 25%;">Objective</th>
                                <th style="width: 20%;">Key Result</th>
                                <th style="width: 33%;">Status</th>
                                <th style="width: 10%;">Actions</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${priorityOkrs.map(okr => {
                                // Get the most recent monthly progress status
                                let displayStatus = okr.status || '';
                                let statusNote = '';

                                if (okr.progress && okr.progress.length > 0) {
                                    const latestProgress = okr.progress[okr.progress.length - 1];
                                    displayStatus = latestProgress.status || okr.status || '';
                                    if (latestProgress.note) {
                                        statusNote = ' - ' + latestProgress.note.substring(0, 50) + (latestProgress.note.length > 50 ? '...' : '');
                                    }
                                }

                                const statusClass = displayStatus === 'On Track' ? 'status-on-track' :
                                                   displayStatus === 'Behind Schedule' ? 'status-behind' :
                                                   displayStatus === 'At Risk' ? 'status-at-risk' :
                                                   displayStatus === 'Completed' ? 'status-completed' :
                                                   displayStatus === 'Not a Priority' ? 'status-not-priority' : '';
                                return `
                                    <tr>
                                        <td>${okr.category || ''}</td>
                                        <td style="text-align: center;"><span class="priority-badge priority-${okr.priority.toLowerCase()}">${okr.priority}</span></td>
                                        <td>${okr.objective || ''}</td>
                                        <td>${okr.key_result || ''}</td>
                                        <td><span class="status-badge ${statusClass}">${displayStatus}</span>${statusNote}</td>
                                        <td style="text-align: center;">
                                            <button class="btn-sm" onclick="navigateTo('okrs')">View</button>
                                        </td>
                                    </tr>
                                `;
                            }).join('')}
                        </tbody>
                    </table>
                `;
            } catch (error) {
                console.error('Error loading Q1 OKRs:', error);
                document.getElementById('q1-okrs-list').innerHTML = '<div class="empty-state">Error loading OKRs</div>';
            }
        }

        async function loadRecentContacts() {
            try {
                const response = await fetch('/api/contacts');
                const contacts = await response.json();

                const container = document.getElementById('recent-contacts-list');

                if (contacts.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">👥</div><p>No contacts yet</p></div>';
                    return;
                }

                // Show last 3 contacts (reduced from 5)
                const recent = contacts.slice(0, 3);

                container.innerHTML = recent.map(contact => `
                    <div class="contact-item">
                        <div class="contact-info">
                            <div class="contact-name">${contact.person_name}</div>
                            <div class="contact-role">${contact.role || ''} ${contact.company_name ? 'at ' + contact.company_name : ''}</div>
                        </div>
                        <div class="contact-actions">
                            <button class="btn-sm" onclick="navigateTo('contacts')">View</button>
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Error loading contacts:', error);
                document.getElementById('recent-contacts-list').innerHTML = '<div class="empty-state">Error loading contacts</div>';
            }
        }

        async function loadNextActions() {
            try {
                const response = await fetch('/api/followups/active');
                const todos = await response.json();

                const container = document.getElementById('next-actions-list');

                // Filter to only items with due dates and sort by due date
                const withDates = todos.filter(t => t.due_date).sort((a, b) => {
                    return new Date(a.due_date) - new Date(b.due_date);
                });

                if (withDates.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">⚡</div><p>No upcoming actions</p></div>';
                    return;
                }

                const next5 = withDates.slice(0, 5);
                container.innerHTML = next5.map(todo => `
                    <div class="todo-item">
                        <input type="checkbox" class="todo-checkbox" onclick="completeDashboardTodo(${todo.task_id})">
                        <div class="todo-content">
                            <div class="todo-title">${todo.task_title}</div>
                            <div class="todo-meta">Due: ${formatDueDate(todo.due_date)}</div>
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Error loading next actions:', error);
                document.getElementById('next-actions-list').innerHTML = '<div class="empty-state">Error loading actions</div>';
            }
        }

        async function loadRecentActivity() {
            try {
                const response = await fetch('/api/followups');
                const allTodos = await response.json();

                const completed = allTodos.filter(t => t.completed);

                const container = document.getElementById('recent-activity-list');

                if (completed.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">📅</div><p>No recent activity</p></div>';
                    return;
                }

                const recent5 = completed.slice(0, 5);
                container.innerHTML = recent5.map(todo => `
                    <div class="todo-item">
                        <div class="todo-content">
                            <div class="todo-title" style="text-decoration: line-through; color: #95a5a6;">${todo.task_title}</div>
                            <div class="todo-meta">Completed</div>
                        </div>
                    </div>
                `).join('');
            } catch (error) {
                console.error('Error loading recent activity:', error);
                document.getElementById('recent-activity-list').innerHTML = '<div class="empty-state">Error loading activity</div>';
            }
        }

        window.completeDashboardTodo = async function(taskId) {
            try {
                await fetch('/api/complete_followup', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ task_id: taskId })
                });
                loadDashboard();
            } catch (error) {
                console.error('Error completing todo:', error);
            }
        }

        function formatDueDate(dateStr) {
            if (!dateStr) return 'No date';
            const date = new Date(dateStr);
            return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
        }

        // ==================== CONTACTS FUNCTIONS ====================
        window.switchContactTab = function(tab) {
            document.querySelectorAll('.sub-tab').forEach(t => t.classList.remove('active'));
            document.querySelectorAll('.sub-tab-content').forEach(c => c.classList.remove('active'));

            event.target.classList.add('active');
            document.getElementById(tab + '-subtab').classList.add('active');
        }

        async function loadContacts() {
            const response = await fetch('/api/contacts');
            allContacts = await response.json();
            displayContacts(allContacts);
            await loadCompanies();
            await loadContactNames();
        }

        async function loadCompanies() {
            const response = await fetch('/api/companies');
            const data = await response.json();
            const companies = data.companies || [];
            const datalist = document.getElementById('companies-list');
            datalist.innerHTML = companies.map(c => `<option value="${c.name}">`).join('');
        }

        async function loadContactNames() {
            const datalist = document.getElementById('contacts-names-list');
            datalist.innerHTML = allContacts.map(c => `<option value="${c.person_name}">`).join('');
        }

        function displayContacts(contacts) {
            const container = document.getElementById('contacts-list');
            if (contacts.length === 0) {
                container.innerHTML = '<div class="loading">No contacts found</div>';
                return;
            }

            container.innerHTML = contacts.map(contact => `
                <div class="contact-card" data-contact-name="${contact.person_name}" data-contact-id="${contact.contact_id}">
                    <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 10px;">
                        <div style="flex: 1;">
                            <div class="contact-name">${contact.person_name}</div>
                            <div class="contact-company">${contact.company_name || 'No company'}</div>
                        </div>
                        <div style="display: flex; gap: 8px;">
                            <button class="btn-todo-contact" data-name="${contact.person_name}" style="padding: 6px 12px; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">✅ To Do's</button>
                            <button class="btn-edit-contact" data-name="${contact.person_name}" style="padding: 6px 12px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">✏️ Edit</button>
                        </div>
                    </div>
                    ${contact.role ? `<div class="contact-details">📋 ${contact.role}</div>` : ''}
                    ${contact.email ? `<div class="contact-details">✉️ ${contact.email}</div>` : ''}
                    ${contact.phone_number ? `<div class="contact-details">📞 ${contact.phone_number}</div>` : ''}
                    ${contact.linkedin_url ? `<div class="contact-details">🔗 <a href="${contact.linkedin_url}" target="_blank" style="color: #0077b5; text-decoration: none;">LinkedIn Profile</a></div>` : ''}
                    ${contact.opportunity ? `<div class="contact-details">🎯 ${contact.opportunity}</div>` : ''}
                    ${contact.relationship_types ? `
                        <div class="relationship-badges">
                            ${contact.relationship_types.split(', ').map(rel =>
                                `<span class="badge">${rel}</span>`
                            ).join('')}
                        </div>
                    ` : ''}
                    <div class="notes-section" style="margin-top: 15px; border-top: 1px solid #e0e0e0; padding-top: 15px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                            <strong style="color: #667eea;">📝 Notes</strong>
                            <button class="btn-add-note" data-name="${contact.person_name}" style="padding: 4px 12px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">+ Add Note</button>
                        </div>
                        <div id="note-input-${contact.person_name.replace(/\\s+/g, '-')}" style="display: none; margin-bottom: 10px;">
                            <textarea id="note-text-${contact.person_name.replace(/\\s+/g, '-')}" style="width: 100%; padding: 8px; border: 2px solid #667eea; border-radius: 4px; font-size: 14px; min-height: 60px;" placeholder="Add your note here..."></textarea>
                            <div style="margin-top: 8px; display: flex; gap: 8px;">
                                <button class="btn-save-note" data-name="${contact.person_name}" style="padding: 6px 16px; background: #28a745; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">Save Note</button>
                                <button class="btn-cancel-note" data-name="${contact.person_name}" style="padding: 6px 16px; background: #6c757d; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 12px;">Cancel</button>
                            </div>
                        </div>
                        <div id="notes-display-${contact.person_name.replace(/\\s+/g, '-')}" style="white-space: pre-wrap; font-size: 13px; color: #555; max-height: 200px; overflow-y: auto; background: #f9f9f9; padding: 10px; border-radius: 4px;">
                            ${contact.notes || '<em style="color: #999;">No notes yet</em>'}
                        </div>
                    </div>
                </div>
            `).join('');

            // Add event listeners for buttons
            document.querySelectorAll('.btn-edit-contact').forEach(btn => {
                btn.addEventListener('click', function() {
                    editContact(this.dataset.name);
                });
            });

            document.querySelectorAll('.btn-todo-contact').forEach(btn => {
                btn.addEventListener('click', function() {
                    createTodoForContact(this.dataset.name);
                });
            });

            document.querySelectorAll('.btn-add-note').forEach(btn => {
                btn.addEventListener('click', function() {
                    toggleNoteInput(this.dataset.name);
                });
            });

            document.querySelectorAll('.btn-save-note').forEach(btn => {
                btn.addEventListener('click', function() {
                    saveNote(this.dataset.name);
                });
            });

            document.querySelectorAll('.btn-cancel-note').forEach(btn => {
                btn.addEventListener('click', function() {
                    toggleNoteInput(this.dataset.name);
                });
            });
        }

        window.filterByRelationship = async function(rel) {
            currentFilter = rel;
            document.querySelectorAll('.filter-buttons .filter-btn').forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');

            if (rel === 'all') {
                displayContacts(allContacts);
            } else {
                const response = await fetch(`/api/contacts_by_relationship?type=${rel}`);
                const contacts = await response.json();
                displayContacts(contacts);
            }
        }

        document.getElementById('search-input').addEventListener('input', async (e) => {
            const term = e.target.value.trim();
            if (!term) {
                displayContacts(allContacts);
                return;
            }

            const response = await fetch(`/api/search?q=${encodeURIComponent(term)}`);
            const results = await response.json();
            displayContacts(results);
        });

        document.getElementById('add-contact-form').addEventListener('submit', async (e) => {
            e.preventDefault();

            const relationships = Array.from(document.querySelectorAll('input[name="relationship"]:checked'))
                .map(cb => cb.value);

            const data = {
                person_name: document.getElementById('name').value,
                company_name: document.getElementById('company').value,
                role: document.getElementById('role').value || null,
                email: document.getElementById('email').value || null,
                phone: document.getElementById('phone').value || null,
                opportunity: document.getElementById('opportunity').value || null,
                notes: document.getElementById('notes').value || null,
                relationships: relationships
            };

            const response = await fetch('/api/add_contact', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();
            const messageDiv = document.getElementById('add-message');

            if (result.success) {
                messageDiv.innerHTML = '<div class="message success">✓ Contact added successfully!</div>';
                document.getElementById('add-contact-form').reset();
                await loadContacts();
                setTimeout(() => messageDiv.innerHTML = '', 3000);
            } else {
                messageDiv.innerHTML = `<div class="message error">✗ Error: ${result.error}</div>`;
            }
        });

        document.getElementById('add-relationship-form').addEventListener('submit', async (e) => {
            e.preventDefault();

            const data = {
                person_name: document.getElementById('rel-contact-name').value,
                relationship_type: document.getElementById('rel-type').value
            };

            const response = await fetch('/api/add_relationship', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();
            const messageDiv = document.getElementById('rel-message');

            if (result.success) {
                messageDiv.innerHTML = '<div class="message success">✓ Relationship added successfully!</div>';
                document.getElementById('add-relationship-form').reset();
                await loadContacts();
                setTimeout(() => messageDiv.innerHTML = '', 3000);
            } else {
                messageDiv.innerHTML = `<div class="message error">✗ Error: ${result.error}</div>`;
            }
        });

        window.toggleNoteInput = function(personName) {
            const inputId = 'note-input-' + personName.replace(/\\s+/g, '-');
            const inputDiv = document.getElementById(inputId);
            if (inputDiv.style.display === 'none') {
                inputDiv.style.display = 'block';
            } else {
                inputDiv.style.display = 'none';
                const textId = 'note-text-' + personName.replace(/\\s+/g, '-');
                document.getElementById(textId).value = '';
            }
        }

        window.saveNote = async function(personName) {
            const textId = 'note-text-' + personName.replace(/\\s+/g, '-');
            const noteText = document.getElementById(textId).value.trim();

            if (!noteText) {
                alert('Please enter a note');
                return;
            }

            const data = {
                person_name: personName,
                note: noteText
            };

            const response = await fetch('/api/append_note', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();

            if (result.success) {
                const displayId = 'notes-display-' + personName.replace(/\\s+/g, '-');
                document.getElementById(displayId).innerHTML = result.notes.replace(/\\n/g, '<br>');

                toggleNoteInput(personName);

                const card = document.querySelector(`[data-contact-name="${personName}"]`);
                const successMsg = document.createElement('div');
                successMsg.style.cssText = 'background: #d4edda; color: #155724; padding: 8px; border-radius: 4px; margin-top: 10px; font-size: 13px;';
                successMsg.textContent = '✓ Note saved!';
                card.appendChild(successMsg);
                setTimeout(() => successMsg.remove(), 2000);
            } else {
                alert('Error saving note: ' + result.error);
            }
        }

        window.editContact = async function(personName) {
            currentEditingContact = personName;

            const contacts = await (await fetch('/api/contacts')).json();
            const contact = contacts.find(c => c.person_name === personName);

            if (!contact) return;

            const companiesResponse = await (await fetch('/api/companies')).json();
            const companiesData = companiesResponse.companies || [];
            const companyDatalist = document.getElementById('edit-companies-list');
            companyDatalist.innerHTML = '';

            companiesData.forEach(company => {
                const option = document.createElement('option');
                option.value = company.name;
                companyDatalist.appendChild(option);
            });

            document.getElementById('edit-name').value = contact.person_name || '';
            document.getElementById('edit-company').value = contact.company_name || '';
            document.getElementById('edit-email').value = contact.email || '';
            document.getElementById('edit-phone').value = contact.phone_number || '';
            document.getElementById('edit-role').value = contact.role || '';
            document.getElementById('edit-linkedin').value = contact.linkedin_url || '';
            document.getElementById('edit-opportunity').value = contact.opportunity || '';

            document.getElementById('editModal').style.display = 'block';
        }

        window.closeEditModal = function() {
            document.getElementById('editModal').style.display = 'none';
            currentEditingContact = null;
        }

        window.createTodoFromEditModal = function() {
            if (!currentEditingContact) return;
            const contactName = currentEditingContact;
            closeEditModal();
            createTodoForContact(contactName);
        }

        window.saveContactEdit = async function() {
            if (!currentEditingContact) return;

            const companyName = document.getElementById('edit-company').value;

            const data = {
                person_name: currentEditingContact,
                company_name: companyName || null,
                email: document.getElementById('edit-email').value,
                phone: document.getElementById('edit-phone').value,
                role: document.getElementById('edit-role').value,
                linkedin_url: document.getElementById('edit-linkedin').value,
                opportunity: document.getElementById('edit-opportunity').value,
                notes: null,
                last_connection: null,
                follow_up: null
            };

            const response = await fetch('/api/update_contact', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data)
            });

            const result = await response.json();

            if (result.success) {
                closeEditModal();
                loadContacts();
                showMessage('Contact updated successfully!', 'success');
            } else {
                showMessage('Error updating contact: ' + result.error, 'error');
            }
        }

        // ==================== FOLLOWUPS/TODOS FUNCTIONS (Asana-style) ====================

        window.createTodoForContact = function(contactName) {
            navigateTo('todos');
            setTimeout(() => {
                startInlineAdd('todo');
            }, 200);
        }

        async function loadFollowups() {
            try {
                const response = await fetch('/api/followups');
                allTodos = await response.json();
                allFollowups = allTodos; // backwards compat for dashboard
                renderTodoSections();
            } catch (err) {
                console.error('Failed to load todos:', err);
            }
        }

        function renderTodoSections() {
            const container = document.getElementById('todos-sections');
            if (!container) return;

            const sections = [
                { key: 'todo', label: 'To do', icon: '📋' },
                { key: 'doing', label: 'Doing', icon: '🔨' },
                { key: 'done', label: 'Done', icon: '✅' }
            ];

            container.innerHTML = sections.map(section => {
                const tasks = allTodos.filter(t => {
                    if (section.key === 'done') return t.section === 'done' || t.completed;
                    return t.section === section.key && !t.completed;
                });
                const isCollapsed = collapsedSections[section.key] || false;

                return `
                    <div class="todo-section ${isCollapsed ? 'collapsed' : ''}" data-section="${section.key}">
                        <div class="todo-section-header" onclick="toggleSection('${section.key}')">
                            <span class="section-collapse-icon">▾</span>
                            <span>${section.label}</span>
                            <span class="section-count">${tasks.length}</span>
                        </div>
                        <div class="todo-section-body">
                            ${tasks.map(task => renderTaskRow(task)).join('')}
                            <div class="todo-add-row" onclick="startInlineAdd('${section.key}')">
                                <div class="col-checkbox"></div>
                                <div class="col-name">+ Add task</div>
                            </div>
                        </div>
                    </div>
                `;
            }).join('');
        }

        function renderTaskRow(task) {
            const dueDate = task.due_date ? new Date(task.due_date) : null;
            const dueDateStr = dueDate ? dueDate.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) : '--';
            const isOverdue = dueDate && !task.completed && dueDate < new Date();
            const prio = (task.priority || 'medium').toLowerCase();
            const statusKey = (task.status || 'on_track');
            const statusClass = statusKey.replace('_', '-');
            const statusLabels = { on_track: 'On track', at_risk: 'At risk', off_track: 'Off track' };
            const prioLabel = prio.charAt(0).toUpperCase() + prio.slice(1);

            return `
                <div class="todo-row ${selectedTaskId === task.task_id ? 'selected' : ''}"
                     data-task-id="${task.task_id}" onclick="openTaskDetail(${task.task_id})">
                    <div class="col-checkbox">
                        <span class="row-checkbox ${task.completed ? 'checked' : ''}"
                              onclick="event.stopPropagation(); toggleComplete(${task.task_id})"></span>
                    </div>
                    <div class="col-name ${task.completed ? 'completed-text' : ''}">${task.task_title || 'Untitled'}</div>
                    <div class="col-assignee" style="color: var(--text-secondary)">${task.assignee || '--'}</div>
                    <div class="col-due-date ${isOverdue ? 'overdue-text' : ''}" style="color: ${isOverdue ? 'var(--priority-high-text)' : 'var(--text-secondary)'}">${dueDateStr}</div>
                    <div class="col-priority">
                        <span class="priority-tag ${prio}">${prioLabel}</span>
                    </div>
                    <div class="col-status">
                        ${!task.completed
                            ? `<span class="status-tag ${statusClass}">${statusLabels[statusKey] || 'On track'}</span>`
                            : '<span class="status-tag done-tag">Done</span>'}
                    </div>
                </div>
            `;
        }

        window.toggleSection = function(sectionKey) {
            collapsedSections[sectionKey] = !collapsedSections[sectionKey];
            renderTodoSections();
        }

        window.openTaskDetail = function(taskId) {
            selectedTaskId = taskId;
            isNewTask = false;
            const task = allTodos.find(t => t.task_id === taskId);
            if (!task) return;

            document.getElementById('detail-title').value = task.task_title || '';
            document.getElementById('detail-assignee').value = task.assignee || '';
            document.getElementById('detail-due-date').value = task.due_date ? task.due_date.slice(0, 16) : '';
            document.getElementById('detail-priority').value = task.priority || 'medium';
            document.getElementById('detail-status').value = task.status || 'on_track';
            document.getElementById('detail-section').value = task.section || 'todo';
            document.getElementById('detail-description').value = task.task_description || '';
            document.getElementById('detail-contact').textContent = task.person_name || '--';
            document.getElementById('detail-company').textContent = task.company_name || '--';

            // Update complete button (show it for existing tasks)
            const btn = document.getElementById('detail-complete-btn');
            btn.style.display = 'inline-flex';
            if (task.completed) {
                btn.style.borderColor = 'var(--text-tertiary)';
                btn.style.color = 'var(--text-tertiary)';
                btn.innerHTML = '<span class="checkmark-circle-btn" style="background: var(--text-tertiary); border-color: var(--text-tertiary);"></span> Completed';
            } else {
                btn.style.borderColor = '#4ade80';
                btn.style.color = '#4ade80';
                btn.innerHTML = '<span class="checkmark-circle-btn"></span> Mark complete';
            }

            document.getElementById('task-detail-panel').classList.add('open');
            document.getElementById('task-detail-overlay').classList.add('open');
            renderTodoSections();
        }

        window.closeTaskDetail = function() {
            selectedTaskId = null;
            isNewTask = false;
            document.getElementById('task-detail-panel').classList.remove('open');
            document.getElementById('task-detail-overlay').classList.remove('open');
            renderTodoSections();
        }

        // Open the detail panel in "create new task" mode
        window.openNewTaskDetail = function() {
            selectedTaskId = null;
            isNewTask = true;

            // Set defaults for new task
            document.getElementById('detail-title').value = '';
            document.getElementById('detail-assignee').value = '';
            const tomorrow = new Date();
            tomorrow.setDate(tomorrow.getDate() + 1);
            tomorrow.setHours(14, 0, 0, 0);
            const pad = (n) => String(n).padStart(2, '0');
            document.getElementById('detail-due-date').value =
                `${tomorrow.getFullYear()}-${pad(tomorrow.getMonth()+1)}-${pad(tomorrow.getDate())}T${pad(tomorrow.getHours())}:${pad(tomorrow.getMinutes())}`;
            document.getElementById('detail-priority').value = 'medium';
            document.getElementById('detail-status').value = 'on_track';
            document.getElementById('detail-section').value = 'todo';
            document.getElementById('detail-description').value = '';
            document.getElementById('detail-contact').textContent = '--';
            document.getElementById('detail-company').textContent = '--';

            // Update complete button to hidden for new tasks
            const btn = document.getElementById('detail-complete-btn');
            btn.style.display = 'none';

            document.getElementById('task-detail-panel').classList.add('open');
            document.getElementById('task-detail-overlay').classList.add('open');

            // Focus the title field
            setTimeout(() => document.getElementById('detail-title').focus(), 100);
        }

        window.saveTaskDetail = async function() {
            if (!selectedTaskId && !isNewTask) return;
            const title = document.getElementById('detail-title').value.trim();
            if (!title) {
                document.getElementById('detail-title').focus();
                return;
            }

            if (isNewTask) {
                // CREATE new task
                const data = {
                    task_title: title,
                    task_description: document.getElementById('detail-description').value,
                    due_date: document.getElementById('detail-due-date').value,
                    priority: document.getElementById('detail-priority').value,
                    status: document.getElementById('detail-status').value,
                    section: document.getElementById('detail-section').value,
                    assignee: document.getElementById('detail-assignee').value
                };

                try {
                    const response = await fetch('/api/add_followup', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(data)
                    });
                    const result = await response.json();
                    if (result.success) {
                        const msg = document.getElementById('followup-message');
                        msg.innerHTML = '<div class="message success">Task created!</div>';
                        setTimeout(() => msg.innerHTML = '', 2000);
                        isNewTask = false;
                        closeTaskDetail();
                        await loadFollowups();
                    }
                } catch (err) {
                    console.error('Create failed:', err);
                }
            } else {
                // UPDATE existing task
                const data = {
                    task_id: selectedTaskId,
                    task_title: title,
                    task_description: document.getElementById('detail-description').value,
                    due_date: document.getElementById('detail-due-date').value,
                    priority: document.getElementById('detail-priority').value,
                    status: document.getElementById('detail-status').value,
                    section: document.getElementById('detail-section').value,
                    assignee: document.getElementById('detail-assignee').value
                };

                try {
                    const response = await fetch('/api/update_followup', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(data)
                    });
                    const result = await response.json();
                    if (result.success) {
                        const msg = document.getElementById('followup-message');
                        msg.innerHTML = '<div class="message success">Task saved!</div>';
                        setTimeout(() => msg.innerHTML = '', 2000);
                        await loadFollowups();
                        openTaskDetail(selectedTaskId);
                    }
                } catch (err) {
                    console.error('Save failed:', err);
                }
            }
        }

        window.completeFromDetail = async function() {
            if (!selectedTaskId) return;
            const task = allTodos.find(t => t.task_id === selectedTaskId);
            if (task && task.completed) return; // already done

            try {
                const response = await fetch('/api/complete_followup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ task_id: selectedTaskId })
                });
                const result = await response.json();
                if (result.success) {
                    await loadFollowups();
                    openTaskDetail(selectedTaskId);
                }
            } catch (err) {
                console.error('Complete failed:', err);
            }
        }

        window.deleteFromDetail = async function() {
            if (!selectedTaskId) return;
            if (!confirm('Delete this task permanently?')) return;

            try {
                const response = await fetch('/api/delete_followup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ task_id: selectedTaskId })
                });
                const result = await response.json();
                if (result.success) {
                    closeTaskDetail();
                    await loadFollowups();
                    const msg = document.getElementById('followup-message');
                    msg.innerHTML = '<div class="message success">Task deleted.</div>';
                    setTimeout(() => msg.innerHTML = '', 2000);
                }
            } catch (err) {
                console.error('Delete failed:', err);
            }
        }

        window.toggleComplete = async function(taskId) {
            try {
                const response = await fetch('/api/complete_followup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ task_id: taskId })
                });
                const result = await response.json();
                if (result.success) {
                    await loadFollowups();
                }
            } catch (err) {
                console.error('Toggle complete failed:', err);
            }
        }

        window.startInlineAdd = function(section) {
            const sectionEl = document.querySelector(`.todo-section[data-section="${section}"] .todo-add-row`);
            if (!sectionEl) return;
            sectionEl.outerHTML = `
                <div class="inline-add-form">
                    <div class="col-checkbox"></div>
                    <input type="text" class="inline-add-input" placeholder="Task name..."
                           autofocus onkeydown="handleInlineAdd(event, '${section}')"
                           onblur="setTimeout(() => cancelInlineAdd('${section}'), 200)">
                    <div></div><div></div><div></div><div></div>
                </div>
            `;
            const input = document.querySelector('.inline-add-input');
            if (input) input.focus();
        }

        window.handleInlineAdd = async function(event, section) {
            if (event.key === 'Escape') {
                renderTodoSections();
                return;
            }
            if (event.key !== 'Enter') return;
            const title = event.target.value.trim();
            if (!title) return;

            event.target.disabled = true;

            const tomorrow = new Date();
            tomorrow.setDate(tomorrow.getDate() + 1);
            tomorrow.setHours(14, 0, 0, 0);

            const data = {
                task_title: title,
                due_date: tomorrow.toISOString().slice(0, 16),
                priority: 'medium',
                section: section,
                status: 'on_track'
            };

            try {
                await fetch('/api/add_followup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                await loadFollowups();
            } catch (err) {
                console.error('Add task failed:', err);
                renderTodoSections();
            }
        }

        window.cancelInlineAdd = function(section) {
            renderTodoSections();
        }

        // Legacy compat functions
        window.completeFollowup = window.toggleComplete;
        window.deleteFollowup = async function(taskId) {
            if (!confirm('Delete this task?')) return;
            try {
                await fetch('/api/delete_followup', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ task_id: taskId })
                });
                await loadFollowups();
            } catch (err) { console.error(err); }
        }

        // ==================== OPPORTUNITY GRID FUNCTIONS ====================
        window.showAddCompanyModal = function() {
            document.getElementById('addCompanyGridModal').style.display = 'block';
        }

        window.showAddCriteriaModal = function() {
            document.getElementById('addCriteriaGridModal').style.display = 'block';
        }

        window.closeGridModal = function(modalId) {
            document.getElementById(modalId).style.display = 'none';
        }

        let currentStageFilter = 'all';
        let showClosedLost = false;

        window.loadOpportunityGrid = async function() {
            try {
                const response = await fetch('/api/opportunity-grid');
                opportunityGridData = await response.json();
                renderStageSummary();
                renderOpportunityGrid();
            } catch (error) {
                console.error('Error loading grid:', error);
            }
        }

        function renderStageSummary() {
            const { companies } = opportunityGridData;
            if (!companies) return;

            const stageCounts = {
                'Inquiry': 0,
                'Screening': 0,
                'Interviewing': 0,
                'Offer': 0,
                'Accepted': 0,
                'Closed Lost': 0
            };

            companies.forEach(company => {
                const stage = company.opportunity_stage || 'Inquiry';
                // Map Declined and Withdrawn to "Closed Lost"
                if (stage === 'Declined' || stage === 'Withdrawn') {
                    stageCounts['Closed Lost']++;
                } else if (stageCounts[stage] !== undefined) {
                    stageCounts[stage]++;
                }
            });

            const total = companies.length;
            const summaryContainer = document.getElementById('stage-summary');

            let html = `
                <div class="stage-stat ${currentStageFilter === 'all' ? 'active' : ''}" onclick="filterByStage('all')">
                    <div class="stage-stat-label">Total</div>
                    <div class="stage-stat-number">${total}</div>
                    <div class="stage-stat-percent">100%</div>
                </div>
            `;

            Object.keys(stageCounts).forEach(stage => {
                const count = stageCounts[stage];
                const percent = total > 0 ? Math.round((count / total) * 100) : 0;
                html += `
                    <div class="stage-stat ${currentStageFilter === stage ? 'active' : ''}" onclick="filterByStage('${stage}')">
                        <div class="stage-stat-label">${stage}</div>
                        <div class="stage-stat-number">${count}</div>
                        <div class="stage-stat-percent">${percent}%</div>
                    </div>
                `;
            });

            summaryContainer.innerHTML = html;
        }

        window.filterByStage = function(stage) {
            currentStageFilter = stage;
            renderStageSummary();
            renderOpportunityGrid();
        }

        window.toggleClosedLost = function() {
            showClosedLost = !showClosedLost;
            const btn = document.getElementById('toggle-closed-lost-btn');
            if (showClosedLost) {
                btn.textContent = 'Hide Closed Lost';
                btn.classList.add('showing');
            } else {
                btn.textContent = 'Show Closed Lost';
                btn.classList.remove('showing');
            }
            renderOpportunityGrid();
        }

        function renderOpportunityGrid() {
            const container = document.getElementById('grid-container');
            const { companies, criteria, scores } = opportunityGridData;

            if (!companies || companies.length === 0) {
                container.innerHTML = '<p style="padding: 20px; text-align: center; color: #666;">No companies added yet. Click "+ Add Company" to get started!</p>';
                return;
            }

            // Filter companies based on current stage filter and closed lost toggle
            let filteredCompanies = companies.filter(company => {
                const stage = company.opportunity_stage || 'Inquiry';
                const isClosedLost = (stage === 'Declined' || stage === 'Withdrawn');

                // Filter out Closed Lost opportunities if showClosedLost is false
                if (!showClosedLost && isClosedLost) {
                    return false;
                }

                // Apply stage filter
                if (currentStageFilter !== 'all') {
                    // If filtering by "Closed Lost", show both Declined and Withdrawn
                    if (currentStageFilter === 'Closed Lost') {
                        return isClosedLost;
                    }
                    // Otherwise, exact match
                    if (stage !== currentStageFilter) {
                        return false;
                    }
                }

                return true;
            });

            // Sort by total score descending
            filteredCompanies.sort((a, b) =>
                calculateCompanyScore(b.grid_company_id) - calculateCompanyScore(a.grid_company_id)
            );

            if (filteredCompanies.length === 0) {
                container.innerHTML = '<p style="padding: 20px; text-align: center; color: #666;">No opportunities match the selected filters.</p>';
                return;
            }

            let html = '<table class="grid-table"><thead><tr>';
            html += '<th class="criteria-col">Criteria</th>';

            filteredCompanies.forEach(company => {
                const totalScore = calculateCompanyScore(company.grid_company_id, opportunityGridData);
                html += `
                    <th class="company-header">
                        <div onclick="viewOpportunityDetail(${company.grid_company_id})"
                             style="cursor: pointer; padding: 5px; border-radius: 5px; transition: background 0.2s;"
                             onmouseover="this.style.background='rgba(255,255,255,0.1)'"
                             onmouseout="this.style.background='transparent'"
                             title="Click to view opportunity details">
                            <div class="company-name">${company.company_name}</div>
                            ${company.opportunity_stage ? `<div style="font-size: 11px; opacity: 0.85; margin-top: 3px;">🎯 ${company.opportunity_stage}</div>` : ''}
                            ${company.role ? `<div class="company-role">${company.role}</div>` : ''}
                        </div>
                        <div class="total-score">${totalScore}</div>
                        <button onclick="viewOpportunityDetail(${company.grid_company_id})"
                                style="margin-top: 8px; padding: 5px 12px; background: rgba(255,255,255,0.12);
                                       border: 1px solid rgba(255,255,255,0.25); color: #94A3B8; border-radius: 6px; cursor: pointer; font-size: 12px; font-family: inherit; font-weight: 500;">
                            View Details
                        </button>
                        <button onclick="viewCompanyDetails(${company.grid_company_id}); event.stopPropagation();"
                                style="margin-top: 4px; padding: 4px 12px; background: transparent;
                                       border: 1px solid rgba(255,255,255,0.15); color: #64748B; border-radius: 6px; cursor: pointer; font-size: 11px; font-family: inherit;">
                            Edit
                        </button>
                    </th>
                `;
            });

            html += '</tr></thead><tbody>';

            // Section point totals
            const sectionTotals = { 'ROLE FIT': 70, 'COMPANY QUALITY': 50, 'CULTURE': 25, 'COMPENSATION': 30, 'PASSION / GUT': 25 };
            let lastCategory = null;

            criteria.forEach(crit => {
                // Inject section header row when category changes
                if (crit.category && crit.category !== lastCategory) {
                    lastCategory = crit.category;
                    const sectionPts = sectionTotals[crit.category] ? ` — ${sectionTotals[crit.category]} pts` : '';
                    const colSpan = filteredCompanies.length + 1;
                    html += `<tr class="criteria-section-header"><td colspan="${colSpan}">${crit.category}${sectionPts}</td></tr>`;
                }

                html += '<tr>';
                html += `
                    <td class="criteria-label">
                        <div class="criteria-with-score">
                            <span>${crit.criteria_name}</span>
                            <div style="display: flex; align-items: center; gap: 8px;">
                                <span class="criteria-score-badge">${crit.max_score}</span>
                                <button onclick="editGridCriteria(${crit.criteria_id}, '${crit.criteria_name.replace(/'/g, "\\'")}', ${crit.max_score})"
                                        style="padding: 4px 8px; background: #667eea; color: white; border: none;
                                               border-radius: 3px; cursor: pointer; font-size: 11px;"
                                        title="Edit criteria">
                                    ✏️
                                </button>
                            </div>
                        </div>
                    </td>
                `;

                filteredCompanies.forEach(company => {
                    const currentVal = scores[company.grid_company_id]?.[crit.criteria_id] ?? '';
                    html += `
                        <td class="score-cell">
                            <input type="number"
                                class="grid-score-input"
                                min="0"
                                max="${crit.max_score}"
                                value="${currentVal}"
                                placeholder="—"
                                data-company="${company.grid_company_id}"
                                data-criteria="${crit.criteria_id}"
                                data-max="${crit.max_score}"
                                onchange="saveGridScore(this)"
                                onclick="this.select()">
                        </td>
                    `;
                });

                html += '</tr>';
            });

            html += '</tbody></table>';
            container.innerHTML = html;
        }

        function calculateCompanyScore(companyId, gridData = opportunityGridData) {
            const { criteria, scores } = gridData;
            let total = 0;

            if (criteria && scores) {
                criteria.forEach(crit => {
                    const val = scores[companyId]?.[crit.criteria_id];
                    if (val !== null && val !== undefined) {
                        total += parseInt(val) || 0;
                    }
                });
            }

            return total;
        }

        async function saveGridScore(input) {
            const companyId = parseInt(input.dataset.company);
            const criteriaId = parseInt(input.dataset.criteria);
            const max = parseInt(input.dataset.max);
            let val = input.value === '' ? null : parseInt(input.value);

            // Clamp to valid range
            if (val !== null) {
                if (val < 0) val = 0;
                if (val > max) val = max;
                input.value = val;
            }

            // Update local data so score totals refresh immediately
            if (!opportunityGridData.scores[companyId]) opportunityGridData.scores[companyId] = {};
            opportunityGridData.scores[companyId][criteriaId] = val;

            // Re-render score headers to reflect updated totals
            renderOpportunityGrid(opportunityGridData);

            try {
                await fetch('/api/set-grid-score', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        grid_company_id: companyId,
                        criteria_id: criteriaId,
                        numeric_score: val
                    })
                });
            } catch(e) {
                console.error('Failed to save score:', e);
            }
        }

        let currentOpportunity = null;
        let isEditingOpportunity = false;

        // Helper function to format text with bold and bullets
        function formatText(text) {
            if (!text) return text;

            // Convert markdown-style formatting to HTML
            let formatted = text;

            // Handle bold text: **text** or __text__
            formatted = formatted.replace(/[*][*](.*?)[*][*]/g, '<strong>$1</strong>');
            formatted = formatted.replace(/__(.*?)__/g, '<strong>$1</strong>');

            // Handle bullet points: lines starting with - or * or •
            formatted = formatted.replace(/^[-*•]\\s+(.+)$/gm, '<li>$1</li>');

            // Wrap consecutive list items in ul tags
            formatted = formatted.replace(/(<li>[\\s\\S]*?<\\/li>\\n?)+/g, function(match) {
                return '<ul>' + match + '</ul>';
            });

            // Convert line breaks to <br> for non-list items
            formatted = formatted.replace(/\\n(?!<\\/?(ul|li)>)/g, '<br>');

            return formatted;
        }

        window.viewOpportunityDetail = async function(companyId) {
            try {
                const response = await fetch(`/api/opportunity/${companyId}`);
                const opportunity = await response.json();

                if (opportunity.error) {
                    console.error('Error loading opportunity:', opportunity.error);
                    return;
                }

                currentOpportunity = opportunity;
                isEditingOpportunity = false;
                renderOpportunityDetail();
                navigateTo('opportunity-detail');
            } catch (error) {
                console.error('Error loading opportunity detail:', error);
                document.getElementById('opportunity-detail-content').innerHTML =
                    '<div class="empty-state"><div class="empty-state-icon">❌</div><p>Error loading opportunity details</p></div>';
            }
        }

        function renderOpportunityDetail() {
            if (!currentOpportunity) return;

            const opportunity = currentOpportunity;
            const companyId = opportunity.grid_company_id;
            const totalScore = calculateCompanyScore(companyId);

            const stageClass = opportunity.opportunity_stage ?
                `stage-${opportunity.opportunity_stage.toLowerCase().replace(/\\s+/g, '-')}` : '';

            const content = document.getElementById('opportunity-detail-content');

            if (isEditingOpportunity) {
                content.innerHTML = `
                    <div class="opportunity-detail-header">
                        <div style="display: flex; justify-content: space-between; align-items: start;">
                            <div>
                                <h2>Edit Opportunity</h2>
                                <div class="role">${opportunity.company_name || 'Unnamed Opportunity'}</div>
                            </div>
                            <div style="display: flex; gap: 10px;">
                                <button onclick="saveOpportunityEdit()" class="btn" style="background: #28a745; color: white; padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;">
                                    💾 Save Changes
                                </button>
                                <button onclick="cancelOpportunityEdit()" class="btn" style="background: #6c757d; color: white; padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;">
                                    ✕ Cancel
                                </button>
                            </div>
                        </div>
                    </div>

                    <form id="opportunity-edit-form">
                        <div class="detail-section">
                            <h3>📊 Company Information</h3>
                            <div class="detail-grid">
                                <div class="detail-field">
                                    <label>Company Name *</label>
                                    <input type="text" id="edit-company-name" class="form-control" value="${opportunity.company_name || ''}" required>
                                </div>
                                <div class="detail-field">
                                    <label>Location</label>
                                    <input type="text" id="edit-location" class="form-control" value="${opportunity.location || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>LinkedIn</label>
                                    <input type="url" id="edit-company-linkedin" class="form-control" value="${opportunity.company_linkedin || ''}" placeholder="https://linkedin.com/company/...">
                                </div>
                                <div class="detail-field">
                                    <label>Stage</label>
                                    <input type="text" id="edit-stage" class="form-control" value="${opportunity.stage || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Employees</label>
                                    <input type="text" id="edit-employees" class="form-control" value="${opportunity.employees || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Employee LinkedIn</label>
                                    <input type="url" id="edit-employee-linkedin-url" class="form-control" value="${opportunity.employee_linkedin_url || ''}" placeholder="https://linkedin.com/company/.../people">
                                </div>
                            </div>
                        </div>

                        <div class="detail-section">
                            <h3>💼 Role Details</h3>
                            <div class="detail-grid">
                                <div class="detail-field">
                                    <label>Role</label>
                                    <input type="text" id="edit-role" class="form-control" value="${opportunity.role || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Opportunity Stage</label>
                                    <select id="edit-opportunity-stage" class="form-control">
                                        <option value="Inquiry" ${opportunity.opportunity_stage === 'Inquiry' ? 'selected' : ''}>Inquiry</option>
                                        <option value="Screening" ${opportunity.opportunity_stage === 'Screening' ? 'selected' : ''}>Screening</option>
                                        <option value="Interviewing" ${opportunity.opportunity_stage === 'Interviewing' ? 'selected' : ''}>Interviewing</option>
                                        <option value="Offer" ${opportunity.opportunity_stage === 'Offer' ? 'selected' : ''}>Offer</option>
                                        <option value="Accepted" ${opportunity.opportunity_stage === 'Accepted' ? 'selected' : ''}>Accepted</option>
                                        <option value="Declined" ${opportunity.opportunity_stage === 'Declined' ? 'selected' : ''}>Closed Lost</option>
                                        <option value="Withdrawn" ${opportunity.opportunity_stage === 'Withdrawn' ? 'selected' : ''}>Withdrawn</option>
                                    </select>
                                </div>
                                <div class="detail-field" style="grid-column: 1 / -1;">
                                    <label>Next Step</label>
                                    <textarea id="edit-next-step" class="form-control" rows="3">${opportunity.next_step || ''}</textarea>
                                </div>
                            </div>
                        </div>

                        <div class="detail-section">
                            <h3>💰 Compensation</h3>
                            <div class="detail-grid">
                                <div class="detail-field">
                                    <label>Base Salary</label>
                                    <input type="text" id="edit-base-salary" class="form-control" value="${opportunity.base_salary || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Incentive</label>
                                    <input type="text" id="edit-incentive" class="form-control" value="${opportunity.incentive || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Equity</label>
                                    <input type="text" id="edit-equity" class="form-control" value="${opportunity.equity || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Cash Comp</label>
                                    <input type="text" id="edit-cash-comp" class="form-control" value="${opportunity.cash_comp || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Targeted Annual Comp</label>
                                    <input type="text" id="edit-targeted-annual-comp" class="form-control" value="${opportunity.targeted_annual_comp || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Total 4-Year Comp</label>
                                    <input type="text" id="edit-total-4year-comp" class="form-control" value="${opportunity.total_4year_comp || ''}">
                                </div>
                                <div class="detail-field" style="grid-column: 1 / -1;">
                                    <label>Benefits</label>
                                    <textarea id="edit-benefits" class="form-control" rows="3">${opportunity.benefits || ''}</textarea>
                                </div>
                            </div>
                        </div>

                        <div class="detail-section">
                            <h3>💡 Company Details</h3>
                            <div class="detail-grid">
                                <div class="detail-field">
                                    <label>Raised Amount</label>
                                    <input type="text" id="edit-raised-amount" class="form-control" value="${opportunity.raised_amount || ''}">
                                </div>
                                <div class="detail-field">
                                    <label>Revenue</label>
                                    <input type="text" id="edit-revenue" class="form-control" value="${opportunity.revenue || ''}">
                                </div>
                            </div>
                            <div class="detail-field" style="margin-top: 20px;">
                                <label>Funding Details</label>
                                <textarea id="edit-funding-details" class="form-control" rows="4">${opportunity.funding_details || ''}</textarea>
                            </div>
                            <div class="detail-field" style="margin-top: 20px;">
                                <label>Core Solution</label>
                                <textarea id="edit-core-solution" class="form-control" rows="4">${opportunity.core_solution || ''}</textarea>
                            </div>
                        </div>
                    </form>
                `;
            } else {
                // VIEW MODE - Apply formatText to specific fields
                const formattedBenefits = opportunity.benefits ? formatText(opportunity.benefits) : '<span class="empty-value">Not specified</span>';
                const formattedFundingDetails = opportunity.funding_details ? formatText(opportunity.funding_details) : '<span class="empty-value">Not specified - click "Fill with Claude" to populate</span>';
                const formattedCoreSolution = opportunity.core_solution ? formatText(opportunity.core_solution) : '<span class="empty-value">Not specified - click "Fill with Claude" to populate</span>';
                const formattedNextStep = opportunity.next_step ? formatText(opportunity.next_step) : '<span class="empty-value">Not specified</span>';

                content.innerHTML = `
                    <div class="opportunity-detail-header">
                        <div style="display: flex; justify-content: space-between; align-items: start;">
                            <div>
                                <h2>${opportunity.company_name || 'Unnamed Opportunity'}</h2>
                                <div class="role">${opportunity.role || 'No role specified'}</div>
                                <div style="margin-top: 15px; display: flex; align-items: center; gap: 15px;">
                                    <span class="stage-badge ${stageClass}">${opportunity.opportunity_stage || 'Inquiry'}</span>
                                    <span style="font-size: 20px; font-weight: bold;">Score: ${totalScore}</span>
                                </div>
                            </div>
                            <button onclick="toggleOpportunityEdit()" class="btn" style="background: rgba(255,255,255,0.2); color: white; padding: 10px 20px; border: none; border-radius: 6px; cursor: pointer; font-weight: 600;">
                                ✏️ Edit
                            </button>
                        </div>
                    </div>

                    <div class="detail-section">
                        <h3>📊 Company Information</h3>
                        <div class="detail-grid">
                            <div class="detail-field">
                                <label>Company Name</label>
                                <div class="value">${opportunity.company_name || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Location</label>
                                <div class="value">${opportunity.location || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>LinkedIn</label>
                                <div class="value">${opportunity.company_linkedin ?
                                    `<a href="${opportunity.company_linkedin}" target="_blank">View Profile</a>` :
                                    '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Stage</label>
                                <div class="value">${opportunity.stage || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Employees</label>
                                <div class="value">${opportunity.employees || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Employee LinkedIn</label>
                                <div class="value">${opportunity.employee_linkedin_url ?
                                    `<a href="${opportunity.employee_linkedin_url}" target="_blank">View Employees</a>` :
                                    '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                        </div>
                    </div>

                    <div class="detail-section">
                        <h3>💼 Role Details</h3>
                        <div class="detail-grid">
                            <div class="detail-field">
                                <label>Role</label>
                                <div class="value">${opportunity.role || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Next Step</label>
                                <div class="value">${formattedNextStep}</div>
                            </div>
                        </div>
                    </div>

                    <div class="detail-section">
                        <h3>💰 Compensation</h3>
                        <div class="detail-grid">
                            <div class="detail-field">
                                <label>Base Salary</label>
                                <div class="value">${opportunity.base_salary || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Incentive</label>
                                <div class="value">${opportunity.incentive || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Equity</label>
                                <div class="value">${opportunity.equity || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Cash Comp</label>
                                <div class="value">${opportunity.cash_comp || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Targeted Annual Comp</label>
                                <div class="value">${opportunity.targeted_annual_comp || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Total 4-Year Comp</label>
                                <div class="value">${opportunity.total_4year_comp || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Benefits</label>
                                <div class="value">${formattedBenefits}</div>
                            </div>
                        </div>
                    </div>

                    <div class="detail-section">
                        <h3>💡 Company Details</h3>
                        <div class="detail-grid">
                            <div class="detail-field">
                                <label>Raised Amount</label>
                                <div class="value">${opportunity.raised_amount || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                            <div class="detail-field">
                                <label>Revenue</label>
                                <div class="value">${opportunity.revenue || '<span class="empty-value">Not specified</span>'}</div>
                            </div>
                        </div>
                        <div class="detail-field" style="margin-top: 20px;">
                            <label>Funding Details</label>
                            <div class="value">${opportunity.funding_details || '<span class="empty-value">Not specified - click "Fill with Claude" to populate</span>'}</div>
                        </div>
                        <div class="detail-field" style="margin-top: 20px;">
                            <label>Core Solution</label>
                            <div class="value">${formattedCoreSolution}</div>
                        </div>
                    </div>

                    <div style="text-align: center; margin-top: 30px;">
                        <button class="claude-chat-btn" onclick="fillWithClaude(${companyId}, '${opportunity.company_name || ''}')">
                            🤖 Fill Missing Details with Claude
                        </button>
                    </div>
                `;
            }
        }

        window.toggleOpportunityEdit = function() {
            isEditingOpportunity = true;
            renderOpportunityDetail();
        }

        window.cancelOpportunityEdit = function() {
            isEditingOpportunity = false;
            renderOpportunityDetail();
        }

        window.saveOpportunityEdit = async function() {
            try {
                const data = {
                    grid_company_id: currentOpportunity.grid_company_id,
                    company_name: document.getElementById('edit-company-name').value,
                    location: document.getElementById('edit-location').value,
                    company_linkedin: document.getElementById('edit-company-linkedin').value,
                    stage: document.getElementById('edit-stage').value,
                    employees: document.getElementById('edit-employees').value,
                    employee_linkedin_url: document.getElementById('edit-employee-linkedin-url').value,
                    role: document.getElementById('edit-role').value,
                    opportunity_stage: document.getElementById('edit-opportunity-stage').value,
                    next_step: document.getElementById('edit-next-step').value,
                    base_salary: document.getElementById('edit-base-salary').value,
                    incentive: document.getElementById('edit-incentive').value,
                    equity: document.getElementById('edit-equity').value,
                    cash_comp: document.getElementById('edit-cash-comp').value,
                    targeted_annual_comp: document.getElementById('edit-targeted-annual-comp').value,
                    total_4year_comp: document.getElementById('edit-total-4year-comp').value,
                    benefits: document.getElementById('edit-benefits').value,
                    raised_amount: document.getElementById('edit-raised-amount').value,
                    revenue: document.getElementById('edit-revenue').value,
                    funding_details: document.getElementById('edit-funding-details').value,
                    core_solution: document.getElementById('edit-core-solution').value
                };

                const response = await fetch('/api/update-grid-company', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    // Reload the opportunity data
                    await viewOpportunityDetail(currentOpportunity.grid_company_id);
                    showMessage('Opportunity updated successfully!', 'success');
                } else {
                    showMessage('Error: ' + result.error, 'error');
                }
            } catch (error) {
                console.error('Error saving opportunity:', error);
                showMessage('Error saving opportunity', 'error');
            }
        }

        window.fillWithClaude = function(companyId, companyName) {
            const message = `I need help researching information about ${companyName}. Please provide:\n\n` +
                `1. Funding Details - Total funding raised, major investors, and funding rounds\n` +
                `2. Number of Active Employees - Link to LinkedIn company people page\n` +
                `3. Location - Company headquarters address\n` +
                `4. Core Solution - What does the product do and what problem does it solve?\n\n` +
                `Once you have this information, I can manually add it to the opportunity details.`;

            if (confirm('This will open a prompt that you can copy to Claude Chat to research this company. Click OK to continue.')) {
                const textarea = document.createElement('textarea');
                textarea.value = message;
                document.body.appendChild(textarea);
                textarea.select();
                document.execCommand('copy');
                document.body.removeChild(textarea);

                alert('Prompt copied to clipboard! Paste it into Claude Chat to get the research, then come back here and click the Edit button to add the information.');
            }
        }

        window.toggleGridScore = async function(companyId, criteriaId) {
            try {
                await fetch('/api/toggle-grid-score', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ grid_company_id: companyId, criteria_id: criteriaId })
                });
                await loadOpportunityGrid();
            } catch (error) {
                console.error('Error toggling score:', error);
            }
        }

        window.saveGridCompany = async function(event) {
            event.preventDefault();

            const data = {
                company_name: document.getElementById('grid-company-name').value,
                opportunity_stage: document.getElementById('grid-company-opportunity-stage').value,
                company_linkedin: document.getElementById('grid-company-linkedin').value,
                location: document.getElementById('grid-company-location').value,
                role: document.getElementById('grid-company-role').value,
                stage: document.getElementById('grid-company-stage').value,
                employees: document.getElementById('grid-company-employees').value
            };

            try {
                const response = await fetch('/api/add-grid-company', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    closeGridModal('addCompanyGridModal');
                    event.target.reset();
                    await loadOpportunityGrid();
                    showMessage('Company added successfully!', 'success');
                } else {
                    showMessage('Error: ' + result.error, 'error');
                }
            } catch (error) {
                console.error('Error saving company:', error);
                showMessage('Error saving company', 'error');
            }
        }

        window.saveGridCriteria = async function(event) {
            event.preventDefault();

            const data = {
                criteria_name: document.getElementById('grid-criteria-name').value,
                max_score: parseInt(document.getElementById('grid-criteria-score').value),
                category: document.getElementById('grid-criteria-category').value
            };

            try {
                const response = await fetch('/api/add-grid-criteria', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    closeGridModal('addCriteriaGridModal');
                    event.target.reset();
                    await loadOpportunityGrid();
                    showMessage('Criteria added successfully!', 'success');
                } else {
                    showMessage('Error: ' + result.error, 'error');
                }
            } catch (error) {
                console.error('Error saving criteria:', error);
                showMessage('Error saving criteria', 'error');
            }
        }

        window.editGridCriteria = function(criteriaId, criteriaName, maxScore) {
            currentEditingCriteriaId = criteriaId;
            document.getElementById('edit-grid-criteria-id').value = criteriaId;
            document.getElementById('edit-grid-criteria-name').value = criteriaName;
            document.getElementById('edit-grid-criteria-score').value = maxScore;
            document.getElementById('editCriteriaGridModal').style.display = 'block';
        }

        window.updateGridCriteria = async function(event) {
            event.preventDefault();

            const data = {
                criteria_id: currentEditingCriteriaId,
                criteria_name: document.getElementById('edit-grid-criteria-name').value,
                max_score: parseInt(document.getElementById('edit-grid-criteria-score').value)
            };

            try {
                const response = await fetch('/api/update-grid-criteria', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    closeGridModal('editCriteriaGridModal');
                    await loadOpportunityGrid();
                    showMessage('Criteria updated successfully!', 'success');
                } else {
                    showMessage('Error: ' + result.error, 'error');
                }
            } catch (error) {
                console.error('Error updating criteria:', error);
                showMessage('Error updating criteria', 'error');
            }
        }

        window.confirmDeleteCriteria = async function() {
            if (!confirm('Delete this criteria? This will remove it from all companies.')) return;

            try {
                const response = await fetch('/api/delete-grid-criteria', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ criteria_id: currentEditingCriteriaId })
                });

                const result = await response.json();

                if (result.success) {
                    closeGridModal('editCriteriaGridModal');
                    await loadOpportunityGrid();
                    showMessage('Criteria deleted', 'success');
                } else {
                    showMessage('Error deleting criteria', 'error');
                }
            } catch (error) {
                console.error('Error deleting criteria:', error);
                showMessage('Error deleting criteria', 'error');
            }
        }

        window.viewCompanyDetails = function(companyId) {
            const company = opportunityGridData.companies.find(c => c.grid_company_id === companyId);
            if (!company) return;

            currentEditingCompanyId = companyId;

            document.getElementById('company-detail-id').value = companyId;
            document.getElementById('company-detail-name').value = company.company_name || '';
            document.getElementById('company-detail-opportunity-stage').value = company.opportunity_stage || 'Inquiry';
            document.getElementById('company-detail-linkedin').value = company.company_linkedin || '';
            document.getElementById('company-detail-location').value = company.location || '';
            document.getElementById('company-detail-role').value = company.role || '';
            document.getElementById('company-detail-stage').value = company.stage || '';
            document.getElementById('company-detail-employees').value = company.employees || '';
            document.getElementById('company-detail-raised').value = company.raised_amount || '';
            document.getElementById('company-detail-revenue').value = company.revenue || '';
            document.getElementById('company-detail-next-step').value = company.next_step || '';
            document.getElementById('company-detail-benefits').value = company.benefits || '';
            document.getElementById('company-detail-cash').value = company.cash_comp || '';
            document.getElementById('company-detail-base').value = company.base_salary || '';
            document.getElementById('company-detail-incentive').value = company.incentive || '';
            document.getElementById('company-detail-equity').value = company.equity || '';
            document.getElementById('company-detail-targeted').value = company.targeted_annual_comp || '';
            document.getElementById('company-detail-total4year').value = company.total_4year_comp || '';

            const linkedinDisplay = document.getElementById('linkedin-link-display');
            if (company.company_linkedin) {
                linkedinDisplay.innerHTML = `<a href="${company.company_linkedin}" target="_blank" style="color: #0077b5; text-decoration: none;">🔗 View LinkedIn Profile</a>`;
            } else {
                linkedinDisplay.innerHTML = '';
            }

            document.getElementById('companyDetailModal').style.display = 'block';
        }

        window.updateGridCompanyDetails = async function(event) {
            event.preventDefault();

            const data = {
                grid_company_id: currentEditingCompanyId,
                company_name: document.getElementById('company-detail-name').value,
                opportunity_stage: document.getElementById('company-detail-opportunity-stage').value,
                company_linkedin: document.getElementById('company-detail-linkedin').value,
                location: document.getElementById('company-detail-location').value,
                role: document.getElementById('company-detail-role').value,
                stage: document.getElementById('company-detail-stage').value,
                employees: document.getElementById('company-detail-employees').value,
                raised_amount: document.getElementById('company-detail-raised').value,
                revenue: document.getElementById('company-detail-revenue').value,
                next_step: document.getElementById('company-detail-next-step').value,
                benefits: document.getElementById('company-detail-benefits').value,
                cash_comp: document.getElementById('company-detail-cash').value,
                base_salary: document.getElementById('company-detail-base').value,
                incentive: document.getElementById('company-detail-incentive').value,
                equity: document.getElementById('company-detail-equity').value,
                targeted_annual_comp: document.getElementById('company-detail-targeted').value,
                total_4year_comp: document.getElementById('company-detail-total4year').value
            };

            try {
                const response = await fetch('/api/update-grid-company', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();

                if (result.success) {
                    closeGridModal('companyDetailModal');
                    await loadOpportunityGrid();
                    showMessage('Company updated successfully!', 'success');
                } else {
                    showMessage('Error: ' + result.error, 'error');
                }
            } catch (error) {
                console.error('Error updating company:', error);
                showMessage('Error updating company', 'error');
            }
        }

        window.confirmDeleteGridCompany = async function() {
            if (!confirm('Delete this company from the grid? This will remove all scores.')) return;

            try {
                const response = await fetch('/api/delete-grid-company', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ grid_company_id: currentEditingCompanyId })
                });

                const result = await response.json();

                if (result.success) {
                    closeGridModal('companyDetailModal');
                    await loadOpportunityGrid();
                    showMessage('Company deleted', 'success');
                } else {
                    showMessage('Error deleting company', 'error');
                }
            } catch (error) {
                console.error('Error deleting company:', error);
                showMessage('Error deleting company', 'error');
            }
        }

        // ==================== COMPANIES PAGE ====================
        async function loadCompaniesPage() {
            try {
                const response = await fetch('/api/companies');
                const companies = await response.json();

                const container = document.getElementById('companies-list-container');

                if (companies.length === 0) {
                    container.innerHTML = '<div class="empty-state"><div class="empty-state-icon">🏢</div><p>No companies yet</p></div>';
                    return;
                }

                container.innerHTML = `
                    <div class="contacts-grid">
                        ${companies.map(company => `
                            <div class="contact-card">
                                <div class="contact-name">${company}</div>
                            </div>
                        `).join('')}
                    </div>
                `;
            } catch (error) {
                console.error('Error loading companies:', error);
                document.getElementById('companies-list-container').innerHTML = '<div class="empty-state">Error loading companies</div>';
            }
        }

        // ==================== UTILITY FUNCTIONS ====================
        function showMessage(msg, type) {
            const messageDiv = document.createElement('div');
            messageDiv.className = `message ${type}`;
            messageDiv.textContent = msg;
            messageDiv.style.cssText = 'position: fixed; top: 20px; right: 20px; z-index: 2000; padding: 15px 20px; border-radius: 5px;';
            document.body.appendChild(messageDiv);
            setTimeout(() => messageDiv.remove(), 3000);
        }

        window.onclick = function(event) {
            const editModal = document.getElementById('editModal');
            if (event.target === editModal) {
                closeEditModal();
            }
        }

        // ==================== OKR FUNCTIONS ====================
        let okrCategories = [];
        let okrGoalTypes = [];
        let currentQuarterYear = 2026;
        let currentQuarterNum = 1;

        async function loadAnnualOKRs() {
            try {
                // Load categories and goal types if not already loaded
                if (okrCategories.length === 0) {
                    const catResponse = await fetch('/api/okrs/categories');
                    okrCategories = await catResponse.json();
                }
                if (okrGoalTypes.length === 0) {
                    const goalResponse = await fetch('/api/okrs/goal-types');
                    okrGoalTypes = await goalResponse.json();
                }

                const response = await fetch('/api/okrs/annual');
                const okrs = await response.json();

                const container = document.getElementById('annual-okr-view');
                if (okrs.length === 0) {
                    container.innerHTML = '<div class="loading">No annual OKRs found. Click "+ Add OKR" to create one.</div>';
                    return;
                }

                let html = '<div class="okr-table"><table>';
                html += '<thead><tr>';
                html += '<th style="width: 80px;">Category</th>';
                html += '<th style="width: 40px;">P</th>';
                html += '<th style="width: 250px;">Objective</th>';
                html += '<th style="width: 250px;">Key Result(s)</th>';
                html += '<th style="width: 80px;">Done Date</th>';
                html += '<th style="width: 150px;">2025</th>';
                html += '<th style="width: 150px;">2026</th>';
                html += '<th style="width: 150px;">2027</th>';
                html += '<th style="width: 150px;">2028</th>';
                html += '<th style="width: 150px;">2029</th>';
                html += '<th style="width: 80px;">Actions</th>';
                html += '</tr></thead><tbody>';

                okrs.forEach(okr => {
                    html += '<tr>';
                    html += `<td><span class="category-tag">${okr.category}</span></td>`;
                    html += `<td><span class="okr-priority ${okr.priority.toLowerCase()}">${okr.priority}</span></td>`;
                    html += `<td>${okr.objective}</td>`;
                    html += `<td>${okr.key_result}</td>`;
                    html += `<td>${okr.done_date || ''}</td>`;

                    // Progress columns for years 2025-2029
                    for (let year = 2025; year <= 2029; year++) {
                        const progress = okr.annual_progress[year];
                        if (progress) {
                            const statusClass = getStatusClass(progress.status);
                            html += `<td class="progress-cell" onclick="editAnnualProgress(${okr.okr_id}, ${year})" style="cursor: pointer;">`;
                            html += `<span class="okr-status ${statusClass}">${progress.status}</span>`;
                            if (progress.note) {
                                html += `<div class="progress-note">${progress.note}</div>`;
                            }
                            html += '</td>';
                        } else {
                            html += `<td class="progress-cell" onclick="editAnnualProgress(${okr.okr_id}, ${year})" style="cursor: pointer; color: #999;">Click to add</td>`;
                        }
                    }

                    html += '<td><div class="okr-actions">';
                    html += `<button class="btn-edit" onclick="editOKR(${okr.okr_id})">Edit</button>`;
                    html += `<button class="btn-delete" onclick="deleteOKR(${okr.okr_id})">Delete</button>`;
                    html += '</div></td>';
                    html += '</tr>';
                });

                html += '</tbody></table></div>';
                container.innerHTML = html;
            } catch (error) {
                console.error('Error loading annual OKRs:', error);
                document.getElementById('annual-okr-view').innerHTML = '<div class="loading">Error loading OKRs</div>';
            }
        }

        async function loadQuarterlyOKRs(year, quarter) {
            try {
                currentQuarterYear = year;
                currentQuarterNum = quarter;

                // Update active quarter button
                document.querySelectorAll('.quarter-btn').forEach(btn => {
                    btn.classList.remove('active');
                    // Check if this button matches the year and quarter we're loading
                    const btnText = btn.textContent.trim();
                    if (btnText === `Q${quarter} ${year}`) {
                        btn.classList.add('active');
                    }
                });

                const response = await fetch(`/api/okrs/quarterly?year=${year}&quarter=${quarter}`);
                const okrs = await response.json();

                const container = document.getElementById('quarterly-okr-list');
                if (okrs.length === 0) {
                    container.innerHTML = '<div class="loading">No quarterly OKRs for this period. Click "+ Add OKR" to create one.</div>';
                    return;
                }

                // Determine the months for this quarter
                const quarterMonths = {
                    1: ['January', 'February', 'March'],
                    2: ['April', 'May', 'June'],
                    3: ['July', 'August', 'September'],
                    4: ['October', 'November', 'December']
                };
                const months = quarterMonths[quarter];

                let html = '<div class="okr-table"><table>';
                html += '<thead><tr>';
                html += '<th style="width: 80px;">Category</th>';
                html += '<th style="width: 40px;">P</th>';
                html += '<th style="width: 250px;">Objective</th>';
                html += '<th style="width: 250px;">Key Result(s)</th>';
                html += '<th style="width: 80px;">Done Date</th>';
                html += `<th style="width: 120px;">${months[0]}</th>`;
                html += `<th style="width: 120px;">${months[1]}</th>`;
                html += `<th style="width: 120px;">${months[2]}</th>`;
                html += '<th style="width: 120px;">Final</th>';
                html += '<th style="width: 80px;">Actions</th>';
                html += '</tr></thead><tbody>';

                okrs.forEach(okr => {
                    html += '<tr>';
                    html += `<td><span class="category-tag">${okr.category}</span></td>`;
                    html += `<td><span class="okr-priority ${okr.priority.toLowerCase()}">${okr.priority}</span></td>`;
                    html += `<td>${okr.objective}</td>`;
                    html += `<td>${okr.key_result}</td>`;
                    html += `<td>${okr.done_date || ''}</td>`;

                    // Monthly progress columns
                    for (let i = 0; i < 3; i++) {
                        const monthProgress = okr.progress[i];
                        if (monthProgress) {
                            const statusClass = getStatusClass(monthProgress.status);
                            html += `<td class="progress-cell">`;
                            html += `<span class="okr-status ${statusClass}">${monthProgress.status}</span>`;
                            if (monthProgress.percentage !== null) {
                                html += ` <small>(${monthProgress.percentage}%)</small>`;
                            }
                            if (monthProgress.note) {
                                html += `<div class="progress-note">${monthProgress.note}</div>`;
                            }
                            html += '</td>';
                        } else {
                            html += '<td style="color: #999;">-</td>';
                        }
                    }

                    // Final status column (current OKR status)
                    const statusClass = getStatusClass(okr.status);
                    html += `<td><span class="okr-status ${statusClass}">${okr.status}</span></td>`;

                    html += '<td><div class="okr-actions">';
                    html += `<button class="btn-progress" onclick="addQuarterlyProgress(${okr.okr_id})">Progress</button>`;
                    html += `<button class="btn-edit" onclick="editOKR(${okr.okr_id})">Edit</button>`;
                    html += '</div></td>';
                    html += '</tr>';
                });

                html += '</tbody></table></div>';
                container.innerHTML = html;
            } catch (error) {
                console.error('Error loading quarterly OKRs:', error);
                document.getElementById('quarterly-okr-list').innerHTML = '<div class="loading">Error loading OKRs</div>';
            }
        }

        function switchOKRView(view) {
            // Update tabs
            document.querySelectorAll('.okr-tab').forEach(tab => tab.classList.remove('active'));
            event.target.classList.add('active');

            // Toggle views
            if (view === 'annual') {
                document.getElementById('annual-okr-view').style.display = 'block';
                document.getElementById('quarterly-okr-view').style.display = 'none';
                loadAnnualOKRs();
            } else if (view === 'quarterly') {
                document.getElementById('annual-okr-view').style.display = 'none';
                document.getElementById('quarterly-okr-view').style.display = 'block';
                loadQuarterlyOKRs(currentQuarterYear, currentQuarterNum);
            }
        }

        function getStatusClass(status) {
            if (!status) return '';
            const statusMap = {
                'On Track': 'on-track',
                'Behind Schedule': 'behind',
                'At Risk': 'at-risk',
                'Completed': 'completed',
                'Not a Priority': 'not-priority'
            };
            return statusMap[status] || '';
        }

        async function openAddOKRModal() {
            // Reset form
            document.getElementById('okr-id').value = '';
            document.getElementById('okr-edit-mode').value = '';
            document.getElementById('okr-modal-title').textContent = '➕ Add New OKR';
            document.getElementById('okr-period-type').value = '';
            document.getElementById('okr-year').value = '';
            document.getElementById('okr-quarter').value = '';
            document.getElementById('okr-category').value = '';
            document.getElementById('okr-priority').value = '';
            document.getElementById('okr-objective').value = '';
            document.getElementById('okr-key-result').value = '';
            document.getElementById('okr-done-date').value = '';
            document.getElementById('okr-status').value = 'On Track';
            document.getElementById('okr-key-people').value = '';
            document.querySelectorAll('.goal-checkbox').forEach(cb => cb.checked = false);

            document.getElementById('period-fields').style.display = 'none';
            document.getElementById('quarter-field').style.display = 'none';

            // Always load categories to ensure dropdown is populated
            const response = await fetch('/api/okrs/categories');
            okrCategories = await response.json();
            const categorySelect = document.getElementById('okr-category');
            categorySelect.innerHTML = '<option value="">Select category...</option>';
            okrCategories.forEach(cat => {
                categorySelect.innerHTML += `<option value="${cat.id}">${cat.name}</option>`;
            });

            // Show modal
            document.getElementById('okrModal').style.display = 'block';
        }

        async function editOKR(okrId) {
            // Fetch the OKR directly by ID using the new efficient endpoint
            const response = await fetch(`/api/okrs/get?id=${okrId}`);
            const okr = await response.json();

            if (okr.error) {
                alert(`Error loading OKR: ${okr.error}`);
                return;
            }

            // Update current quarter variables if it's a quarterly OKR
            if (okr.period_type === 'quarterly' && okr.quarter) {
                currentQuarterYear = okr.year;
                currentQuarterNum = okr.quarter;
            }

            // Always load categories to ensure dropdown is populated
            const catResponse = await fetch('/api/okrs/categories');
            okrCategories = await catResponse.json();
            const categorySelect = document.getElementById('okr-category');
            categorySelect.innerHTML = '<option value="">Select category...</option>';
            okrCategories.forEach(cat => {
                categorySelect.innerHTML += `<option value="${cat.id}">${cat.name}</option>`;
            });

            // Populate form - use category_id directly from the API response
            document.getElementById('okr-id').value = okr.okr_id;
            document.getElementById('okr-edit-mode').value = '1';
            document.getElementById('okr-modal-title').textContent = '✏️ Edit OKR';
            document.getElementById('okr-period-type').value = okr.period_type || (okr.quarter ? 'quarterly' : 'annual');
            document.getElementById('okr-year').value = okr.year;
            document.getElementById('okr-quarter').value = okr.quarter || '';
            document.getElementById('okr-category').value = okr.category_id;
            document.getElementById('okr-priority').value = okr.priority;
            document.getElementById('okr-objective').value = okr.objective;
            document.getElementById('okr-key-result').value = okr.key_result;
            document.getElementById('okr-done-date').value = okr.done_date || '';
            document.getElementById('okr-status').value = okr.status;
            document.getElementById('okr-key-people').value = okr.key_people || '';

            // Set goal checkboxes
            if (okr.goal_type_ids) {
                const goalIds = okr.goal_type_ids.split(',');
                goalIds.forEach(id => {
                    const checkbox = document.querySelector(`.goal-checkbox[value="${id.trim()}"]`);
                    if (checkbox) checkbox.checked = true;
                });
            }

            // Show appropriate fields
            togglePeriodFields();

            // Show modal
            document.getElementById('okrModal').style.display = 'block';
        }

        function togglePeriodFields() {
            const periodType = document.getElementById('okr-period-type').value;
            const periodFields = document.getElementById('period-fields');
            const quarterField = document.getElementById('quarter-field');
            const yearLabel = document.getElementById('year-label');

            if (periodType === 'annual') {
                periodFields.style.display = 'flex';
                quarterField.style.display = 'none';
                yearLabel.textContent = 'Start Year *';
                document.getElementById('okr-quarter').removeAttribute('required');
            } else if (periodType === 'quarterly') {
                periodFields.style.display = 'flex';
                quarterField.style.display = 'block';
                yearLabel.textContent = 'Year *';
                document.getElementById('okr-quarter').setAttribute('required', 'required');
            } else {
                periodFields.style.display = 'none';
                quarterField.style.display = 'none';
            }
        }

        function closeOKRModal() {
            document.getElementById('okrModal').style.display = 'none';
        }

        async function saveOKR() {
            const okrId = document.getElementById('okr-id').value;
            const editMode = document.getElementById('okr-edit-mode').value;
            const periodType = document.getElementById('okr-period-type').value;

            // Collect goal type IDs
            const goalCheckboxes = document.querySelectorAll('.goal-checkbox:checked');
            const goalTypeIds = Array.from(goalCheckboxes).map(cb => cb.value).join(',');

            const okrData = {
                category_id: parseInt(document.getElementById('okr-category').value),
                priority: document.getElementById('okr-priority').value,
                objective: document.getElementById('okr-objective').value,
                key_result: document.getElementById('okr-key-result').value,
                done_date: document.getElementById('okr-done-date').value || null,
                period_type: periodType,
                year: parseInt(document.getElementById('okr-year').value),
                quarter: periodType === 'quarterly' ? parseInt(document.getElementById('okr-quarter').value) : null,
                status: document.getElementById('okr-status').value,
                key_people: document.getElementById('okr-key-people').value,
                goal_type_ids: goalTypeIds
            };

            const endpoint = editMode ? '/api/okrs/update' : '/api/okrs/add';
            if (editMode) {
                okrData.okr_id = parseInt(okrId);
            }

            try {
                const response = await fetch(endpoint, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(okrData)
                });

                const result = await response.json();

                if (result.success) {
                    closeOKRModal();
                    // Reload the appropriate view
                    if (periodType === 'annual') {
                        loadAnnualOKRs();
                    } else {
                        // Use the year and quarter from the form data, not global variables
                        const year = parseInt(document.getElementById('okr-year').value);
                        const quarter = parseInt(document.getElementById('okr-quarter').value);
                        // Update global variables
                        currentQuarterYear = year;
                        currentQuarterNum = quarter;
                        loadQuarterlyOKRs(year, quarter);
                    }
                    alert(editMode ? 'OKR updated successfully!' : 'OKR created successfully!');
                } else {
                    alert('Error saving OKR: ' + result.error);
                }
            } catch (error) {
                alert('Error saving OKR: ' + error.message);
            }
        }

        function deleteOKR(okrId) {
            if (confirm('Are you sure you want to delete this OKR?')) {
                fetch('/api/okrs/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ okr_id: okrId })
                })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        loadAnnualOKRs();
                    } else {
                        alert('Error deleting OKR: ' + data.error);
                    }
                });
            }
        }

        async function editAnnualProgress(okrId, year) {
            // Fetch the OKR details to show in the modal
            const response = await fetch(`/api/okrs/get?id=${okrId}`);
            const okr = await response.json();

            if (okr.error) {
                alert(`Error loading OKR: ${okr.error}`);
                return;
            }

            // Populate the modal
            document.getElementById('annual-progress-okr-id').value = okrId;
            document.getElementById('annual-progress-year').value = year;
            document.getElementById('annual-progress-okr-objective').textContent = okr.objective;
            document.getElementById('annual-progress-year-display').textContent = year;

            // Reset fields
            document.getElementById('annual-progress-status').value = '';
            document.getElementById('annual-progress-note').value = '';

            // Show modal
            document.getElementById('annualProgressModal').style.display = 'block';
        }

        function closeAnnualProgressModal() {
            document.getElementById('annualProgressModal').style.display = 'none';
        }

        async function saveAnnualProgress() {
            const okrId = document.getElementById('annual-progress-okr-id').value;
            const year = document.getElementById('annual-progress-year').value;
            const status = document.getElementById('annual-progress-status').value;
            const note = document.getElementById('annual-progress-note').value;

            try {
                const response = await fetch('/api/okrs/update-annual-progress', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        okr_id: parseInt(okrId),
                        year: parseInt(year),
                        status: status,
                        progress_note: note || ''
                    })
                });

                const data = await response.json();

                if (data.success) {
                    closeAnnualProgressModal();
                    loadAnnualOKRs();
                } else {
                    alert('Error updating progress: ' + data.error);
                }
            } catch (error) {
                alert('Error updating progress: ' + error.message);
            }
        }

        async function addQuarterlyProgress(okrId) {
            // Fetch the OKR details to show in the modal
            const response = await fetch(`/api/okrs/get?id=${okrId}`);
            const okr = await response.json();

            if (okr.error) {
                alert(`Error loading OKR: ${okr.error}`);
                return;
            }

            // Populate the modal
            document.getElementById('progress-okr-id').value = okrId;
            document.getElementById('progress-okr-objective').textContent = okr.objective;

            // Set default date to today
            const today = new Date().toISOString().split('T')[0];
            document.getElementById('progress-date').value = today;

            // Reset other fields
            document.getElementById('progress-status').value = '';
            document.getElementById('progress-percentage').value = '';
            document.getElementById('progress-note').value = '';

            // Show modal
            document.getElementById('progressModal').style.display = 'block';
        }

        function closeProgressModal() {
            document.getElementById('progressModal').style.display = 'none';
        }

        async function saveMonthlyProgress() {
            const okrId = document.getElementById('progress-okr-id').value;
            const progressData = {
                okr_id: parseInt(okrId),
                progress_date: document.getElementById('progress-date').value,
                status: document.getElementById('progress-status').value,
                progress_percentage: document.getElementById('progress-percentage').value ? parseInt(document.getElementById('progress-percentage').value) : null,
                progress_note: document.getElementById('progress-note').value
            };

            try {
                const response = await fetch('/api/okrs/add-progress', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(progressData)
                });

                const result = await response.json();

                if (result.success) {
                    alert('Progress saved successfully!');
                    closeProgressModal();
                    // Reload the quarterly view
                    loadQuarterlyOKRs(currentQuarterYear, currentQuarterNum);
                } else {
                    alert(`Error: ${result.error || 'Failed to save progress'}`);
                }
            } catch (error) {
                alert(`Error saving progress: ${error.message}`);
            }
        }

        // ==================== INITIALIZATION ====================
        console.log('About to initialize...');
        // Check if DOM is already loaded (since script is at end of body)
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => {
                console.log('DOMContentLoaded fired!');
                // Check if there was a pending navigation from early clicks
                if (window._pendingNavigation) {
                    console.log('Processing pending navigation to:', window._pendingNavigation);
                    navigateTo(window._pendingNavigation);
                    delete window._pendingNavigation;
                } else {
                    console.log('Calling loadDashboard...');
                    loadDashboard();
                }
            });
        } else {
            // DOM is already loaded
            console.log('DOM already loaded!');
            // Check if there was a pending navigation from early clicks
            if (window._pendingNavigation) {
                console.log('Processing pending navigation to:', window._pendingNavigation);
                navigateTo(window._pendingNavigation);
                delete window._pendingNavigation;
            } else {
                console.log('Calling loadDashboard...');
                loadDashboard();
            }
        }
        console.log('Initialization complete');

        // ==================== GTM AGENT HUB ====================
        let gtmHistory = JSON.parse(localStorage.getItem('gtm-hub-history') || '[]');
        let gtmModalMeeting = '';
        let gtmModalType = 'prep';
        let gtmPageInited = false;

        function gtmInitPage() {
            if (!gtmPageInited) {
                gtmUpdateClock();
                setInterval(gtmUpdateClock, 30000);
                if (localStorage.getItem('gtm-hub-banner') === 'dismissed') {
                    const b = document.getElementById('gtm-how-banner');
                    if (b) b.style.display = 'none';
                }
                gtmUpdateStats();
                gtmRenderHistory();
                gtmPageInited = true;
            }
        }

        function gtmUpdateClock() {
            const n = new Date();
            const el = document.getElementById('gtm-clock');
            if (el) el.textContent = n.toLocaleDateString('en-US',{weekday:'short',month:'short',day:'numeric'}) + ' · ' + n.toLocaleTimeString('en-US',{hour:'numeric',minute:'2-digit'});
        }

        window.gtmDismissBanner = function() {
            const b = document.getElementById('gtm-how-banner');
            if (b) b.style.display = 'none';
            localStorage.setItem('gtm-hub-banner','dismissed');
        };

        window.gtmSwitchView = function(v, btn) {
            document.querySelectorAll('.gtm-view').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.gtm-nav-tab').forEach(el => el.classList.remove('active'));
            document.getElementById('gtm-view-'+v).classList.add('active');
            btn.classList.add('active');
        };

        window.gtmCopyPrompt = function(btn, type, meeting, attendees, time) {
            let text = type === 'prep'
                ? 'Run meeting prep for "' + meeting + '" with ' + attendees + ' at ' + time + '.\\n\\nSearch Gmail for recent email threads with these attendees, check Google Calendar for additional meeting context, and search Granola for past meeting transcripts.\\n\\nReturn:\\n- Attendee context (who they are and their relevance)\\n- Email thread summary (recent relevant threads)\\n- Past meeting history\\n- 4–6 suggested talking points'
                : 'Run post-meeting follow-up for "' + meeting + '" with ' + attendees + '.\\n\\nPull the Granola transcript for this meeting and any relevant Gmail threads.\\n\\nReturn:\\n- Meeting summary (3–4 sentences)\\n- Action items with owners (numbered list)\\n- Draft follow-up email (subject line + body)\\n- CRM update: deal stage, key signals, recommended next steps';
            const orig = btn.innerHTML;
            navigator.clipboard.writeText(text).then(() => {
                btn.innerHTML = '✓ Copied!'; btn.classList.add('copied');
                gtmShowToast('Prompt copied — paste into Cowork');
                setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('copied'); }, 2200);
            }).catch(() => {
                const ta = document.createElement('textarea');
                ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
                document.body.appendChild(ta); ta.select(); document.execCommand('copy');
                document.body.removeChild(ta);
                btn.innerHTML = '✓ Copied!'; btn.classList.add('copied');
                gtmShowToast('Prompt copied — paste into Cowork');
                setTimeout(() => { btn.innerHTML = orig; btn.classList.remove('copied'); }, 2200);
            });
        };

        window.gtmShowToast = function(msg) {
            const t = document.getElementById('gtm-toast');
            if (!t) return;
            t.textContent = msg; t.classList.add('show');
            setTimeout(() => t.classList.remove('show'), 2500);
        };

        window.gtmOpenLog = function(meeting) {
            gtmModalMeeting = meeting;
            document.getElementById('gtm-modal-title').textContent = 'Log result — ' + meeting;
            document.getElementById('gtm-modal-text').value = '';
            gtmSetType('prep');
            document.getElementById('gtm-modal-overlay').classList.add('open');
            setTimeout(() => document.getElementById('gtm-modal-text').focus(), 200);
        };

        window.gtmCloseModal = function() {
            document.getElementById('gtm-modal-overlay').classList.remove('open');
        };

        window.gtmHandleOverlayClick = function(e) {
            if (e.target === document.getElementById('gtm-modal-overlay')) gtmCloseModal();
        };

        window.gtmSetType = function(t) {
            gtmModalType = t;
            document.getElementById('gtm-type-prep').classList.toggle('active', t === 'prep');
            document.getElementById('gtm-type-followup').classList.toggle('active', t === 'followup');
        };

        window.gtmSaveResult = function() {
            const text = document.getElementById('gtm-modal-text').value.trim();
            if (!text) { gtmShowToast('Please paste a result first'); return; }
            const entry = { id: Date.now(), meeting: gtmModalMeeting, type: gtmModalType, text, timestamp: new Date().toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) };
            gtmHistory.unshift(entry);
            localStorage.setItem('gtm-hub-history', JSON.stringify(gtmHistory));
            gtmCloseModal();
            gtmUpdateStats();
            gtmRenderHistory();
            gtmShowToast('Result saved to history');
        };

        function gtmUpdateStats() {
            const el = document.getElementById('gtm-hist-count');
            if (el) el.textContent = gtmHistory.length;
            const t = document.getElementById('gtm-stat-total'); if (t) t.textContent = gtmHistory.length;
            const p = document.getElementById('gtm-stat-prep'); if (p) p.textContent = gtmHistory.filter(h => h.type==='prep').length;
            const f = document.getElementById('gtm-stat-fu'); if (f) f.textContent = gtmHistory.filter(h => h.type==='followup').length;
        }

        function gtmRenderHistory() {
            const el = document.getElementById('gtm-hist-list');
            if (!el) return;
            if (!gtmHistory.length) {
                el.innerHTML = '<div class="gtm-hist-empty">No results logged yet.<br>Run an agent in Cowork and click "Log result" to archive outputs here.</div>';
                return;
            }
            el.innerHTML = gtmHistory.map(h => '<div class="gtm-hist-card" onclick="gtmShowHistEntry(' + h.id + ')"><div class="gtm-hist-meta"><span class="gtm-hist-title">' + h.meeting + '</span><span class="gtm-hist-chip ' + (h.type==='prep'?'chip-prep':'chip-followup') + '">' + (h.type==='prep'?'prep':'follow-up') + '</span><span class="gtm-hist-time">' + h.timestamp + '</span></div><div class="gtm-hist-preview">' + h.text.slice(0,140) + '…</div></div>').join('');
        }

        window.gtmClearHistDetail = function() {
            document.getElementById('gtm-hist-detail').innerHTML = '';
        };

        window.gtmShowHistEntry = function(id) {
            const h = gtmHistory.find(x => x.id===id); if (!h) return;
            const el = document.getElementById('gtm-hist-detail');
            el.innerHTML = '<div class="gtm-result-panel"><div class="gtm-rp-head"><span class="gtm-rp-title">' + h.meeting + ' — ' + (h.type==='prep'?'prep':'follow-up') + ' · ' + h.timestamp + '</span><button class="gtm-rp-close" onclick="gtmClearHistDetail()">×</button></div><div class="gtm-rp-content">' + h.text + '</div></div>';
            el.scrollIntoView({behavior:'smooth',block:'nearest'});
        };

    </script>
</body>
</html>
        """
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Expires', '0')
        self.end_headers()
        self.wfile.write(html.encode())

    def serve_logo(self):
        """Serve the logo image"""
        import os
        logo_path = os.path.join(os.path.dirname(__file__), 'static', 'logo.png')
        try:
            with open(logo_path, 'rb') as f:
                self.send_response(200)
                self.send_header('Content-type', 'image/png')
                self.end_headers()
                self.wfile.write(f.read())
        except FileNotFoundError:
            self.send_error(404, 'Logo not found')

    def send_json_response(self, data):
        """Send JSON response"""
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def get_all_contacts(self):
        """Get all contacts with relationships"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM v_contacts_with_relationships ORDER BY person_name")
        contacts = [dict(row) for row in cursor.fetchall()]

        conn.close()
        self.send_json_response(contacts)

    def get_companies(self):
        """Get all company names"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT company_id, company_name as name FROM companies ORDER BY company_name")
        companies = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json_response({'companies': companies})

    def get_relationship_types(self):
        """Get all relationship types"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT relationship_type_id as type_id, relationship_name as name FROM relationship_types ORDER BY relationship_name")
        types = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json_response({'types': types})

    def search_contacts(self):
        """Search contacts"""
        query_components = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(query_components.query)
        search_term = params.get('q', [''])[0]

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM v_contacts_with_relationships
            WHERE person_name LIKE ? OR company_name LIKE ? OR email LIKE ?
            ORDER BY person_name
        """, (f'%{search_term}%', f'%{search_term}%', f'%{search_term}%'))

        contacts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json_response(contacts)

    def get_contacts_by_relationship(self):
        """Get contacts filtered by relationship type"""
        query_components = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(query_components.query)
        rel_type = params.get('type', [''])[0]

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute("""
            SELECT c.*, co.company_name, rt.relationship_name as relationship_types
            FROM contacts c
            JOIN contact_relationships cr ON c.contact_id = cr.contact_id
            JOIN relationship_types rt ON cr.relationship_type_id = rt.relationship_type_id
            LEFT JOIN companies co ON c.company_id = co.company_id
            WHERE rt.relationship_name = ?
            ORDER BY c.person_name
        """, (rel_type,))

        contacts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json_response(contacts)

    def get_statistics(self):
        """Get database statistics"""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM contacts")
        total_contacts = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM companies")
        total_companies = cursor.fetchone()[0]

        cursor.execute("""
            SELECT COUNT(DISTINCT cr.contact_id)
            FROM contact_relationships cr
            JOIN relationship_types rt ON cr.relationship_type_id = rt.relationship_type_id
            WHERE rt.relationship_name = 'recruiter'
        """)
        recruiters = cursor.fetchone()[0]

        conn.close()

        stats = {
            'total_contacts': total_contacts,
            'total_companies': total_companies,
            'recruiters': recruiters
        }

        self.send_json_response(stats)

    def add_contact_from_extension(self, data):
        """Add a new contact from Chrome extension"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Handle company - either existing or new
            company_id = None
            if data.get('company_id'):
                company_id = data['company_id']
            elif data.get('new_company'):
                # Create new company
                cursor.execute("INSERT INTO companies (company_name) VALUES (?)",
                             (data['new_company'],))
                company_id = cursor.lastrowid

            if not company_id:
                raise ValueError("Company is required")

            # Build full name from first and last name
            first_name = data.get('first_name', '')
            last_name = data.get('last_name', '')
            person_name = f"{first_name} {last_name}".strip() if last_name else first_name

            # Add contact
            cursor.execute("""
                INSERT INTO contacts (person_name, first_name, last_name, company_id, role, email, phone_number, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (person_name, first_name, last_name, company_id, data.get('title'), data.get('email'),
                  data.get('phone'), data.get('notes')))

            contact_id = cursor.lastrowid

            # Add relationship
            if data.get('relationship_type'):
                cursor.execute("""
                    INSERT INTO contact_relationships (contact_id, relationship_type_id)
                    VALUES (?, ?)
                """, (contact_id, data['relationship_type']))

            # Add LinkedIn URL to notes if provided
            if data.get('linkedin_url'):
                linkedin_note = f"LinkedIn: {data['linkedin_url']}"
                if data.get('notes'):
                    cursor.execute("""
                        UPDATE contacts SET notes = notes || '\n' || ?
                        WHERE contact_id = ?
                    """, (linkedin_note, contact_id))
                else:
                    cursor.execute("""
                        UPDATE contacts SET notes = ?
                        WHERE contact_id = ?
                    """, (linkedin_note, contact_id))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True, 'contact_id': contact_id})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def add_contact(self, data):
        """Add a new contact"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get or create company
            cursor.execute("SELECT company_id FROM companies WHERE company_name = ?",
                          (data['company_name'],))
            result = cursor.fetchone()

            if result:
                company_id = result[0]
            else:
                cursor.execute("INSERT INTO companies (company_name) VALUES (?)",
                             (data['company_name'],))
                company_id = cursor.lastrowid

            # Add contact
            cursor.execute("""
                INSERT INTO contacts (person_name, company_id, role, email, phone_number, opportunity, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (data['person_name'], company_id, data.get('role'), data.get('email'),
                  data.get('phone'), data.get('opportunity'), data.get('notes')))

            contact_id = cursor.lastrowid

            # Add relationships
            for rel_type in data.get('relationships', []):
                cursor.execute("""
                    INSERT INTO contact_relationships (contact_id, relationship_type_id)
                    SELECT ?, relationship_type_id
                    FROM relationship_types
                    WHERE relationship_name = ?
                """, (contact_id, rel_type))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True, 'contact_id': contact_id})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def add_relationship(self, data):
        """Add a relationship to a contact"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO contact_relationships (contact_id, relationship_type_id)
                SELECT
                    (SELECT contact_id FROM contacts WHERE person_name = ?),
                    (SELECT relationship_type_id FROM relationship_types WHERE relationship_name = ?)
            """, (data['person_name'], data['relationship_type']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def update_contact(self, data):
        """Update a contact"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Build dynamic update query based on provided fields
            update_fields = []
            update_values = []

            if 'role' in data and data.get('role') is not None:
                update_fields.append('role = ?')
                update_values.append(data.get('role'))

            if 'email' in data and data.get('email') is not None:
                update_fields.append('email = ?')
                update_values.append(data.get('email'))

            if 'phone' in data and data.get('phone') is not None:
                update_fields.append('phone_number = ?')
                update_values.append(data.get('phone'))

            # Handle company - either by ID or by name (create if doesn't exist)
            if 'company_name' in data and data.get('company_name'):
                company_name = data.get('company_name')
                # Check if company exists
                cursor.execute("SELECT company_id FROM companies WHERE company_name = ?", (company_name,))
                result = cursor.fetchone()

                if result:
                    company_id = result[0]
                else:
                    # Create new company
                    cursor.execute("INSERT INTO companies (company_name) VALUES (?)", (company_name,))
                    company_id = cursor.lastrowid

                update_fields.append('company_id = ?')
                update_values.append(company_id)
            elif 'company_id' in data and data.get('company_id') is not None:
                update_fields.append('company_id = ?')
                update_values.append(data.get('company_id'))

            if 'linkedin_url' in data and data.get('linkedin_url') is not None:
                update_fields.append('linkedin_url = ?')
                update_values.append(data.get('linkedin_url'))

            if 'opportunity' in data and data.get('opportunity') is not None:
                update_fields.append('opportunity = ?')
                update_values.append(data.get('opportunity'))

            if 'notes' in data and data.get('notes') is not None:
                update_fields.append('notes = ?')
                update_values.append(data.get('notes'))

            if 'last_connection' in data and data.get('last_connection') is not None:
                update_fields.append('last_connection = ?')
                update_values.append(data.get('last_connection'))

            if 'follow_up' in data and data.get('follow_up') is not None:
                update_fields.append('follow_up = ?')
                update_values.append(data.get('follow_up'))

            # Add person_name for WHERE clause
            update_values.append(data['person_name'])

            if update_fields:
                query = f"UPDATE contacts SET {', '.join(update_fields)} WHERE person_name = ?"
                cursor.execute(query, update_values)

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def append_note(self, data):
        """Append a timestamped note to a contact"""
        try:
            from datetime import datetime
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get current notes
            cursor.execute("SELECT notes FROM contacts WHERE person_name = ?",
                          (data['person_name'],))
            result = cursor.fetchone()

            current_notes = result[0] if result and result[0] else ""

            # Create timestamped note
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            new_note = f"[{timestamp}] {data['note']}"

            # Append to existing notes
            if current_notes:
                updated_notes = f"{current_notes}\n{new_note}"
            else:
                updated_notes = new_note

            # Update contact
            cursor.execute("""
                UPDATE contacts
                SET notes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE person_name = ?
            """, (updated_notes, data['person_name']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True, 'notes': updated_notes})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def get_all_followups(self):
        """Get all follow-up tasks"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    ft.task_id,
                    ft.task_title,
                    ft.task_description,
                    ft.due_date,
                    ft.priority,
                    ft.completed,
                    ft.google_calendar_event_id,
                    c.person_name,
                    c.email AS contact_email,
                    co.company_name,
                    ft.status,
                    ft.section,
                    ft.assignee
                FROM followup_tasks ft
                LEFT JOIN contacts c ON ft.contact_id = c.contact_id
                LEFT JOIN companies co ON ft.company_id = co.company_id
                ORDER BY ft.due_date ASC
            """)

            rows = cursor.fetchall()
            tasks = []
            for row in rows:
                tasks.append({
                    'task_id': row[0],
                    'task_title': row[1],
                    'task_description': row[2],
                    'due_date': row[3],
                    'priority': row[4],
                    'completed': bool(row[5]),
                    'google_calendar_event_id': row[6],
                    'person_name': row[7],
                    'contact_email': row[8],
                    'company_name': row[9],
                    'status': row[10] or 'on_track',
                    'section': row[11] or ('done' if row[5] else 'todo'),
                    'assignee': row[12]
                })

            conn.close()
            self.send_json_response(tasks)

        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_active_followups(self):
        """Get active (incomplete) follow-up tasks"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM v_active_followup_tasks")

            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            tasks = []
            for row in rows:
                task = dict(zip(columns, row))
                tasks.append(task)

            conn.close()
            self.send_json_response(tasks)

        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_completed_followups(self):
        """Get completed follow-up tasks"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM v_completed_followup_tasks")

            rows = cursor.fetchall()
            columns = [description[0] for description in cursor.description]
            tasks = []
            for row in rows:
                task = dict(zip(columns, row))
                tasks.append(task)

            conn.close()
            self.send_json_response(tasks)

        except Exception as e:
            self.send_json_response({'error': str(e)})

    def add_followup(self, data):
        """Add a new follow-up task with Google Calendar integration"""
        try:
            from datetime import datetime
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get contact/company info for calendar event
            contact_name = None
            company_name = None
            contact_email = None

            if data.get('contact_name'):
                cursor.execute("""
                    SELECT c.contact_id, c.person_name, c.email, co.company_name
                    FROM contacts c
                    LEFT JOIN companies co ON c.company_id = co.company_id
                    WHERE c.person_name = ?
                """, (data['contact_name'],))
                result = cursor.fetchone()
                if result:
                    contact_id = result[0]
                    contact_name = result[1]
                    contact_email = result[2]
                    company_name = result[3]
                else:
                    contact_id = None
            else:
                contact_id = None

            if data.get('company_name') and not contact_id:
                cursor.execute("""
                    SELECT company_id, company_name FROM companies WHERE company_name = ?
                """, (data['company_name'],))
                result = cursor.fetchone()
                if result:
                    company_id = result[0]
                    company_name = result[1]
                else:
                    company_id = None
            else:
                company_id = None

            # Create Google Calendar event and Google Task
            google_event_id = None
            google_task_id = None
            try:
                from google_calendar import get_calendar_service
                calendar = get_calendar_service()
                result = calendar.create_event(
                    task_title=data['task_title'],
                    task_description=data.get('task_description', ''),
                    due_date=data['due_date'],
                    contact_name=contact_name,
                    company_name=company_name,
                    contact_email=contact_email,
                    priority=data.get('priority', 'medium')
                )
                if result:
                    google_event_id = result.get('event_id')
                    google_task_id = result.get('task_id')
            except Exception as cal_error:
                print(f"Calendar/Task error (continuing without): {cal_error}")

            # Insert task into database
            cursor.execute("""
                INSERT INTO followup_tasks
                (task_title, task_description, due_date, priority, contact_id, company_id,
                 google_calendar_event_id, google_task_id, status, section, assignee)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['task_title'],
                data.get('task_description'),
                data.get('due_date'),
                data.get('priority', 'medium'),
                contact_id,
                company_id,
                google_event_id,
                google_task_id,
                data.get('status', 'on_track'),
                data.get('section', 'todo'),
                data.get('assignee')
            ))

            task_id = cursor.lastrowid
            conn.commit()
            conn.close()

            self.send_json_response({
                'success': True,
                'task_id': task_id,
                'google_event_id': google_event_id
            })

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def update_followup(self, data):
        """Update a follow-up task and sync with Google Calendar"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get current task info
            cursor.execute("""
                SELECT ft.google_calendar_event_id, c.person_name, c.email, co.company_name
                FROM followup_tasks ft
                LEFT JOIN contacts c ON ft.contact_id = c.contact_id
                LEFT JOIN companies co ON ft.company_id = co.company_id
                WHERE ft.task_id = ?
            """, (data['task_id'],))

            result = cursor.fetchone()
            if result:
                google_event_id = result[0]
                contact_name = result[1]
                contact_email = result[2]
                company_name = result[3]

                # Update Google Calendar event if it exists
                if google_event_id:
                    try:
                        from google_calendar import get_calendar_service
                        calendar = get_calendar_service()
                        calendar.update_event(
                            event_id=google_event_id,
                            task_title=data.get('task_title'),
                            task_description=data.get('task_description'),
                            due_date=data.get('due_date'),
                            contact_name=contact_name,
                            company_name=company_name,
                            contact_email=contact_email,
                            priority=data.get('priority')
                        )
                    except Exception as cal_error:
                        print(f"Calendar update error: {cal_error}")

            # Update database
            cursor.execute("""
                UPDATE followup_tasks
                SET task_title = ?, task_description = ?, due_date = ?, priority = ?,
                    status = ?, section = ?, assignee = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
            """, (
                data.get('task_title'),
                data.get('task_description'),
                data.get('due_date'),
                data.get('priority'),
                data.get('status'),
                data.get('section'),
                data.get('assignee'),
                data['task_id']
            ))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def complete_followup(self, data):
        """Mark a follow-up task as complete, delete calendar event, and mark Google Task as complete"""
        try:
            from datetime import datetime
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get Google Calendar event ID and Google Task ID
            cursor.execute("""
                SELECT google_calendar_event_id, google_task_id FROM followup_tasks WHERE task_id = ?
            """, (data['task_id'],))

            result = cursor.fetchone()
            if result:
                google_event_id = result[0]
                google_task_id = result[1]

                try:
                    from google_calendar import get_calendar_service
                    calendar = get_calendar_service()

                    # Delete calendar event (15-minute meeting)
                    if google_event_id:
                        calendar.delete_event(google_event_id, None)

                    # Mark Google Task as completed (keeps it in task list as done)
                    if google_task_id:
                        calendar.complete_task(google_task_id)

                except Exception as cal_error:
                    print(f"Calendar/Task update error: {cal_error}")

            # Mark as completed in database
            cursor.execute("""
                UPDATE followup_tasks
                SET completed = 1, completed_at = ?,
                    section = 'done',
                    google_calendar_event_id = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = ?
            """, (datetime.now().isoformat(), data['task_id']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def delete_followup(self, data):
        """Delete a follow-up task and remove from Google Calendar and Tasks"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get Google Calendar event ID and Google Task ID
            cursor.execute("""
                SELECT google_calendar_event_id, google_task_id FROM followup_tasks WHERE task_id = ?
            """, (data['task_id'],))

            result = cursor.fetchone()
            if result:
                google_event_id = result[0]
                google_task_id = result[1]

                # Delete from both Google Calendar and Google Tasks
                try:
                    from google_calendar import get_calendar_service
                    calendar = get_calendar_service()
                    calendar.delete_event(google_event_id, google_task_id)
                except Exception as cal_error:
                    print(f"Calendar/Task delete error: {cal_error}")

            # Delete from database
            cursor.execute("DELETE FROM followup_tasks WHERE task_id = ?", (data['task_id'],))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    # Opportunity Grid API Methods
    def get_opportunity_grid(self):
        """Get complete opportunity grid data"""
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Get grid info (use first grid for now)
            cursor.execute("SELECT * FROM opportunity_grids LIMIT 1")
            grid_row = cursor.fetchone()

            if not grid_row:
                self.send_json_response({'companies': [], 'criteria': [], 'scores': {}})
                return

            grid = dict(grid_row)
            grid_id = grid['grid_id']

            # Get companies
            cursor.execute("""
                SELECT * FROM grid_companies
                WHERE grid_id = ?
                ORDER BY column_order, grid_company_id
            """, (grid_id,))
            companies = [dict(row) for row in cursor.fetchall()]

            # Get criteria
            cursor.execute("""
                SELECT * FROM grid_criteria
                WHERE grid_id = ?
                ORDER BY row_order, criteria_id
            """, (grid_id,))
            criteria = [dict(row) for row in cursor.fetchall()]

            # Get scores
            cursor.execute("""
                SELECT gs.*, gc.grid_company_id, gcr.criteria_id
                FROM grid_scores gs
                JOIN grid_companies gc ON gs.grid_company_id = gc.grid_company_id
                JOIN grid_criteria gcr ON gs.criteria_id = gcr.criteria_id
                WHERE gc.grid_id = ?
            """, (grid_id,))
            scores_raw = cursor.fetchall()

            # Build scores dict: {company_id: {criteria_id: numeric_score}}
            scores = {}
            for score in scores_raw:
                score_dict = dict(score)
                company_id = score_dict['grid_company_id']
                criteria_id = score_dict['criteria_id']
                if company_id not in scores:
                    scores[company_id] = {}
                # Use numeric_score if set, fall back to has_check*max for legacy rows
                scores[company_id][criteria_id] = score_dict['numeric_score']

            conn.close()

            self.send_json_response({
                'grid': grid,
                'companies': companies,
                'criteria': criteria,
                'scores': scores
            })

        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_opportunity_detail(self):
        """Get detailed information for a single opportunity"""
        try:
            # Extract company_id from path: /api/opportunity/{id}
            path_parts = self.path.split('/')
            company_id = int(path_parts[-1])

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT * FROM grid_companies WHERE grid_company_id = ?", (company_id,))
            row = cursor.fetchone()

            if not row:
                conn.close()
                self.send_json_response({'error': 'Opportunity not found'})
                return

            opportunity = dict(row)
            conn.close()
            self.send_json_response(opportunity)

        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_grid_companies(self):
        """Get all companies in grid"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM grid_companies WHERE grid_id = 1 ORDER BY column_order")
        companies = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json_response({'companies': companies})

    def get_grid_criteria(self):
        """Get all criteria in grid"""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM grid_criteria WHERE grid_id = 1 ORDER BY row_order")
        criteria = [dict(row) for row in cursor.fetchall()]
        conn.close()
        self.send_json_response({'criteria': criteria})

    def add_grid_company(self, data):
        """Add a new company to the grid"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO grid_companies (
                    grid_id, company_name, company_linkedin, location, role,
                    stage, raised_amount, employees, revenue, next_step, benefits,
                    cash_comp, base_salary, incentive, equity,
                    targeted_annual_comp, total_4year_comp, column_order, opportunity_stage
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                1,  # grid_id
                data['company_name'],
                data.get('company_linkedin'),
                data.get('location'),
                data.get('role'),
                data.get('stage'),
                data.get('raised_amount'),
                data.get('employees'),
                data.get('revenue'),
                data.get('next_step'),
                data.get('benefits'),
                data.get('cash_comp'),
                data.get('base_salary'),
                data.get('incentive'),
                data.get('equity'),
                data.get('targeted_annual_comp'),
                data.get('total_4year_comp'),
                data.get('column_order', 0),
                data.get('opportunity_stage', 'Inquiry')
            ))

            company_id = cursor.lastrowid

            # Create empty scores for all criteria
            cursor.execute("SELECT criteria_id FROM grid_criteria WHERE grid_id = 1")
            criteria_ids = [row[0] for row in cursor.fetchall()]

            for criteria_id in criteria_ids:
                cursor.execute("""
                    INSERT INTO grid_scores (grid_company_id, criteria_id, has_check)
                    VALUES (?, ?, 0)
                """, (company_id, criteria_id))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True, 'company_id': company_id})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def update_grid_company(self, data):
        """Update company details"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE grid_companies SET
                    company_name = ?, company_linkedin = ?, location = ?, role = ?,
                    stage = ?, raised_amount = ?, employees = ?, revenue = ?,
                    next_step = ?, benefits = ?, cash_comp = ?, base_salary = ?,
                    incentive = ?, equity = ?, targeted_annual_comp = ?, total_4year_comp = ?,
                    opportunity_stage = ?, funding_details = ?, employee_linkedin_url = ?, core_solution = ?
                WHERE grid_company_id = ?
            """, (
                data['company_name'], data.get('company_linkedin'), data.get('location'),
                data.get('role'), data.get('stage'), data.get('raised_amount'),
                data.get('employees'), data.get('revenue'), data.get('next_step'),
                data.get('benefits'), data.get('cash_comp'), data.get('base_salary'),
                data.get('incentive'), data.get('equity'), data.get('targeted_annual_comp'),
                data.get('total_4year_comp'), data.get('opportunity_stage', 'Inquiry'),
                data.get('funding_details'), data.get('employee_linkedin_url'), data.get('core_solution'),
                data['grid_company_id']
            ))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def delete_grid_company(self, data):
        """Delete a company from grid"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM grid_companies WHERE grid_company_id = ?",
                         (data['grid_company_id'],))
            conn.commit()
            conn.close()
            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def add_grid_criteria(self, data):
        """Add new scoring criteria"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO grid_criteria (grid_id, criteria_name, max_score, row_order, category)
                VALUES (?, ?, ?, ?, ?)
            """, (1, data['criteria_name'], data.get('max_score', 10),
                  data.get('row_order', 0), data.get('category', 'Personal Fit')))

            criteria_id = cursor.lastrowid

            # Create scores for all companies
            cursor.execute("SELECT grid_company_id FROM grid_companies WHERE grid_id = 1")
            company_ids = [row[0] for row in cursor.fetchall()]

            for company_id in company_ids:
                cursor.execute("""
                    INSERT INTO grid_scores (grid_company_id, criteria_id, has_check)
                    VALUES (?, ?, 0)
                """, (company_id, criteria_id))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True, 'criteria_id': criteria_id})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def update_grid_criteria(self, data):
        """Update criteria name or max score"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE grid_criteria SET criteria_name = ?, max_score = ?
                WHERE criteria_id = ?
            """, (data['criteria_name'], data['max_score'], data['criteria_id']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def delete_grid_criteria(self, data):
        """Delete a criteria row"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM grid_criteria WHERE criteria_id = ?",
                         (data['criteria_id'],))
            conn.commit()
            conn.close()
            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def toggle_grid_score(self, data):
        """Toggle checkbox score for company/criteria"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Check if score exists
            cursor.execute("""
                SELECT score_id, has_check FROM grid_scores
                WHERE grid_company_id = ? AND criteria_id = ?
            """, (data['grid_company_id'], data['criteria_id']))

            result = cursor.fetchone()

            if result:
                # Toggle existing score
                score_id, current_check = result
                new_check = 0 if current_check else 1
                cursor.execute("""
                    UPDATE grid_scores SET has_check = ? WHERE score_id = ?
                """, (new_check, score_id))
            else:
                # Create new score
                cursor.execute("""
                    INSERT INTO grid_scores (grid_company_id, criteria_id, has_check)
                    VALUES (?, ?, 1)
                """, (data['grid_company_id'], data['criteria_id']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})

        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def set_grid_score(self, data):
        """Set a numeric score for company/criteria"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            company_id = data['grid_company_id']
            criteria_id = data['criteria_id']
            value = data.get('numeric_score')  # None clears the score

            cursor.execute("""
                SELECT score_id FROM grid_scores
                WHERE grid_company_id = ? AND criteria_id = ?
            """, (company_id, criteria_id))
            result = cursor.fetchone()

            if result:
                cursor.execute("""
                    UPDATE grid_scores SET numeric_score = ? WHERE score_id = ?
                """, (value, result[0]))
            else:
                cursor.execute("""
                    INSERT INTO grid_scores (grid_company_id, criteria_id, has_check, numeric_score)
                    VALUES (?, ?, 0, ?)
                """, (company_id, criteria_id, value))

            conn.commit()
            conn.close()
            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    # ==================== OKR API METHODS ====================

    def get_okr_categories(self):
        """Get all OKR categories"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT category_id, category_name FROM okr_categories ORDER BY category_name")
            categories = [{'id': row[0], 'name': row[1]} for row in cursor.fetchall()]
            conn.close()
            self.send_json_response(categories)
        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_okr_goal_types(self):
        """Get all OKR goal types"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT goal_type_id, goal_number, goal_name FROM okr_goal_types ORDER BY goal_number")
            goal_types = [{'id': row[0], 'number': row[1], 'name': row[2]} for row in cursor.fetchall()]
            conn.close()
            self.send_json_response(goal_types)
        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_annual_okrs(self):
        """Get all annual OKRs with progress by year"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get annual OKRs
            cursor.execute("""
                SELECT
                    o.okr_id, o.priority, c.category_name, o.objective,
                    o.key_result, o.done_date, o.year, o.status,
                    o.key_people, o.goal_type_ids
                FROM okrs o
                JOIN okr_categories c ON o.category_id = c.category_id
                WHERE o.period_type = 'annual'
                ORDER BY o.priority, c.category_name, o.objective
            """)

            okrs = []
            for row in cursor.fetchall():
                okr = {
                    'okr_id': row[0],
                    'priority': row[1],
                    'category': row[2],
                    'objective': row[3],
                    'key_result': row[4],
                    'done_date': row[5],
                    'year': row[6],
                    'status': row[7],
                    'key_people': row[8],
                    'goal_type_ids': row[9],
                    'annual_progress': {}
                }

                # Get annual progress for this OKR
                cursor.execute("""
                    SELECT year, progress_note, status
                    FROM okr_annual_progress
                    WHERE okr_id = ?
                    ORDER BY year
                """, (okr['okr_id'],))

                for prog_row in cursor.fetchall():
                    okr['annual_progress'][prog_row[0]] = {
                        'note': prog_row[1],
                        'status': prog_row[2]
                    }

                okrs.append(okr)

            conn.close()
            self.send_json_response(okrs)
        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_quarterly_okrs(self):
        """Get quarterly OKRs for a specific quarter"""
        try:
            # Parse query parameters
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            year = int(query.get('year', [2026])[0])
            quarter = int(query.get('quarter', [1])[0])

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get quarterly OKRs
            cursor.execute("""
                SELECT
                    o.okr_id, o.priority, c.category_name, o.objective,
                    o.key_result, o.done_date, o.status,
                    o.key_people, o.goal_type_ids
                FROM okrs o
                JOIN okr_categories c ON o.category_id = c.category_id
                WHERE o.period_type = 'quarterly' AND o.year = ? AND o.quarter = ?
                ORDER BY o.priority, c.category_name, o.objective
            """, (year, quarter))

            okrs = []
            for row in cursor.fetchall():
                okr = {
                    'okr_id': row[0],
                    'priority': row[1],
                    'category': row[2],
                    'objective': row[3],
                    'key_result': row[4],
                    'done_date': row[5],
                    'status': row[6],
                    'key_people': row[7],
                    'goal_type_ids': row[8],
                    'progress': []
                }

                # Get monthly progress for this OKR
                cursor.execute("""
                    SELECT progress_date, progress_percentage, progress_note, status
                    FROM okr_progress
                    WHERE okr_id = ?
                    ORDER BY progress_date
                """, (okr['okr_id'],))

                for prog_row in cursor.fetchall():
                    okr['progress'].append({
                        'date': prog_row[0],
                        'percentage': prog_row[1],
                        'note': prog_row[2],
                        'status': prog_row[3]
                    })

                okrs.append(okr)

            conn.close()
            self.send_json_response(okrs)
        except Exception as e:
            self.send_json_response({'error': str(e)})

    def get_single_okr(self):
        """Get a single OKR by ID"""
        try:
            # Parse query parameters
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            okr_id = int(query.get('id', [0])[0])

            if not okr_id:
                self.send_json_response({'error': 'Missing OKR ID'})
                return

            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Get the OKR with category name
            cursor.execute("""
                SELECT
                    o.okr_id, o.category_id, c.category_name, o.priority, o.objective,
                    o.key_result, o.done_date, o.period_type, o.year, o.quarter,
                    o.status, o.key_people, o.goal_type_ids
                FROM okrs o
                JOIN okr_categories c ON o.category_id = c.category_id
                WHERE o.okr_id = ?
            """, (okr_id,))

            row = cursor.fetchone()
            if not row:
                conn.close()
                self.send_json_response({'error': 'OKR not found'})
                return

            okr = {
                'okr_id': row[0],
                'category_id': row[1],
                'category': row[2],
                'priority': row[3],
                'objective': row[4],
                'key_result': row[5],
                'done_date': row[6],
                'period_type': row[7],
                'year': row[8],
                'quarter': row[9],
                'status': row[10],
                'key_people': row[11],
                'goal_type_ids': row[12]
            }

            # Get progress based on period type
            if okr['period_type'] == 'annual':
                okr['annual_progress'] = {}
                cursor.execute("""
                    SELECT year, progress_note, status
                    FROM okr_annual_progress
                    WHERE okr_id = ?
                    ORDER BY year
                """, (okr_id,))

                for prog_row in cursor.fetchall():
                    okr['annual_progress'][prog_row[0]] = {
                        'note': prog_row[1],
                        'status': prog_row[2]
                    }
            else:  # quarterly
                okr['progress'] = []
                cursor.execute("""
                    SELECT progress_date, progress_percentage, progress_note, status
                    FROM okr_progress
                    WHERE okr_id = ?
                    ORDER BY progress_date
                """, (okr_id,))

                for prog_row in cursor.fetchall():
                    okr['progress'].append({
                        'date': prog_row[0],
                        'percentage': prog_row[1],
                        'note': prog_row[2],
                        'status': prog_row[3]
                    })

            conn.close()
            self.send_json_response(okr)
        except Exception as e:
            self.send_json_response({'error': str(e)})

    def add_okr(self, data):
        """Add a new OKR"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                INSERT INTO okrs (
                    category_id, priority, objective, key_result, done_date,
                    period_type, year, quarter, status, key_people, goal_type_ids
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                data['category_id'], data['priority'], data['objective'],
                data['key_result'], data.get('done_date'), data['period_type'],
                data['year'], data.get('quarter'), data.get('status', 'On Track'),
                data.get('key_people', ''), data.get('goal_type_ids', '')
            ))

            okr_id = cursor.lastrowid
            conn.commit()
            conn.close()

            self.send_json_response({'success': True, 'okr_id': okr_id})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def update_okr(self, data):
        """Update an existing OKR"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            cursor.execute("""
                UPDATE okrs SET
                    category_id = ?, priority = ?, objective = ?, key_result = ?,
                    done_date = ?, status = ?, key_people = ?, goal_type_ids = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE okr_id = ?
            """, (
                data['category_id'], data['priority'], data['objective'],
                data['key_result'], data.get('done_date'), data['status'],
                data.get('key_people', ''), data.get('goal_type_ids', ''),
                data['okr_id']
            ))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def delete_okr(self, data):
        """Delete an OKR"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM okrs WHERE okr_id = ?", (data['okr_id'],))
            conn.commit()
            conn.close()
            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def add_okr_progress(self, data):
        """Add monthly progress update for a quarterly OKR"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Check if progress entry exists for this date
            cursor.execute("""
                SELECT progress_id FROM okr_progress
                WHERE okr_id = ? AND progress_date = ?
            """, (data['okr_id'], data['progress_date']))

            existing = cursor.fetchone()

            if existing:
                # Update existing progress
                cursor.execute("""
                    UPDATE okr_progress SET
                        progress_percentage = ?, progress_note = ?, status = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE progress_id = ?
                """, (data['progress_percentage'], data.get('progress_note', ''),
                      data['status'], existing[0]))
            else:
                # Insert new progress
                cursor.execute("""
                    INSERT INTO okr_progress (
                        okr_id, progress_date, progress_percentage, progress_note, status
                    ) VALUES (?, ?, ?, ?, ?)
                """, (data['okr_id'], data['progress_date'], data['progress_percentage'],
                      data.get('progress_note', ''), data['status']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})

    def update_annual_progress(self, data):
        """Update annual progress for a multi-year OKR"""
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()

            # Check if annual progress exists
            cursor.execute("""
                SELECT annual_progress_id FROM okr_annual_progress
                WHERE okr_id = ? AND year = ?
            """, (data['okr_id'], data['year']))

            existing = cursor.fetchone()

            if existing:
                # Update existing
                cursor.execute("""
                    UPDATE okr_annual_progress SET
                        progress_note = ?, status = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE annual_progress_id = ?
                """, (data.get('progress_note', ''), data['status'], existing[0]))
            else:
                # Insert new
                cursor.execute("""
                    INSERT INTO okr_annual_progress (okr_id, year, progress_note, status)
                    VALUES (?, ?, ?, ?)
                """, (data['okr_id'], data['year'], data.get('progress_note', ''), data['status']))

            conn.commit()
            conn.close()

            self.send_json_response({'success': True})
        except Exception as e:
            self.send_json_response({'success': False, 'error': str(e)})


def create_scheduled_todo(title, description, due_date, priority='medium'):
    """Create a scheduled to-do task"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if this exact todo already exists for today
        cursor.execute("""
            SELECT task_id FROM followups
            WHERE task_title = ?
            AND date(created_at) = date('now')
            AND status = 'pending'
        """, (title,))

        if cursor.fetchone():
            conn.close()
            return  # Already created today

        cursor.execute("""
            INSERT INTO followups (task_title, task_description, due_date, priority, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', datetime('now'))
        """, (title, description, due_date, priority))

        conn.commit()
        conn.close()
        print(f"✓ Created scheduled to-do: {title}")
    except Exception as e:
        print(f"Error creating scheduled to-do: {e}")


def check_scheduled_tasks():
    """Check and create scheduled OKR to-dos based on date"""
    today = datetime.now()
    month = today.month
    day = today.day

    # 1. December 15: Annual OKR tasks
    if month == 12 and day == 15:
        # Update Annual OKRs
        create_scheduled_todo(
            title="Update Annual OKRs",
            description="Review and update progress on all annual OKRs for the current year.",
            due_date=(today + timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S'),
            priority='medium'
        )

        # Draft Next Year OKRs
        create_scheduled_todo(
            title="Draft Next Year OKRs",
            description=f"Create and plan OKRs for {today.year + 1}. Review goals and set priorities for the upcoming year.",
            due_date=(today + timedelta(days=14)).strftime('%Y-%m-%d %H:%M:%S'),
            priority='medium'
        )

    # 2. First of each month: Update Quarterly OKRs
    if day == 1:
        quarter = (month - 1) // 3 + 1
        create_scheduled_todo(
            title="Update Quarterly OKRs",
            description=f"Update progress on Q{quarter} OKRs. Review status and add monthly progress notes.",
            due_date=(today + timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S'),
            priority='medium'
        )

    # 3. 10 days before end of quarter: Draft next quarter's OKRs
    # March 20, June 20, September 20, December 20
    if (month == 3 and day == 20) or (month == 6 and day == 20) or \
       (month == 9 and day == 20) or (month == 12 and day == 20):
        current_quarter = (month - 1) // 3 + 1
        next_quarter = current_quarter + 1 if current_quarter < 4 else 1
        next_year = today.year if next_quarter > current_quarter else today.year + 1

        create_scheduled_todo(
            title="Draft Next Quarter's OKRs",
            description=f"Plan and create OKRs for Q{next_quarter} {next_year}. Set objectives and key results for the upcoming quarter.",
            due_date=(today + timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S'),
            priority='medium'
        )


def scheduled_task_runner():
    """Background thread that checks for scheduled tasks daily"""
    last_check_date = None

    while True:
        try:
            current_date = datetime.now().date()

            # Only run once per day
            if current_date != last_check_date:
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Checking for scheduled tasks...")
                check_scheduled_tasks()
                last_check_date = current_date

            # Sleep for 1 hour before checking again
            time.sleep(3600)
        except Exception as e:
            print(f"Error in scheduled task runner: {e}")
            time.sleep(3600)


def main():
    """Start the web server"""
    Handler = DatabaseHandler

    # Start the scheduled task runner in a background thread
    scheduler_thread = threading.Thread(target=scheduled_task_runner, daemon=True)
    scheduler_thread.start()

    class DualStackServer(socketserver.TCPServer):
        address_family = socket.AF_INET6
        allow_reuse_address = True

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    # Run database migration before starting server
    migrate_database()

    with DualStackServer(("::", PORT), Handler) as httpd:
        print("\n" + "="*60)
        print("🚀 Wetzel CRM - Your Network Hub")
        print("="*60)
        print(f"\nServer running at: http://localhost:{PORT}")
        print("\nOpen your browser and navigate to the URL above")
        print("📅 Scheduled OKR task checker: ACTIVE")
        print("\nPress Ctrl+C to stop the server")
        print("="*60 + "\n")

        # Run initial check on startup
        print("Running initial scheduled task check...")
        check_scheduled_tasks()

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n\nServer stopped.")


if __name__ == "__main__":
    main()
