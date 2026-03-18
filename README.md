# Thought Evolution 思维进化

> Paste content, one-click rewrite for any social platform.
>
> 中文介绍见下方 👉 [中文](#中文介绍)

Thought Evolution is a self-hosted content rewriting tool — powered by **Claude Code CLI** and **Gemini** image generation. Paste an article, pick a platform, choose your tone, and get a publish-ready rewrite in seconds. Zero API costs for text (uses your Claude subscription).

## Why Thought Evolution?

Content creators spend hours adapting one piece of content for different platforms. Each platform has its own tone, length, and formatting conventions. Thought Evolution automates this:

- **Multi-platform** — WeChat (mass/moments/articles), Xiaohongshu, Weibo, X (Twitter)
- **Goal-driven** — Authentic sharing, growth, promotion, education, branding
- **Style tags** — Warm, edgy, humor, professional, sharp, literary, casual, premium
- **AI cover art** — Generate matching images via Gemini with aspect ratio control
- **Bilingual UI** — Full Chinese/English interface with auto language detection
- **History** — Browse, reload, and re-edit past rewrites
- **User system** — Session auth, password management, tiered access, user creation

## How It Works

```
Browser → NAS proxy (static HTML + reverse proxy) → Backend API → Claude Code CLI / Gemini API
```

The frontend is a single HTML file served by a lightweight Python proxy. All API requests are reverse-proxied to the backend, which calls Claude Code CLI for text rewriting and Gemini API for image generation.

**No Claude API key needed.** Text rewriting uses your existing Claude subscription via CLI.

## Quick Start

### Backend

```bash
git clone https://github.com/GeraldYa/thought-evolution.git
cd thought-evolution

cp .env.example .env
# Edit .env with your Gemini API key

python server.py
# Backend runs on port 3200
```

### Systemd Service

```bash
sudo cp thought-evolution.service /etc/systemd/system/
# Edit paths in the service file to match your setup
sudo systemctl daemon-reload
sudo systemctl enable --now thought-evolution
```

## Architecture

```
thought-evolution/
├── server.py                  # Backend API server (port 3200)
├── thought-evolution.service  # Systemd unit file
├── .env                       # Gemini API key (gitignored)
└── .env.example               # Config template
```

The frontend (`index.html`) is deployed separately on a NAS/CDN/static host with a reverse proxy pointing `/api/*` and `/images/*` to the backend.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Yes | Google Gemini API key for image generation |

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | No | Health check |
| POST | `/api/login` | No | Authenticate, returns session token |
| POST | `/api/logout` | Yes | Invalidate session |
| GET | `/api/me` | Yes | Current user info + tier |
| POST | `/api/rewrite` | Yes | SSE stream — rewrite article via Claude CLI |
| POST | `/api/save-history` | Yes | Save rewrite to history |
| GET | `/api/history` | Yes | Paginated history list |
| POST | `/api/analyze-image` | Yes | Generate image prompt from article text |
| POST | `/api/gen-image` | Yes | Generate image via Gemini |
| POST | `/api/update-history-image` | Yes | Attach image to history entry |
| POST | `/api/change-password` | Yes | Update password |
| POST | `/api/create-user` | Yes | Create new user (admin only) |
| GET | `/images/*` | Yes | Serve generated images |

## Rewrite Flow

1. User pastes article text, selects platform/format/goal/style
2. Frontend detects input language (Chinese or English)
3. Backend builds a tailored prompt and streams it to Claude Code CLI
4. Claude rewrites the content following platform-specific conventions
5. Result streams back via SSE to the browser in real-time
6. Optionally: AI analyzes the article and generates a cover image via Gemini

## Supported Platforms

| Platform | Formats |
|----------|---------|
| WeChat | Mass message, Moments, Official Account article |
| Xiaohongshu | Photo essay, Short note |
| Weibo | Long post, Short post |
| X (Twitter) | Tweet, Thread |

## User Tiers

| Tier | History Limit | Features |
|------|---------------|----------|
| Free | 3 | Basic rewriting |
| Pro | 100 | Full history, image generation |
| Pro + Admin | 100 | All above + create new users |

## Security

- Session-based auth with 7-day expiry
- Salted SHA-256 password hashing (auto-migrates unsalted hashes on login)
- Path traversal protection on image serving
- Request body size limit (2MB)
- Ownership validation on history updates
- Subprocess timeout (3 min) for Claude CLI

## Requirements

- **Claude Pro or Team subscription** with Claude Code CLI installed
- Python 3.10+
- Google Gemini API key (for image generation)
- A Linux/macOS machine to host the backend

## License

MIT

---

## 中文介绍

思维进化是一个自托管的内容改写工具——基于 **Claude Code CLI** 和 **Gemini** 图片生成。粘贴文章，选平台，选风格，秒出可发布的改写内容。文字改写零额外费用（用你已有的 Claude 订阅）。

### 为什么用思维进化？

做自媒体最烦的就是同一篇内容要适配不同平台。每个平台的调性、长度、排版都不一样。思维进化帮你自动搞定：

- **多平台** — 微信（群发/朋友圈/公众号）、小红书、微博、X (Twitter)
- **目标驱动** — 真诚分享、涨粉、推广、科普、品牌塑造
- **风格标签** — 温暖、扎心、幽默、专业、犀利、文艺、接地气、高级感
- **AI 配图** — 通过 Gemini 生成匹配的图片，支持多种宽高比
- **中英双语** — 完整的中英文界面，自动检测输入语言
- **历史记录** — 浏览、回溯、重新编辑过去的改写
- **用户系统** — 会话认证、密码管理、分级权限、用户创建

### 工作原理

```
浏览器 → NAS 代理（静态 HTML + 反向代理）→ 后端 API → Claude Code CLI / Gemini API
```

前端是一个纯 HTML 文件，由轻量 Python 代理服务器托管。所有 API 请求反向代理到后端，后端调用 Claude Code CLI 做文字改写，调用 Gemini API 做图片生成。

**不需要 Claude API key。** 文字改写直接用你的 Claude 订阅额度。

### 快速开始

```bash
git clone https://github.com/GeraldYa/thought-evolution.git
cd thought-evolution

cp .env.example .env
# 编辑 .env，填入你的 Gemini API key

python server.py
# 后端运行在 3200 端口
```

### Systemd 部署

```bash
sudo cp thought-evolution.service /etc/systemd/system/
# 编辑 service 文件中的路径
sudo systemctl daemon-reload
sudo systemctl enable --now thought-evolution
```

### 改写流程

1. 粘贴文章原文，选择平台 / 格式 / 目标 / 风格
2. 前端自动检测输入语言（中文或英文）
3. 后端构建定制 prompt，流式调用 Claude Code CLI
4. Claude 按照平台规则改写内容
5. 结果通过 SSE 实时流式返回浏览器
6. 可选：AI 分析文章主题，通过 Gemini 生成配图

### 支持平台

| 平台 | 格式 |
|------|------|
| 微信 | 群发消息、朋友圈、公众号文章 |
| 小红书 | 图文笔记、短笔记 |
| 微博 | 长微博、短微博 |
| X (Twitter) | 推文、长推文 (thread) |

### 前置条件

- **Claude Pro 或 Team 订阅**，并安装好 Claude Code CLI
- Python 3.10+
- Google Gemini API key（用于图片生成）
- 一台 Linux/macOS 机器托管后端
