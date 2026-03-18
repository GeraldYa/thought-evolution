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
import threading
import time
import urllib.request
from contextlib import closing
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

MAX_BODY = 2 * 1024 * 1024  # 2MB request body limit
REWRITE_TIMEOUT = 180  # 3 min timeout for claude rewrite

PORT = 3200
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")
WORK_DIR = os.path.expanduser("~/.claude/workspace/telegram-bot")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyA67P-IGUy-hw21eBVRoMNjLZcC31zCCv8")
GEMINI_MODEL = "gemini-2.5-flash-image"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
IMAGE_DIR = os.path.expanduser("~/thought-evolution-images")
DB_PATH = os.path.join(os.path.dirname(__file__), "history.db")
os.makedirs(IMAGE_DIR, exist_ok=True)

USERS = {
    "gerald": "a58b058ded39b3ea53a625cb8b22e9d2947692ee4cb201d01977131978d82dcd",
    "effy": "e1d076bd1a793065c0a81c94c7218670498736b9af57aa264abdf68b1a9c6fe2",
}
USER_TIERS = {
    "gerald": {"tier": "pro", "label": "PRO,内测", "history_limit": 100},
    "effy": {"tier": "pro", "label": "内测", "history_limit": 100},
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
    # Seed default users if not exist, update tier/label for existing ones
    for u, h in USERS.items():
        t = USER_TIERS.get(u, DEFAULT_TIER)
        conn.execute("INSERT OR IGNORE INTO users (username, pw_hash, tier, label) VALUES (?,?,?,?)",
                     (u, h, t["tier"], t["label"]))
        conn.execute("UPDATE users SET tier=?, label=? WHERE username=?",
                     (t["tier"], t["label"], u))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
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
    return hashlib.sha256((salt + password).encode()).hexdigest(), salt

def get_user_tier(username):
    with closing(get_db()) as conn:
        row = conn.execute("SELECT tier, label FROM users WHERE username=?", (username,)).fetchone()
    if row:
        hl = 100 if row[0] == "pro" else 3
        labels = [l.strip() for l in row[1].split(",") if l.strip()]
        can_create = "PRO" in labels and "内测" in labels
        return {"tier": row[0], "label": row[1], "labels": labels, "history_limit": hl, "can_create": can_create}
    return {**DEFAULT_TIER, "labels": [], "can_create": False}

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
        headers={"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY},
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
        self.send_response(code); self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": msg}).encode())

    def _require_auth(self):
        if not self._get_user():
            self._json_error(401, "unauthorized")
            return False
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
        if path.startswith("/images/"):
            if not self._require_auth(): return
            filename = os.path.basename(path.split("/images/", 1)[1])
            filepath = os.path.join(IMAGE_DIR, filename)
            if not os.path.realpath(filepath).startswith(os.path.realpath(IMAGE_DIR)):
                self.send_response(403); self.end_headers(); return
            if os.path.isfile(filepath):
                self.send_response(200); self._cors()
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
                return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path == "/api/login": return self._handle_login()
        if self.path == "/api/logout": return self._handle_logout()
        if not self._require_auth(): return
        if self.path == "/api/change-password": return self._handle_change_password()
        if self.path == "/api/create-user": return self._handle_create_user()
        if self.path == "/api/rewrite": return self._handle_rewrite()
        if self.path == "/api/save-history": return self._handle_save_history()
        if self.path == "/api/update-history-image": return self._handle_update_history_image()
        if self.path == "/api/analyze-image": return self._handle_analyze_image()
        if self.path == "/api/gen-image": return self._handle_gen_image()
        self.send_response(404); self.end_headers()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            raise ValueError("request body too large")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _json_ok(self, obj):
        self.send_response(200); self._cors()
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False).encode())

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
        if new_label not in ("", "PRO", "内测"):
            new_label = ""
        if get_user_hash(new_user):
            self._json_error(409, "user already exists"); return
        new_tier = "pro" if new_label else "free"
        pw_hash, salt = hash_password(new_pw)
        create_user(new_user, pw_hash, salt, new_tier, new_label)
        self._json_ok({"ok": True, "user": new_user, "tier": new_tier, "label": new_label})

    def _handle_rewrite(self):
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
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            proc = subprocess.Popen(
                [CLAUDE_BIN, "-p", "-", "--output-format", "text"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=WORK_DIR,
            )
            proc.stdin.write(prompt.encode("utf-8"))
            proc.stdin.close()
            # Watchdog: kill subprocess if it runs too long
            timer = threading.Timer(REWRITE_TIMEOUT, lambda: proc.kill())
            timer.start()
            try:
                for line in iter(proc.stdout.readline, b""):
                    text = line.decode("utf-8", errors="replace")
                    sse_data = json.dumps({"text": text}, ensure_ascii=False)
                    self.wfile.write(f"data: {sse_data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                proc.wait()
            finally:
                timer.cancel()
            if proc.returncode != 0:
                err = proc.stderr.read().decode("utf-8", errors="replace")
                sse_data = json.dumps({"error": err}, ensure_ascii=False)
                self.wfile.write(f"data: {sse_data}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
        except Exception as e:
            sse_data = json.dumps({"error": str(e)}, ensure_ascii=False)
            self.wfile.write(f"data: {sse_data}\n\n".encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()

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
                new_id = save_history(user, "image", aspect_ratio or "1:1", "", [], prompt, "", url)
                result["history_id"] = new_id
            self._json_ok(result)
        except Exception as e:
            self._json_error(500, str(e))

    def log_message(self, format, *args):
        print(f"[{self.log_date_time_string()}] {format % args}")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    server = ThreadedHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Thought Evolution backend running on port {PORT}")
    server.serve_forever()
