"""
Thought Evolution backend - text rewrite via claude CLI + image gen via Gemini.
With session-based auth and SQLite history.
"""

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
import time
import urllib.request
from contextlib import closing
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

MAX_BODY = 2 * 1024 * 1024  # 2MB request body limit
REWRITE_TIMEOUT = 180  # 3 min timeout for claude rewrite
RATE_LIMIT_SECONDS = 60  # Min interval between expensive API calls per user

# Per-user, per-action rate limiter: (username, action) -> last_request_time
import threading
_rate_lock = threading.Lock()
_rate_limits: dict[tuple[str, str], float] = {}

PORT = 3200
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash-image"
GEMINI_URL = f"https://api.tokentrove.co/v1beta/models/{GEMINI_MODEL}:generateContent"
IMAGE_DIR = os.path.expanduser("~/thought-evolution-images")
DB_PATH = os.path.join(os.path.dirname(__file__), "history.db")
os.makedirs(IMAGE_DIR, exist_ok=True)

# Seed users — only used on first run when DB is empty.
# Hashes are for initial passwords; users should change them after first login.
_SEED_USERS = {
    "gerald": {"hash": "a58b058ded39b3ea53a625cb8b22e9d2947692ee4cb201d01977131978d82dcd",
               "tier": "pro", "label": "PRO,内测"},
    "effy": {"hash": "e1d076bd1a793065c0a81c94c7218670498736b9af57aa264abdf68b1a9c6fe2",
             "tier": "free", "label": "内测"},
}
DEFAULT_TIER = {"tier": "free", "label": "", "history_limit": 3}
SESSION_MAX_AGE = 7 * 24 * 3600

PLATFORM_NAMES = {
    "wechat": "微信", "xiaohongshu": "小红书", "weibo": "微博", "x": "X (Twitter)",
}
FORMAT_NAMES = {
    "mass": "群发消息", "moments": "朋友圈", "mp": "公众号文章",
    "long": "图文笔记", "short": "短笔记", "longpost": "长微博",
    "tweet": "推文", "thread": "长推文 (thread)",
}
GOAL_NAMES = {
    "share": "真诚分享（不带目的性，单纯分享好内容、真实感受）",
    "growth": "涨粉（吸引新关注、提高互动率）",
    "promo": "推广（产品或服务软性植入）",
    "edu": "科普（知识传播、建立专业形象）",
    "brand": "品牌塑造（个人IP或品牌调性）",
}
PLATFORM_NAMES_EN = {
    "wechat": "WeChat", "xiaohongshu": "Xiaohongshu (RED)", "weibo": "Weibo", "x": "X (Twitter)",
}
FORMAT_NAMES_EN = {
    "mass": "Group message", "moments": "Moments post", "mp": "Official account article",
    "long": "Photo essay", "short": "Short note", "longpost": "Long-form post",
    "tweet": "Tweet", "thread": "Thread",
}
GOAL_NAMES_EN = {
    "share": "Authentic sharing (genuine, no agenda)",
    "growth": "Growth (attract followers, boost engagement)",
    "promo": "Promotion (soft product/service placement)",
    "edu": "Educational (knowledge sharing, build authority)",
    "brand": "Branding (personal IP or brand identity)",
}


# ── Database ──

def get_db():
    """Get a SQLite connection with WAL mode and busy timeout."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT NOT NULL,
            created_at REAL NOT NULL,
            platform TEXT,
            format TEXT,
            goal TEXT,
            styles TEXT,
            input_text TEXT,
            output_text TEXT,
            image_url TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            pw_hash TEXT NOT NULL,
            tier TEXT NOT NULL DEFAULT 'free',
            label TEXT NOT NULL DEFAULT ''
        )
    """)
    # Add columns if missing (migration)
    try:
        conn.execute("ALTER TABLE users ADD COLUMN salt TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE users ADD COLUMN label TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    # Seed default users on first run only
    for u, info in _SEED_USERS.items():
        conn.execute("INSERT OR IGNORE INTO users (username, pw_hash, tier, label) VALUES (?,?,?,?)",
                     (u, info["hash"], info["tier"], info["label"]))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_creates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            creator TEXT NOT NULL,
            created_user TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    # Clean expired sessions on startup
    conn.execute("DELETE FROM sessions WHERE created_at < ?", (time.time() - SESSION_MAX_AGE,))
    conn.commit()
    conn.close()

def get_user_hash(username):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT pw_hash, salt FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        return None, None
    return row[0], row[1] or ""

def hash_password(password, salt=""):
    if not salt:
        salt = secrets.token_hex(16)
    # PBKDF2 with 260k iterations (OWASP 2023 recommendation for SHA256)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000)
    return h.hex(), salt

PRO_MONTHLY_CREATE_LIMIT = 3

def get_user_tier(username):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT tier, label FROM users WHERE username=?", (username,)).fetchone()
    if row:
        hl = 100 if row[0] == "pro" or "内测" in row[1] else 3
        labels = [l.strip() for l in row[1].split(",") if l.strip()]
        is_pro = row[0] == "pro"
        is_beta = "内测" in labels
        has_pro_label = "PRO" in labels
        # PRO can create normal users (monthly quota); beta can create unlimited normal users;
        # PRO+内测 labels (gerald) can create any tier and access admin
        can_create = is_pro or is_beta
        can_create_privileged = has_pro_label and is_beta
        return {"tier": row[0], "label": row[1], "labels": labels, "history_limit": hl,
                "can_create": can_create, "can_create_privileged": can_create_privileged}
    return {**DEFAULT_TIER, "labels": [], "can_create": False, "can_create_privileged": False}

def count_monthly_creates(username):
    """Count how many users this creator has created in the current calendar month."""
    import calendar
    now = time.time()
    t = time.localtime(now)
    month_start = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    with closing(get_db()) as conn:
        row = conn.execute("SELECT COUNT(*) FROM user_creates WHERE creator=? AND created_at>=?",
                           (username, month_start)).fetchone()
    return row[0] if row else 0

def log_user_create(creator, created_user):
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO user_creates (creator, created_user, created_at) VALUES (?,?,?)",
                     (creator, created_user, time.time()))
        conn.commit()

def set_user_hash(username, pw_hash, salt=""):
    with closing(get_db()) as conn:
        conn.execute("UPDATE users SET pw_hash=?, salt=? WHERE username=?", (pw_hash, salt, username))
        conn.commit()

def save_session(token, username):
    with closing(get_db()) as conn:
        conn.execute("INSERT OR REPLACE INTO sessions (token, username, created_at) VALUES (?,?,?)",
                     (token, username, time.time()))
        conn.commit()

def get_session(token):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT username, created_at FROM sessions WHERE token=?", (token,)).fetchone()
    if row and time.time() - row[1] <= SESSION_MAX_AGE:
        return row[0]
    if row:
        delete_session(token)
    return None

def delete_session(token):
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()

def delete_user_sessions(username):
    """Invalidate all sessions for a user (e.g. after password change)."""
    with closing(get_db()) as conn:
        conn.execute("DELETE FROM sessions WHERE username=?", (username,))
        conn.commit()

def create_user(username, pw_hash, salt, tier, label):
    with closing(get_db()) as conn:
        conn.execute("INSERT INTO users (username, pw_hash, salt, tier, label) VALUES (?,?,?,?,?)",
                     (username, pw_hash, salt, tier, label))
        conn.commit()

def save_history(user, platform, fmt, goal, styles, input_text, output_text, image_url=None):
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO history (user, created_at, platform, format, goal, styles, input_text, output_text, image_url) VALUES (?,?,?,?,?,?,?,?,?)",
            (user, time.time(), platform, fmt, goal, json.dumps(styles, ensure_ascii=False), input_text, output_text, image_url),
        )
        new_id = cur.lastrowid
        # Keep only latest N per user based on tier — delete old entries and their images
        tier = get_user_tier(user)
        limit = tier["history_limit"]
        old_rows = conn.execute("""
            SELECT id, image_url FROM history WHERE user=? AND id NOT IN (
                SELECT id FROM history WHERE user=? ORDER BY created_at DESC LIMIT ?
            )
        """, (user, user, limit)).fetchall()
        for row in old_rows:
            if row[1]:
                img_path = os.path.join(IMAGE_DIR, os.path.basename(row[1]))
                try:
                    os.remove(img_path)
                except OSError:
                    pass
        conn.execute("""
            DELETE FROM history WHERE user=? AND id NOT IN (
                SELECT id FROM history WHERE user=? ORDER BY created_at DESC LIMIT ?
            )
        """, (user, user, limit))
        conn.commit()
    return new_id

def update_history_image(history_id, image_url, user=None):
    with closing(get_db()) as conn:
        if user:
            # Only update if the record belongs to this user
            conn.execute("UPDATE history SET image_url=? WHERE id=? AND user=?", (image_url, history_id, user))
        else:
            conn.execute("UPDATE history SET image_url=? WHERE id=?", (image_url, history_id))
        conn.commit()

def get_history(user, limit=20, offset=0):
    with closing(get_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM history WHERE user=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]

def get_all_users():
    with closing(get_db()) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT username, tier, label FROM users ORDER BY username").fetchall()
    return [dict(r) for r in rows]

def delete_user(username):
    with closing(get_db()) as conn:
        # Delete user's history images
        rows = conn.execute("SELECT image_url FROM history WHERE user=?", (username,)).fetchall()
        for row in rows:
            if row[0]:
                img_path = os.path.join(IMAGE_DIR, os.path.basename(row[0]))
                try: os.remove(img_path)
                except OSError: pass
        conn.execute("DELETE FROM history WHERE user=?", (username,))
        conn.execute("DELETE FROM sessions WHERE username=?", (username,))
        conn.execute("DELETE FROM user_creates WHERE creator=? OR created_user=?", (username, username))
        conn.execute("DELETE FROM users WHERE username=?", (username,))
        conn.commit()

def update_user(username, tier=None, label=None):
    with closing(get_db()) as conn:
        if tier is not None:
            conn.execute("UPDATE users SET tier=? WHERE username=?", (tier, username))
        if label is not None:
            conn.execute("UPDATE users SET label=? WHERE username=?", (label, username))
        conn.commit()

def get_admin_stats():
    with closing(get_db()) as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tier_counts = conn.execute("SELECT tier, COUNT(*) FROM users GROUP BY tier").fetchall()
        total_history = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        active_sessions = conn.execute("SELECT COUNT(*) FROM sessions WHERE created_at > ?",
                                       (time.time() - SESSION_MAX_AGE,)).fetchone()[0]
        # Monthly creates
        t = time.localtime()
        month_start = time.mktime((t.tm_year, t.tm_mon, 1, 0, 0, 0, 0, 0, -1))
        monthly_creates = conn.execute(
            "SELECT creator, created_user, created_at FROM user_creates WHERE created_at >= ? ORDER BY created_at DESC",
            (month_start,)).fetchall()
    return {
        "total_users": total_users,
        "tier_counts": {r[0]: r[1] for r in tier_counts},
        "total_history": total_history,
        "active_sessions": active_sessions,
        "monthly_creates": [{"creator": r[0], "created_user": r[1], "created_at": r[2]} for r in monthly_creates],
    }

def clean_expired_sessions():
    with closing(get_db()) as conn:
        n = conn.execute("SELECT COUNT(*) FROM sessions WHERE created_at < ?",
                         (time.time() - SESSION_MAX_AGE,)).fetchone()[0]
        conn.execute("DELETE FROM sessions WHERE created_at < ?", (time.time() - SESSION_MAX_AGE,))
        conn.commit()
    return n

init_db()


# ── Helpers ──

def build_prompt(article, platform, fmt, goal, styles, lang="zh"):
    if lang == "en":
        pn = PLATFORM_NAMES_EN.get(platform, platform)
        fn = FORMAT_NAMES_EN.get(fmt, fmt)
        gd = GOAL_NAMES_EN.get(goal, goal)
        ss = ", ".join(styles) if styles else "natural and fluent"
        return f"""You are a professional social media content rewriter. Rewrite the article according to the following requirements.

## Requirements
- Platform: {pn}
- Format: {fn}
- Writing style: {ss}
- Goal: {gd}

## Platform rules
Strictly follow the tone, length limits, and formatting conventions of "{pn} · {fn}".
Ensure the content fits the platform's audience reading habits and sharing patterns.

## Output
- Output the rewritten text only, no extra explanation
- If a title is needed, put it on the first line
- Write entirely in English

---
{article}
"""
    platform_name = PLATFORM_NAMES.get(platform, platform)
    format_name = FORMAT_NAMES.get(fmt, fmt)
    goal_desc = GOAL_NAMES.get(goal, goal)
    style_str = "、".join(styles) if styles else "自然流畅"
    return f"""你是一个专业的社交媒体内容改写专家。请根据以下要求改写文章。

## 改写要求
- 目标平台：{platform_name}
- 内容格式：{format_name}
- 写作风格：{style_str}
- 发布目标：{goal_desc}

## 平台规则
请严格遵循「{platform_name} · {format_name}」的内容调性、长度限制和排版习惯。
确保内容适合该平台的用户阅读习惯和传播方式。

## 输出要求
- 只输出改写后的正文，不要额外说明
- 如果需要标题，直接写在正文第一行

---
{article}
"""

ASPECT_DESC = {
    "1:1": "square 1:1 aspect ratio",
    "3:4": "portrait 3:4 aspect ratio",
    "4:3": "landscape 4:3 aspect ratio",
    "9:16": "tall portrait 9:16 aspect ratio (phone screen)",
    "16:9": "wide 16:9 aspect ratio (widescreen)",
    "2.35:1": "ultra-wide 2.35:1 cinematic aspect ratio",
}

def generate_image(prompt, aspect_ratio=None):
    full_prompt = prompt
    if aspect_ratio and aspect_ratio in ASPECT_DESC:
        full_prompt = f"{prompt}. Image format: {ASPECT_DESC[aspect_ratio]}"
    payload = {
        "contents": [{"parts": [{"text": full_prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        GEMINI_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {GEMINI_API_KEY}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    for part in candidates[0].get("content", {}).get("parts", []):
        if "inlineData" in part:
            img_bytes = base64.b64decode(part["inlineData"]["data"])
            filename = f"img_{int(time.time())}_{secrets.token_hex(4)}.png"
            with open(os.path.join(IMAGE_DIR, filename), "wb") as f:
                f.write(img_bytes)
            return filename
    raise RuntimeError("Gemini response contained no image data")


# ── Handler ──

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Credentials", "true")

    def _get_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        # Fallback: token in query string (for <img src> etc.)
        qs = parse_qs(urlparse(self.path).query)
        t = qs.get("token", [None])[0]
        return t

    def _get_user(self):
        token = self._get_token()
        if not token:
            return None
        return get_session(token)

    def _json_error(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _require_auth(self):
        if not self._get_user():
            self._json_error(401, "unauthorized")
            return False
        return True

    def _require_admin(self):
        user = self._get_user()
        if not user:
            self._json_error(401, "unauthorized"); return False
        tier = get_user_tier(user)
        if not tier.get("can_create_privileged"):
            self._json_error(403, "admin only"); return False
        return True

    def _check_rate_limit(self, action="default"):
        """Returns True if allowed, False if rate-limited. Each action has its own timer. Beta users bypass."""
        user = self._get_user() or self.client_address[0]
        # Beta users have no cooldown
        if user:
            tier = get_user_tier(user)
            if "内测" in tier.get("labels", []):
                return True
        key = (user, action)
        now = time.time()
        with _rate_lock:
            last = _rate_limits.get(key, 0)
            if now - last < RATE_LIMIT_SECONDS:
                remaining = int(RATE_LIMIT_SECONDS - (now - last))
                self._json_error(429, f"please wait {remaining}s")
                return False
            _rate_limits[key] = now
        return True

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._json_ok({"status": "ok"})
            return
        if path == "/api/me":
            user = self._get_user()
            if user:
                tier = get_user_tier(user)
                self._json_ok({"user": user, "tier": tier["tier"], "label": tier["label"]})
            else:
                self._json_error(401, "unauthorized")
            return
        if path == "/api/history":
            if not self._require_auth(): return
            qs = parse_qs(parsed.query)
            limit = min(int(qs.get("limit", [20])[0]), 100)
            offset = int(qs.get("offset", [0])[0])
            rows = get_history(self._get_user(), limit, offset)
            self._json_ok({"items": rows})
            return
        if path == "/admin":
            # Serve admin page statically; auth checked client-side via API
            admin_path = os.path.join(WORK_DIR, "admin.html")
            if os.path.isfile(admin_path):
                with open(admin_path, "rb") as f:
                    body = f.read()
                self.send_response(200); self._cors()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self._json_error(404, "admin page not found")
            return
        if path == "/api/admin/users":
            if not self._require_admin(): return
            self._json_ok({"users": get_all_users()})
            return
        if path == "/api/admin/stats":
            if not self._require_admin(): return
            self._json_ok(get_admin_stats())
            return
        if path == "/api/admin/user-history":
            if not self._require_admin(): return
            qs = parse_qs(parsed.query)
            target = qs.get("user", [""])[0]
            if not target:
                self._json_error(400, "user required"); return
            limit = min(int(qs.get("limit", [100])[0]), 200)
            rows = get_history(target, limit, 0)
            self._json_ok({"user": target, "items": rows})
            return
        if path == "/api/admin/sessions":
            if not self._require_admin(): return
            with closing(get_db()) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT token, username, created_at FROM sessions WHERE created_at > ? ORDER BY created_at DESC",
                    (time.time() - SESSION_MAX_AGE,)).fetchall()
            self._json_ok({"sessions": [dict(r) for r in rows]})
            return
        if path.startswith("/images/"):
            filename = os.path.basename(path.split("/images/", 1)[1])
            filepath = os.path.join(IMAGE_DIR, filename)
            if not os.path.realpath(filepath).startswith(os.path.realpath(IMAGE_DIR)):
                self.send_response(403); self.end_headers(); return
            if os.path.isfile(filepath):
                with open(filepath, "rb") as f:
                    body = f.read()
                self.send_response(200); self._cors()
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        self.send_response(404); self.send_header('Content-Length', '0'); self.end_headers()

    def do_POST(self):
        if self.path == "/api/login": return self._handle_login()
        if self.path == "/api/logout": return self._handle_logout()
        # Public endpoints — no auth required
        if self.path == "/api/rewrite": return self._handle_rewrite()
        if self.path == "/api/analyze-image": return self._handle_analyze_image()
        if self.path == "/api/gen-image": return self._handle_gen_image()
        # Auth-required endpoints
        if not self._require_auth(): return
        if self.path == "/api/change-password": return self._handle_change_password()
        if self.path == "/api/create-user": return self._handle_create_user()
        if self.path == "/api/save-history": return self._handle_save_history()
        if self.path == "/api/update-history-image": return self._handle_update_history_image()
        # Admin endpoints
        if self.path.startswith("/api/admin/"):
            if not self._require_admin(): return
            if self.path == "/api/admin/delete-user": return self._handle_admin_delete_user()
            if self.path == "/api/admin/update-user": return self._handle_admin_update_user()
            if self.path == "/api/admin/reset-password": return self._handle_admin_reset_password()
            if self.path == "/api/admin/clean-sessions": return self._handle_admin_clean_sessions()
        self.send_response(404); self.end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json_ok(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_login(self):
        try:
            data = self._read_json()
            username = data.get("username", "").strip().lower()
            password = data.get("password", "")
        except Exception:
            self._json_error(400, "invalid json"); return
        stored_hash, salt = get_user_hash(username)
        if not stored_hash:
            self._json_error(401, "invalid credentials"); return
        if salt:
            pw_hash, _ = hash_password(password, salt)
        else:
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
        if stored_hash != pw_hash:
            self._json_error(401, "invalid credentials"); return
        token = secrets.token_hex(32)
        save_session(token, username)
        # Auto-migrate to salted hash if still unsalted
        if not salt:
            new_hash, new_salt = hash_password(password)
            set_user_hash(username, new_hash, new_salt)
        tier = get_user_tier(username)
        self._json_ok({"token": token, "user": username, "tier": tier["tier"], "label": tier["label"], "can_create": tier["can_create"]})

    def _handle_logout(self):
        token = self._get_token()
        if token: delete_session(token)
        self._json_ok({"ok": True})

    def _handle_change_password(self):
        try:
            data = self._read_json()
            old_pw = data.get("old_password", "")
            new_pw = data.get("new_password", "")
        except Exception:
            self._json_error(400, "invalid json"); return
        if not new_pw or len(new_pw) < 6:
            self._json_error(400, "new password too short (min 6)"); return
        user = self._get_user()
        stored_hash, salt = get_user_hash(user)
        if salt:
            old_hash, _ = hash_password(old_pw, salt)
        else:
            old_hash = hashlib.sha256(old_pw.encode()).hexdigest()
        if old_hash != stored_hash:
            self._json_error(403, "wrong old password"); return
        new_hash, new_salt = hash_password(new_pw)
        set_user_hash(user, new_hash, new_salt)
        delete_user_sessions(user)
        self._json_ok({"ok": True})

    def _handle_create_user(self):
        user = self._get_user()
        tier = get_user_tier(user)
        if not tier.get("can_create"):
            self._json_error(403, "no permission"); return
        try:
            data = self._read_json()
            new_user = data.get("username", "").strip().lower()
            new_pw = data.get("password", "")
            new_label = data.get("label", "")
        except Exception:
            self._json_error(400, "invalid json"); return
        if not new_user or len(new_user) < 2:
            self._json_error(400, "username too short"); return
        if not new_pw or len(new_pw) < 6:
            self._json_error(400, "password too short (min 6)"); return
        # Only PRO+内测 (gerald) can assign privileged labels
        is_privileged_target = new_label in ("PRO", "内测", "PRO,内测")
        if is_privileged_target and not tier.get("can_create_privileged"):
            self._json_error(403, "only admin can create PRO/内测 accounts"); return
        if not is_privileged_target:
            new_label = ""
        # PRO users (non-beta) have monthly quota
        is_beta = "内测" in tier.get("labels", [])
        if not is_beta:
            used = count_monthly_creates(user)
            if used >= PRO_MONTHLY_CREATE_LIMIT:
                self._json_error(429, f"monthly limit reached ({PRO_MONTHLY_CREATE_LIMIT}/month)"); return
        if get_user_hash(new_user)[0]:
            self._json_error(409, "user already exists"); return
        new_tier = "pro" if "PRO" in new_label else "free"
        pw_hash, salt = hash_password(new_pw)
        create_user(new_user, pw_hash, salt, new_tier, new_label)
        log_user_create(user, new_user)
        self._json_ok({"ok": True, "user": new_user, "tier": new_tier, "label": new_label})

    def _handle_rewrite(self):
        if not self._check_rate_limit("rewrite"): return
        try:
            data = self._read_json()
            article = data.get("text", "").strip()
            platform = data.get("platform", "wechat")
            fmt = data.get("format", "mp")
            goal = data.get("goal", "growth")
            styles = data.get("styles", [])
            lang = data.get("lang", "zh")
        except Exception:
            self._json_error(400, "invalid json"); return
        if not article:
            self._json_error(400, "empty text"); return

        prompt = build_prompt(article, platform, fmt, goal, styles, lang)
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Send initial event so proxies start forwarding immediately
        self.wfile.write(b'data: {"status": "processing"}\n\n')
        self.wfile.flush()

        # Keepalive thread prevents proxy timeouts
        write_lock = threading.Lock()
        stop_event = threading.Event()
        def keepalive():
            while not stop_event.is_set():
                stop_event.wait(3)
                if stop_event.is_set():
                    break
                try:
                    with write_lock:
                        self.wfile.write(b'data: {"keepalive": true}\n\n')
                        self.wfile.flush()
                except Exception:
                    break
        ka_thread = threading.Thread(target=keepalive, daemon=True)
        ka_thread.start()

        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "-p", "-", "--output-format", "text"],
                input=prompt.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=WORK_DIR, timeout=REWRITE_TIMEOUT,
            )
            stop_event.set()
            ka_thread.join(2)
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace")
                sse_data = json.dumps({"error": err}, ensure_ascii=False)
                with write_lock:
                    self.wfile.write(("data: " + sse_data + "\n\n").encode("utf-8"))
                    self.wfile.flush()
            else:
                result_text = proc.stdout.decode("utf-8", errors="replace").strip()
                sse_data = json.dumps({"text": result_text}, ensure_ascii=False)
                with write_lock:
                    self.wfile.write(("data: " + sse_data + "\n\n").encode("utf-8"))
                    self.wfile.flush()
        except subprocess.TimeoutExpired:
            stop_event.set()
            ka_thread.join(2)
            sse_data = json.dumps({"error": "rewrite timed out"}, ensure_ascii=False)
            with write_lock:
                self.wfile.write(("data: " + sse_data + "\n\n").encode("utf-8"))
                self.wfile.flush()
        except Exception as e:
            stop_event.set()
            ka_thread.join(2)
            sse_data = json.dumps({"error": str(e)}, ensure_ascii=False)
            with write_lock:
                self.wfile.write(("data: " + sse_data + "\n\n").encode("utf-8"))
                self.wfile.flush()

        with write_lock:
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()

    def _handle_save_history(self):
        try:
            data = self._read_json()
        except Exception:
            self._json_error(400, "invalid json"); return
        user = self._get_user()
        new_id = save_history(
            user,
            data.get("platform", ""),
            data.get("format", ""),
            data.get("goal", ""),
            data.get("styles", []),
            data.get("input_text", ""),
            data.get("output_text", ""),
            data.get("image_url"),
        )
        self._json_ok({"ok": True, "id": new_id})

    def _handle_update_history_image(self):
        try:
            data = self._read_json()
            hid = data.get("id")
            image_url = data.get("image_url", "")
        except Exception:
            self._json_error(400, "invalid json"); return
        if hid:
            update_history_image(hid, image_url, user=self._get_user())
        self._json_ok({"ok": True})

    def _handle_analyze_image(self):
        try:
            data = self._read_json()
            text = data.get("text", "").strip()
        except Exception:
            self._json_error(400, "invalid json"); return
        if not text:
            self._json_error(400, "empty text"); return

        analyze_prompt = f"""分析以下文章，为其生成一段精准的AI绘图提示词（英文）。

要求：
- 描述一个能代表文章核心主题的画面场景
- 风格：现代、干净、适合社交媒体配图
- 不要包含任何文字或 logo
- 只输出提示词本身，不要其他说明
- 用英文输出，100词以内

文章：
{text[:800]}"""
        try:
            proc = subprocess.run(
                [CLAUDE_BIN, "-p", "-", "--output-format", "text"],
                input=analyze_prompt.encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=WORK_DIR, timeout=60,
            )
            img_prompt = proc.stdout.decode("utf-8", errors="replace").strip()
            if not img_prompt or proc.returncode != 0:
                img_prompt = f"Modern clean social media illustration about: {text[:200]}"
        except Exception:
            img_prompt = f"Modern clean social media illustration about: {text[:200]}"
        self._json_ok({"prompt": img_prompt})

    def _handle_gen_image(self):
        if not self._check_rate_limit("gen-image"): return
        try:
            data = self._read_json()
            prompt = data.get("prompt", "").strip()
            aspect_ratio = data.get("aspect_ratio")
            save_hist = data.get("save_history", False)
        except Exception:
            self._json_error(400, "invalid json"); return
        if not prompt:
            self._json_error(400, "empty prompt"); return
        try:
            filename = generate_image(prompt, aspect_ratio)
            url = f"/images/{filename}"
            result = {"url": url}
            if save_hist:
                user = self._get_user()
                if user:
                    new_id = save_history(user, "image", aspect_ratio or "1:1", "", [], prompt, "", url)
                    result["history_id"] = new_id
            self._json_ok(result)
        except Exception as e:
            self._json_error(500, str(e))

    # ── Admin Handlers ──

    def _handle_admin_delete_user(self):
        data = self._read_json()
        target = data.get("username", "").strip().lower()
        if not target:
            self._json_error(400, "username required"); return
        me = self._get_user()
        if target == me:
            self._json_error(400, "cannot delete yourself"); return
        if not get_user_hash(target)[0]:
            self._json_error(404, "user not found"); return
        delete_user(target)
        self._json_ok({"ok": True, "deleted": target})

    def _handle_admin_update_user(self):
        data = self._read_json()
        target = data.get("username", "").strip().lower()
        if not target or not get_user_hash(target)[0]:
            self._json_error(404, "user not found"); return
        new_tier = data.get("tier")
        new_label = data.get("label")
        if new_tier and new_tier not in ("free", "pro"):
            self._json_error(400, "tier must be free or pro"); return
        update_user(target, tier=new_tier, label=new_label)
        self._json_ok({"ok": True, "user": target, "tier": new_tier, "label": new_label})

    def _handle_admin_reset_password(self):
        data = self._read_json()
        target = data.get("username", "").strip().lower()
        new_pw = data.get("password", "")
        if not target or not get_user_hash(target)[0]:
            self._json_error(404, "user not found"); return
        if not new_pw or len(new_pw) < 6:
            self._json_error(400, "password too short (min 6)"); return
        pw_hash, salt = hash_password(new_pw)
        set_user_hash(target, pw_hash, salt)
        delete_user_sessions(target)
        self._json_ok({"ok": True, "user": target})

    def _handle_admin_clean_sessions(self):
        n = clean_expired_sessions()
        self._json_ok({"ok": True, "cleaned": n})

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

if __name__ == "__main__":
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Thought Evolution backend running on port {PORT}")
    server.serve_forever()
