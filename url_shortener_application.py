import http.server
import json
import os
import re
import socketserver
import sqlite3
import string
import sys
import urllib.parse
from datetime import datetime, timedelta

# Render dynamically injects PORT (often 10000). Always bind to 0.0.0.0
HOST = os.environ.get("HOST", "0.0.0.0")
raw_port = os.environ.get("PORT", "10000")
try:
    PORT = int(raw_port)
except ValueError:
    PORT = 10000

# Persistent database path (or local sqlite file)
DATABASE_FILE = os.environ.get("DATABASE_PATH", "url_shortener.db")

# Optional custom public domain (e.g., https://my-shortener.onrender.com)
CUSTOM_BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")

BASE62_ALPHABET = string.digits + string.ascii_letters  # 0-9, a-z, A-Z (length 62)
BASE_LEN = len(BASE62_ALPHABET)

def get_db_connection():
    """Establish a thread-safe connection to SQLite with WAL mode."""
    db_dir = os.path.dirname(DATABASE_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DATABASE_FILE, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
    except Exception:
        pass
    return conn

def init_db():
    """Create tables and apply non-destructive column migrations."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_url TEXT NOT NULL,
                short_code TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                clicks INTEGER DEFAULT 0,
                expires_at TIMESTAMP NULL,
                max_clicks INTEGER NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS click_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                short_code TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_agent TEXT,
                referer TEXT,
                ip_address TEXT
            )
        """)
        
        # Non-destructive migrations for existing SQLite databases
        cursor.execute("PRAGMA table_info(urls);")
        columns = [row[1] for row in cursor.fetchall()]
        if "expires_at" not in columns:
            cursor.execute("ALTER TABLE urls ADD COLUMN expires_at TIMESTAMP NULL;")
        if "max_clicks" not in columns:
            cursor.execute("ALTER TABLE urls ADD COLUMN max_clicks INTEGER NULL;")

        conn.commit()
    finally:
        conn.close()

def encode_base62(num: int) -> str:
    """Encode auto-incrementing integer to base62 string."""
    if num == 0:
        return BASE62_ALPHABET[0]
    encoded = []
    while num > 0:
        num, rem = divmod(num, BASE_LEN)
        encoded.append(BASE62_ALPHABET[rem])
    return "".join(reversed(encoded))

def is_valid_url(url: str) -> bool:
    """Validate HTTP/HTTPS URL pattern."""
    regex = re.compile(
        r'^(https?://)'
        r'([a-zA-Z0-9.-]+(\.[a-zA-Z]{2,}))'
        r'(:\d+)?'
        r'(/.*)?$', re.IGNORECASE
    )
    return bool(re.match(regex, url.strip()))

def is_valid_custom_code(code: str) -> bool:
    """Validate alias format (alphanumeric, dashes, underscores, 3-30 chars)."""
    reserved = {"api", "healthz", "favicon.ico", "robots.txt", "static", "qr"}
    if code.lower() in reserved:
        return False
    return bool(re.match(r'^[a-zA-Z0-9_-]{3,30}$', code))

def shorten_url(original_url: str, base_url: str, custom_code: str = None, expiry_hours: int = None, max_clicks: int = None) -> tuple:
    """Insert or retrieve shortened link record with expiration parameters."""
    original_url = original_url.strip()
    if not (original_url.startswith("http://") or original_url.startswith("https://")):
        original_url = "https://" + original_url

    if not is_valid_url(original_url):
        return {"error": "Invalid URL. Please provide a complete address including domain (e.g. example.com)."}, 400

    expires_at = None
    if expiry_hours and expiry_hours > 0:
        expires_at = (datetime.utcnow() + timedelta(hours=expiry_hours)).strftime("%Y-%m-%d %H:%M:%S")

    max_clicks_val = None
    if max_clicks and max_clicks > 0:
        max_clicks_val = int(max_clicks)

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if custom_code:
            custom_code = custom_code.strip()
            if not is_valid_custom_code(custom_code):
                return {"error": "Custom alias must be 3-30 characters (letters, numbers, '-', '_') and not a reserved word."}, 400
            
            cursor.execute("SELECT id FROM urls WHERE short_code = ?", (custom_code,))
            if cursor.fetchone():
                return {"error": f"The short code '{custom_code}' is already taken. Please pick another."}, 409
            
            cursor.execute(
                "INSERT INTO urls (original_url, short_code, expires_at, max_clicks) VALUES (?, ?, ?, ?)",
                (original_url, custom_code, expires_at, max_clicks_val)
            )
            conn.commit()
            record_id = cursor.lastrowid
            code = custom_code
        else:
            # When expiration rules or click limits are used, always create a unique distinct shortened entry
            if expiry_hours or max_clicks_val:
                cursor.execute(
                    "INSERT INTO urls (original_url, short_code, expires_at, max_clicks) VALUES (?, '', ?, ?)",
                    (original_url, expires_at, max_clicks_val)
                )
                record_id = cursor.lastrowid
                code = encode_base62(record_id + 10000)
                cursor.execute("UPDATE urls SET short_code = ? WHERE id = ?", (code, record_id))
                conn.commit()
            else:
                cursor.execute(
                    "SELECT id, short_code, created_at, clicks, expires_at, max_clicks FROM urls WHERE original_url = ? AND expires_at IS NULL AND max_clicks IS NULL ORDER BY id ASC LIMIT 1",
                    (original_url,)
                )
                existing = cursor.fetchone()
                if existing:
                    return {
                        "id": existing["id"],
                        "original_url": original_url,
                        "short_code": existing["short_code"],
                        "short_url": f"{base_url}/{existing['short_code']}",
                        "created_at": existing["created_at"],
                        "clicks": existing["clicks"],
                        "expires_at": existing["expires_at"],
                        "max_clicks": existing["max_clicks"],
                        "message": "Existing shortened URL found."
                    }, 200

                cursor.execute("INSERT INTO urls (original_url, short_code, expires_at, max_clicks) VALUES (?, '', NULL, NULL)", (original_url,))
                record_id = cursor.lastrowid
                code = encode_base62(record_id + 10000)
                cursor.execute("UPDATE urls SET short_code = ? WHERE id = ?", (code, record_id))
                conn.commit()

        cursor.execute("SELECT id, original_url, short_code, created_at, clicks, expires_at, max_clicks FROM urls WHERE id = ?", (record_id,))
        new_row = cursor.fetchone()

        return {
            "id": new_row["id"],
            "original_url": new_row["original_url"],
            "short_code": new_row["short_code"],
            "short_url": f"{base_url}/{new_row['short_code']}",
            "created_at": new_row["created_at"],
            "clicks": new_row["clicks"],
            "expires_at": new_row["expires_at"],
            "max_clicks": new_row["max_clicks"]
        }, 201
    except Exception as e:
        conn.rollback()
        return {"error": f"Database error: {str(e)}"}, 500
    finally:
        conn.close()

def record_click_and_validate(short_code: str, user_agent: str, referer: str, ip_address: str):
    """Check existence, expiration timestamp, click quota, then record analytics."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, original_url, clicks, expires_at, max_clicks FROM urls WHERE short_code = ?", (short_code,))
        row = cursor.fetchone()
        if not row:
            return {"status": "not_found", "url": None}

        # Check time-based expiration
        if row["expires_at"]:
            try:
                expires_dt = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
                if datetime.utcnow() > expires_dt:
                    return {"status": "expired_time", "expires_at": row["expires_at"], "url": None}
            except Exception:
                pass

        # Check max click quota
        if row["max_clicks"] is not None and row["clicks"] >= row["max_clicks"]:
            return {"status": "expired_clicks", "max_clicks": row["max_clicks"], "url": None}

        # Record valid visit
        cursor.execute("UPDATE urls SET clicks = clicks + 1 WHERE short_code = ?", (short_code,))
        cursor.execute(
            "INSERT INTO click_events (short_code, user_agent, referer, ip_address) VALUES (?, ?, ?, ?)",
            (short_code, user_agent or "Unknown", referer or "Direct", ip_address or "Unknown")
        )
        conn.commit()
        return {"status": "valid", "url": row["original_url"]}
    finally:
        conn.close()

def fetch_all_links(base_url: str):
    """Fetch all shortened URLs with status computed."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, original_url, short_code, created_at, clicks, expires_at, max_clicks FROM urls ORDER BY id DESC")
        rows = cursor.fetchall()
        now_dt = datetime.utcnow()
        links = []
        for r in rows:
            link = dict(r)
            link["short_url"] = f"{base_url}/{link['short_code']}"
            
            # Check expired state
            is_expired = False
            expiry_reason = None
            if link.get("expires_at"):
                try:
                    exp = datetime.strptime(link["expires_at"], "%Y-%m-%d %H:%M:%S")
                    if now_dt > exp:
                        is_expired = True
                        expiry_reason = "Time expired"
                except Exception:
                    pass
            if link.get("max_clicks") is not None and link["clicks"] >= link["max_clicks"]:
                is_expired = True
                expiry_reason = f"Max clicks reached ({link['clicks']}/{link['max_clicks']})"

            link["is_expired"] = is_expired
            link["expiry_reason"] = expiry_reason
            links.append(link)
        return links
    finally:
        conn.close()

def fetch_link_stats(short_code: str, base_url: str):
    """Fetch individual link analytics."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT id, original_url, short_code, created_at, clicks, expires_at, max_clicks FROM urls WHERE short_code = ?", (short_code,))
        link_info = cursor.fetchone()
        if not link_info:
            return None

        cursor.execute(
            "SELECT timestamp, user_agent, referer, ip_address FROM click_events WHERE short_code = ? ORDER BY id DESC LIMIT 20",
            (short_code,)
        )
        events = [dict(row) for row in cursor.fetchall()]
        data = dict(link_info)
        data["short_url"] = f"{base_url}/{data['short_code']}"
        data["recent_clicks"] = events
        return data
    finally:
        conn.close()

FRONTEND_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>ShortLink - Fast URL Shortener & QR Generator</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <!-- QRCode.js for client-side instant QR rendering -->
  <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style> body { font-family: 'Inter', sans-serif; } </style>
</head>
<body class="bg-slate-50 text-slate-800 min-h-screen flex flex-col antialiased">
  <header class="bg-white border-b border-slate-200 sticky top-0 z-20">
    <div class="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
      <div class="flex items-center space-x-3">
        <div class="w-10 h-10 rounded-xl bg-indigo-600 flex items-center justify-center text-white shadow-md shadow-indigo-100">
          <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1"></path></svg>
        </div>
        <div>
          <h1 class="text-lg font-bold text-slate-900 leading-tight">ShortLink</h1>
          <p class="text-xs text-slate-500 font-medium">Cloud Shortener with QR Code & Self-Destruct</p>
        </div>
      </div>
      <div class="flex items-center space-x-3">
        <span class="inline-flex items-center px-2.5 py-1 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200">
          <span class="w-1.5 h-1.5 mr-1.5 bg-emerald-500 rounded-full animate-pulse"></span> Production Ready
        </span>
      </div>
    </div>
  </header>

  <main class="flex-grow max-w-6xl w-full mx-auto px-4 py-8 space-y-8">
    <section class="bg-white rounded-2xl shadow-sm border border-slate-200/80 p-6 md:p-8">
      <h2 class="text-xl font-bold text-slate-900 mb-1">Shorten a URL & Generate QR Code</h2>
      <p class="text-sm text-slate-500 mb-6">Create branded short links with optional expiration limits and instant QR codes for sharing.</p>

      <form id="shortenForm" class="space-y-4">
        <div>
          <label class="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Destination URL *</label>
          <input type="text" id="originalUrl" placeholder="https://example.com/very/long/nested/path/to/resource" required class="w-full px-4 py-3 rounded-xl border border-slate-300 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition text-slate-900 placeholder:text-slate-400 text-sm">
        </div>

        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
          <div>
            <label class="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Custom Alias (Optional)</label>
            <div class="flex items-center">
              <span class="inline-flex items-center px-3 py-3 rounded-l-xl border border-r-0 border-slate-300 bg-slate-50 text-slate-500 text-sm font-mono">/</span>
              <input type="text" id="customCode" placeholder="my-cool-link" class="w-full px-4 py-3 rounded-r-xl border border-slate-300 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition text-slate-900 placeholder:text-slate-400 text-sm">
            </div>
          </div>

          <div>
            <label class="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Link Expiration</label>
            <select id="expiryHours" class="w-full px-4 py-3 rounded-xl border border-slate-300 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition text-slate-900 text-sm bg-white">
              <option value="0">Never (Permanent)</option>
              <option value="1">Expire in 1 Hour</option>
              <option value="24">Expire in 24 Hours</option>
              <option value="168">Expire in 7 Days</option>
              <option value="720">Expire in 30 Days</option>
            </select>
          </div>

          <div>
            <label class="block text-xs font-semibold text-slate-600 uppercase tracking-wider mb-2">Max Clicks (Burn Limit)</label>
            <input type="number" id="maxClicks" min="1" placeholder="e.g. 5 (Leave empty for unlimited)" class="w-full px-4 py-3 rounded-xl border border-slate-300 focus:ring-2 focus:ring-indigo-500 focus:border-indigo-500 outline-none transition text-slate-900 placeholder:text-slate-400 text-sm">
          </div>
        </div>

        <div class="pt-2">
          <button type="submit" id="submitBtn" class="w-full md:w-auto py-3 px-8 rounded-xl font-medium text-sm bg-indigo-600 hover:bg-indigo-700 text-white shadow-lg shadow-indigo-600/20 active:scale-[0.99] transition flex items-center justify-center space-x-2 cursor-pointer">
            <span>Shorten & Generate QR</span>
            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M14 5l7 7m0 0l-7 7m7-7H3"></path></svg>
          </button>
        </div>
      </form>
      <div id="feedbackBanner" class="hidden mt-6 p-4 rounded-xl border text-sm flex items-start justify-between"></div>
    </section>

    <section class="bg-white rounded-2xl shadow-sm border border-slate-200/80 overflow-hidden">
      <div class="px-6 py-4 border-b border-slate-200 flex items-center justify-between">
        <div>
          <h3 class="font-bold text-slate-900 text-base">All Shortened Links</h3>
          <p class="text-xs text-slate-500">Live monitoring, QR generation & expiration state</p>
        </div>
        <button onclick="loadLinks()" class="text-xs font-medium text-indigo-600 hover:text-indigo-800 flex items-center space-x-1 cursor-pointer">
          <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"></path></svg>
          <span>Refresh</span>
        </button>
      </div>

      <div class="overflow-x-auto">
        <table class="w-full text-left text-sm text-slate-600">
          <thead class="bg-slate-50 text-slate-500 uppercase text-[11px] font-semibold tracking-wider">
            <tr>
              <th scope="col" class="px-6 py-3">Short URL</th>
              <th scope="col" class="px-6 py-3">Destination</th>
              <th scope="col" class="px-6 py-3 text-center">Clicks / Limit</th>
              <th scope="col" class="px-6 py-3">Status / Expiry</th>
              <th scope="col" class="px-6 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody id="linksTableBody" class="divide-y divide-slate-100">
            <tr>
              <td colspan="5" class="px-6 py-8 text-center text-slate-400">Loading links...</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </main>

  <!-- QR Code Modal -->
  <div id="qrModal" class="hidden fixed inset-0 bg-slate-900/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl max-w-sm w-full p-6 shadow-2xl border border-slate-200 flex flex-col items-center text-center">
      <div class="flex items-center justify-between w-full pb-3 border-b border-slate-100">
        <h4 class="font-bold text-slate-900 text-base">QR Code</h4>
        <button onclick="closeQrModal()" class="text-slate-400 hover:text-slate-600 rounded-lg p-1.5 cursor-pointer">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>
      
      <div class="py-6 flex flex-col items-center">
        <div id="qrCodeContainer" class="p-3 bg-white border border-slate-200 rounded-xl shadow-sm"></div>
        <p id="qrTargetUrl" class="mt-4 text-xs font-mono text-indigo-600 font-medium break-all px-2"></p>
      </div>

      <div class="w-full flex space-x-2 pt-2 border-t border-slate-100">
        <button onclick="downloadQrCode()" class="flex-1 py-2.5 px-3 text-xs font-semibold bg-indigo-600 hover:bg-indigo-700 text-white rounded-xl shadow cursor-pointer transition">
          Download PNG
        </button>
        <button onclick="closeQrModal()" class="px-4 py-2.5 text-xs font-medium bg-slate-100 hover:bg-slate-200 text-slate-700 rounded-xl cursor-pointer transition">
          Close
        </button>
      </div>
    </div>
  </div>

  <!-- Stats Modal -->
  <div id="statsModal" class="hidden fixed inset-0 bg-slate-900/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
    <div class="bg-white rounded-2xl max-w-xl w-full p-6 shadow-2xl border border-slate-200 max-h-[85vh] flex flex-col">
      <div class="flex items-center justify-between pb-3 border-b border-slate-100">
        <h4 class="font-bold text-slate-900 text-lg">Link Analytics</h4>
        <button onclick="closeStatsModal()" class="text-slate-400 hover:text-slate-600 rounded-lg p-1.5 cursor-pointer">
          <svg class="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12"></path></svg>
        </button>
      </div>
      <div id="statsModalBody" class="py-4 overflow-y-auto space-y-4 flex-grow"></div>
      <div class="pt-3 border-t border-slate-100 flex justify-end">
        <button onclick="closeStatsModal()" class="px-4 py-2 text-sm font-medium bg-slate-100 text-slate-700 rounded-xl hover:bg-slate-200 cursor-pointer">Close</button>
      </div>
    </div>
  </div>

  <script>
    const form = document.getElementById('shortenForm');
    const feedbackBanner = document.getElementById('feedbackBanner');
    const linksTableBody = document.getElementById('linksTableBody');
    let activeQrInstance = null;
    let currentShortUrl = "";

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      feedbackBanner.className = "hidden";
      const original_url = document.getElementById('originalUrl').value;
      const custom_code = document.getElementById('customCode').value;
      const expiry_hours = parseInt(document.getElementById('expiryHours').value, 10);
      const max_clicks_val = document.getElementById('maxClicks').value;
      const max_clicks = max_clicks_val ? parseInt(max_clicks_val, 10) : null;

      try {
        const response = await fetch('/api/shorten', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ original_url, custom_code, expiry_hours, max_clicks })
        });
        const result = await response.json();

        if (response.ok) {
          showFeedback(`Link created: <a href="${result.short_url}" target="_blank" class="font-bold underline ml-1 text-emerald-800">${result.short_url}</a> 
            <button onclick="openQrModal('${result.short_url}')" class="ml-3 inline-flex items-center px-2.5 py-1 text-xs bg-emerald-600 text-white rounded-lg hover:bg-emerald-700">View QR</button>`, 'success');
          form.reset();
          loadLinks();
        } else {
          showFeedback(result.error || 'Failed to shorten URL', 'error');
        }
      } catch (err) {
        showFeedback('A network connection error occurred.', 'error');
      }
    });

    function showFeedback(message, type) {
      feedbackBanner.innerHTML = `<div>${message}</div>`;
      feedbackBanner.className = type === 'success' 
        ? "mt-6 p-4 rounded-xl border bg-emerald-50 border-emerald-200 text-emerald-800 flex items-center justify-between"
        : "mt-6 p-4 rounded-xl border bg-rose-50 border-rose-200 text-rose-800 flex items-center justify-between";
    }

    async function loadLinks() {
      try {
        const res = await fetch('/api/links');
        const links = await res.json();
        
        if (!links.length) {
          linksTableBody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-slate-400">No shortened links yet. Create one above!</td></tr>`;
          return;
        }

        linksTableBody.innerHTML = links.map(link => {
          let statusBadge = `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-emerald-50 text-emerald-700">Active</span>`;
          if (link.is_expired) {
            statusBadge = `<span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-rose-50 text-rose-700" title="${escapeHtml(link.expiry_reason)}">Expired</span>`;
          }

          let expiryText = link.expires_at ? `<div class="text-[11px] text-slate-400 mt-0.5">Exp: ${link.expires_at}</div>` : `<div class="text-[11px] text-slate-400 mt-0.5">Never expires</div>`;
          let clicksText = link.max_clicks ? `${link.clicks} / ${link.max_clicks}` : `${link.clicks}`;

          return `
            <tr class="hover:bg-slate-50/80 transition-colors">
              <td class="px-6 py-4 font-semibold text-indigo-600">
                <a href="${link.short_url}" target="_blank" class="hover:underline flex items-center gap-1.5 font-mono text-xs md:text-sm">
                  <span>/${link.short_code}</span>
                  <svg class="w-3.5 h-3.5 text-slate-400" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"></path></svg>
                </a>
              </td>
              <td class="px-6 py-4">
                <div class="max-w-xs md:max-w-md truncate text-slate-700" title="${escapeHtml(link.original_url)}">
                  ${escapeHtml(link.original_url)}
                </div>
              </td>
              <td class="px-6 py-4 text-center">
                <span class="inline-flex items-center px-2 py-0.5 rounded-full text-xs font-semibold bg-indigo-50 text-indigo-700">
                  ${clicksText}
                </span>
              </td>
              <td class="px-6 py-4 text-xs">
                ${statusBadge}
                ${expiryText}
              </td>
              <td class="px-6 py-4 text-right space-x-1 whitespace-nowrap">
                <button onclick="openQrModal('${link.short_url}')" class="text-xs px-2.5 py-1.5 rounded-lg border border-slate-200 hover:bg-slate-100 text-slate-700 cursor-pointer font-medium transition" title="Show QR Code">
                  QR
                </button>
                <button onclick="copyToClipboard('${link.short_url}', this)" class="text-xs px-2.5 py-1.5 rounded-lg border border-slate-200 hover:bg-slate-100 text-slate-700 cursor-pointer font-medium transition">
                  Copy
                </button>
                <button onclick="viewStats('${link.short_code}')" class="text-xs px-2.5 py-1.5 rounded-lg bg-indigo-50 hover:bg-indigo-100 text-indigo-700 cursor-pointer font-medium transition">
                  Stats
                </button>
              </td>
            </tr>
          `;
        }).join('');
      } catch (err) {
        linksTableBody.innerHTML = `<tr><td colspan="5" class="px-6 py-8 text-center text-rose-500">Failed to fetch links.</td></tr>`;
      }
    }

    function openQrModal(url) {
      currentShortUrl = url;
      const modal = document.getElementById('qrModal');
      const container = document.getElementById('qrCodeContainer');
      const targetLabel = document.getElementById('qrTargetUrl');
      
      container.innerHTML = "";
      targetLabel.innerText = url;
      
      activeQrInstance = new QRCode(container, {
        text: url,
        width: 180,
        height: 180,
        colorDark : "#0f172a",
        colorLight : "#ffffff",
        correctLevel : QRCode.CorrectLevel.H
      });
      
      modal.classList.remove('hidden');
    }

    function closeQrModal() {
      document.getElementById('qrModal').classList.add('hidden');
    }

    function downloadQrCode() {
      const container = document.getElementById('qrCodeContainer');
      const img = container.querySelector('img');
      const canvas = container.querySelector('canvas');
      let dataUrl = img ? img.src : (canvas ? canvas.toDataURL("image/png") : null);

      if (dataUrl) {
        const link = document.createElement('a');
        link.download = 'shortlink-qr.png';
        link.href = dataUrl;
        link.click();
      }
    }

    async function copyToClipboard(text, btnElement) {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
        } else {
          const temp = document.createElement('input');
          temp.value = text;
          document.body.appendChild(temp);
          temp.select();
          document.execCommand('copy');
          document.body.removeChild(temp);
        }
        const originalText = btnElement.innerText;
        btnElement.innerText = 'Copied!';
        btnElement.classList.add('bg-emerald-50', 'text-emerald-700', 'border-emerald-200');
        setTimeout(() => {
          btnElement.innerText = originalText;
          btnElement.classList.remove('bg-emerald-50', 'text-emerald-700', 'border-emerald-200');
        }, 1500);
      } catch (e) {}
    }

    async function viewStats(code) {
      const modal = document.getElementById('statsModal');
      const body = document.getElementById('statsModalBody');
      modal.classList.remove('hidden');
      body.innerHTML = `<p class="text-slate-400 text-center py-6">Loading analytics...</p>`;

      try {
        const res = await fetch(`/api/stats/${code}`);
        const data = await res.json();
        const clicksHtml = data.recent_clicks && data.recent_clicks.length > 0 ? 
          data.recent_clicks.map(c => `
            <div class="p-3 bg-slate-50 rounded-xl border border-slate-100 text-xs text-slate-600 space-y-1">
              <div class="flex justify-between font-medium text-slate-800">
                <span>Time: ${c.timestamp}</span>
                <span class="text-indigo-600">Referer: ${escapeHtml(c.referer)}</span>
              </div>
              <div class="truncate text-slate-500" title="${escapeHtml(c.user_agent)}">UA: ${escapeHtml(c.user_agent)}</div>
            </div>
          `).join('') : '<p class="text-slate-400 text-xs py-2">No click records logged yet.</p>';

        let quotaDetails = data.max_clicks ? `Max Clicks Limit: ${data.max_clicks}` : `Max Clicks: Unlimited`;
        let expiryDetails = data.expires_at ? `Expires: ${data.expires_at}` : `Lifetime: Permanent`;

        body.innerHTML = `
          <div class="space-y-4">
            <div class="bg-indigo-50/60 p-4 rounded-xl border border-indigo-100 flex items-center justify-between">
              <div>
                <p class="text-xs text-indigo-700 font-semibold uppercase">Total Clicks</p>
                <p class="text-3xl font-extrabold text-indigo-900">${data.clicks}</p>
              </div>
              <div class="text-right">
                <p class="text-xs text-slate-500">Short Link</p>
                <p class="text-sm font-bold text-slate-800 font-mono">/${data.short_code}</p>
                <button onclick="openQrModal('${data.short_url}')" class="mt-1 text-xs text-indigo-600 hover:underline">View QR Code</button>
              </div>
            </div>
            <div class="grid grid-cols-2 gap-2 text-xs text-slate-600">
              <div class="p-2 bg-slate-50 rounded-lg border border-slate-100">${quotaDetails}</div>
              <div class="p-2 bg-slate-50 rounded-lg border border-slate-100">${expiryDetails}</div>
            </div>
            <div>
              <p class="text-xs font-semibold text-slate-500 uppercase mb-1">Target Destination</p>
              <p class="text-sm text-slate-800 break-all bg-slate-50 p-2.5 rounded-lg border border-slate-200">${escapeHtml(data.original_url)}</p>
            </div>
            <div>
              <p class="text-xs font-semibold text-slate-500 uppercase mb-2">Recent Click Logs (Up to 20)</p>
              <div class="space-y-2 max-h-48 overflow-y-auto pr-1">
                ${clicksHtml}
              </div>
            </div>
          </div>
        `;
      } catch (e) {
        body.innerHTML = `<p class="text-rose-500 text-center py-4">Failed to load statistics.</p>`;
      }
    }

    function closeStatsModal() {
      document.getElementById('statsModal').classList.add('hidden');
    }

    function escapeHtml(str) {
      if (!str) return '';
      return str.replace(/[&<>"']/g, function(m) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
      });
    }

    loadLinks();
  </script>
</body>
</html>
"""

class ShortenerRequestHandler(http.server.BaseHTTPRequestHandler):
    """Custom HTTP handler with JSON API, QR support, and 302 Redirection with Expiration Checks."""

    def _get_base_url(self) -> str:
        """Resolve public URL using Render proxy headers."""
        if CUSTOM_BASE_URL:
            return CUSTOM_BASE_URL
        
        forwarded_proto = self.headers.get("X-Forwarded-Proto")
        host_header = self.headers.get("Host")

        if forwarded_proto and host_header:
            return f"{forwarded_proto}://{host_header}"
        elif host_header:
            return f"https://{host_header}" if "onrender.com" in host_header else f"http://{host_header}"
        
        return f"http://{HOST}:{PORT}"

    def _send_json(self, status_code: int, data):
        response = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(response)

    def _send_html(self, status_code: int, html_content: str):
        encoded = html_content.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.strip("/")
        base_url = self._get_base_url()

        # Render Healthcheck endpoint
        if path == "healthz":
            self._send_json(200, {"status": "ok", "timestamp": datetime.utcnow().isoformat()})
            return

        # Serve Homepage
        if path == "":
            self._send_html(200, FRONTEND_HTML)
            return

        # Favicon handling
        if path == "favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # API: Links list
        if path == "api/links":
            links = fetch_all_links(base_url)
            self._send_json(200, links)
            return

        # API: Stats lookup
        if path.startswith("api/stats/"):
            code = path.replace("api/stats/", "").strip()
            stats = fetch_link_stats(code, base_url)
            if stats:
                self._send_json(200, stats)
            else:
                self._send_json(404, {"error": "Short code not found."})
            return

        # Short Code Redirection and Expiration Handling
        short_code = path
        user_agent = self.headers.get("User-Agent", "")
        referer = self.headers.get("Referer", "")
        client_ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

        validation = record_click_and_validate(short_code, user_agent, referer, client_ip)
        status = validation["status"]

        if status == "valid":
            self.send_response(302)
            self.send_header("Location", validation["url"])
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.end_headers()
        elif status == "expired_time" or status == "expired_clicks":
            reason = "This short link has reached its expiration date and is no longer active." if status == "expired_time" else "This short link has reached its maximum allowed click count and has expired."
            expired_html = f"""<!DOCTYPE html>
            <html lang="en">
            <head><meta charset="UTF-8"><title>Link Expired</title><script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-slate-50 text-slate-800 flex items-center justify-center min-h-screen p-4 font-sans">
              <div class="bg-white p-8 rounded-2xl shadow-sm border border-slate-200 max-w-md w-full text-center">
                <div class="w-12 h-12 bg-amber-100 text-amber-600 rounded-full flex items-center justify-center mx-auto mb-4 font-bold text-xl">⏳</div>
                <h1 class="text-xl font-bold text-slate-900 mb-2">Link Expired</h1>
                <p class="text-slate-500 text-sm mb-6">{reason}</p>
                <a href="/" class="inline-block bg-indigo-600 text-white font-medium text-sm px-5 py-2.5 rounded-xl hover:bg-indigo-700 transition">Go to Homepage</a>
              </div>
            </body>
            </html>"""
            self._send_html(410, expired_html)
        else:
            not_found_html = f"""<!DOCTYPE html>
            <html lang="en">
            <head><meta charset="UTF-8"><title>404 Not Found</title><script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-slate-50 text-slate-800 flex items-center justify-center min-h-screen p-4 font-sans">
              <div class="bg-white p-8 rounded-2xl shadow-sm border border-slate-200 max-w-md w-full text-center">
                <div class="w-12 h-12 bg-rose-100 text-rose-600 rounded-full flex items-center justify-center mx-auto mb-4 font-bold text-xl">404</div>
                <h1 class="text-xl font-bold text-slate-900 mb-2">Short Link Not Found</h1>
                <p class="text-slate-500 text-sm mb-6">The link <code>/{short_code}</code> does not exist.</p>
                <a href="/" class="inline-block bg-indigo-600 text-white font-medium text-sm px-5 py-2.5 rounded-xl hover:bg-indigo-700 transition">Go to Homepage</a>
              </div>
            </body>
            </html>"""
            self._send_html(404, not_found_html)

    def do_POST(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path.strip("/")
        base_url = self._get_base_url()

        if path == "api/shorten":
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_json(400, {"error": "Missing JSON request body."})
                return

            body = self.rfile.read(content_length)
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json(400, {"error": "Malformed JSON in request body."})
                return

            original_url = data.get("original_url")
            custom_code = data.get("custom_code") or None
            expiry_hours = data.get("expiry_hours") or None
            max_clicks = data.get("max_clicks") or None

            if not original_url:
                self._send_json(400, {"error": "'original_url' field is required."})
                return

            result, status_code = shorten_url(original_url, base_url, custom_code, expiry_hours, max_clicks)
            self._send_json(status_code, result)
            return

        self._send_json(404, {"error": "Endpoint not found."})

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def run_server():
    init_db()
    server_address = (HOST, PORT)
    httpd = ThreadedHTTPServer(server_address, ShortenerRequestHandler)
    
    # Print immediately to unbuffered stdout so Render registers log readiness
    print("=" * 60, flush=True)
    print(f" URL Shortener online and listening on {HOST}:{PORT}", flush=True)
    print(f" Features: Instant QR Codes + Expiration Timers Enabled", flush=True)
    print(f" Database file: {os.path.abspath(DATABASE_FILE)}", flush=True)
    print("=" * 60, flush=True)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...", flush=True)
        httpd.server_close()

if __name__ == "__main__":
    run_server()