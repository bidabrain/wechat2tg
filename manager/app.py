"""wechat2tg 管理面板（Linux 桌面版微信）。

职责：
  * noVNC 网页画面（内置 WebSocket→VNC 转发，单端口 + 统一登录）——扫码登录 / 看画面
  * 微信登录状态、转发服务状态
  * Telegram 配置（bot token / chat id / 开关），保存到 /data/config.json
  * 服务控制（重启微信 / 桥接）、日志查看、最近消息预览
"""

import asyncio
import hashlib
import hmac
import os
import secrets
import subprocess
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

import config
import wxkey
from collector import Collector
from common import DATA, LOG_DIR, log, run, tail

STATIC_DIR = Path(__file__).parent / "static"
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))
SUPERVISOR_CONF = os.environ.get("SUPERVISOR_CONF", "/etc/supervisor/wx.conf")
VNC_PORT = int(os.environ.get("VNC_PORT", "5900"))
WECHAT_HOME = os.environ.get("WECHAT_HOME", "/data/home")
COOKIE = "wx2tg_session"
SESSION_TTL = 7 * 86400
LOG_FILES = {"manager": "manager.log", "bridge": "bridge.log", "wechat": "wechat.log",
             "display": "display.log", "supervisord": "supervisord.log"}


# ---------------------------------------------------------------- 认证
def _load_secret(name: str, generator) -> str:
    p = DATA / name
    if p.exists():
        return p.read_text("utf-8").strip()
    val = generator()
    p.write_text(val, "utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return val


SECRET = _load_secret(".secret", lambda: secrets.token_hex(32)).encode()
WEB_PASSWORD = os.environ.get("WEB_PASSWORD") or _load_secret(".web_password", lambda: secrets.token_urlsafe(12))
if not os.environ.get("WEB_PASSWORD"):
    log.warning("未设置 WEB_PASSWORD，已自动生成管理密码：%s（保存在 %s）", WEB_PASSWORD, DATA / ".web_password")


def make_token() -> str:
    exp = str(int(time.time()) + SESSION_TTL)
    return f"{exp}.{hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()}"


def check_token(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time()


PUBLIC_PATHS = {"/login", "/api/login", "/healthz", "/static/style.css", "/static/login.js"}


# ---------------------------------------------------------------- supervisor
def sv(*args: str) -> subprocess.CompletedProcess:
    return run(["supervisorctl", "-c", SUPERVISOR_CONF, *args], 90)


def sv_status() -> dict[str, str]:
    out = {}
    for line in sv("status").stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


# ---------------------------------------------------------------- 微信状态
_wx_cache = {"ts": 0.0, "logged_in": False}


def wechat_status() -> dict:
    running = wxkey.find_wechat_pid() is not None
    db_dir = wxkey.wechat_db_dir(WECHAT_HOME)
    logged_in = _wx_cache["logged_in"]
    if time.time() - _wx_cache["ts"] > 10:   # 取密钥较重，缓存 10s
        logged_in = bool(running and db_dir and wxkey.find_keys(db_dir))
        _wx_cache.update(ts=time.time(), logged_in=logged_in)
    return {"running": running, "logged_in": logged_in, "db_dir": db_dir}


_collector: Collector | None = None


def get_collector() -> Collector:
    global _collector
    if _collector is None:
        _collector = Collector(WECHAT_HOME)
    return _collector


# ---------------------------------------------------------------- 应用
@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or check_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login")
async def login_page():
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    if not hmac.compare_digest(str(body.get("password", "")), WEB_PASSWORD):
        await asyncio.sleep(1)
        raise HTTPException(401, "密码错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE, make_token(), max_age=SESSION_TTL, httponly=True, samesite="lax")
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------------------------------------------------------------- 状态
@app.get("/api/status")
async def api_status():
    st = await asyncio.to_thread(sv_status)
    wx = await asyncio.to_thread(wechat_status)
    cfg = config.load()
    return {
        "wechat": wx,
        "services": {k: st.get(k, "UNKNOWN") for k in ("wechat", "bridge", "xvfb", "x11vnc", "openbox")},
        "telegram": {"configured": bool(cfg.get("tg_bot_token") and cfg.get("tg_chat_id")),
                     "forward_enabled": cfg.get("forward_enabled", True),
                     "reply_enabled": cfg.get("reply_enabled", True)},
        "now": int(time.time()),
    }


# ---------------------------------------------------------------- Telegram 配置
def _mask(tok: str) -> str:
    return (tok[:4] + "…" + tok[-4:]) if tok and len(tok) > 10 else ("已设置" if tok else "")


@app.get("/api/config")
async def api_get_config():
    cfg = config.load()
    return {
        "tg_bot_token_set": bool(cfg.get("tg_bot_token")),
        "tg_bot_token_mask": _mask(cfg.get("tg_bot_token", "")),
        "tg_chat_id": cfg.get("tg_chat_id", ""),
        "forward_enabled": cfg.get("forward_enabled", True),
        "reply_enabled": cfg.get("reply_enabled", True),
    }


@app.put("/api/config")
async def api_put_config(request: Request):
    body = await request.json()
    cfg = config.load()
    # token 为空表示不改动（避免覆盖已存的）
    if isinstance(body.get("tg_bot_token"), str) and body["tg_bot_token"].strip():
        cfg["tg_bot_token"] = body["tg_bot_token"].strip()
    if "tg_chat_id" in body:
        cfg["tg_chat_id"] = str(body["tg_chat_id"]).strip()
    for k in ("forward_enabled", "reply_enabled"):
        if k in body:
            cfg[k] = bool(body[k])
    config.save(cfg)
    await asyncio.to_thread(sv, "restart", "bridge")   # 让桥接按新配置重启
    return {"ok": True}


# ---------------------------------------------------------------- 服务控制
@app.post("/api/service/{name}/{action}")
async def api_service(name: str, action: str):
    if name not in ("wechat", "bridge") or action not in ("restart", "start", "stop"):
        raise HTTPException(400, "不支持的操作")
    res = await asyncio.to_thread(sv, action, name)
    return {"ok": res.returncode == 0, "output": res.stdout.strip()}


# ---------------------------------------------------------------- 最近消息
@app.get("/api/messages")
async def api_messages(limit: int = 20):
    def _recent():
        col = get_collector()
        if not col.ready():
            return []
        snap = col.snapshot()
        rows = sorted(snap.values(), key=lambda m: m.time, reverse=True)[:min(max(limit, 1), 100)]
        return [{"time": m.time, "chat": m.chat_name, "is_group": m.is_group,
                 "sender": m.sender_name, "type": m.type, "text": m.text[:200]} for m in rows]
    try:
        return {"messages": await asyncio.to_thread(_recent)}
    except Exception as e:  # noqa: BLE001
        return {"messages": [], "error": str(e)}


# ---------------------------------------------------------------- 日志
@app.get("/api/logs/{name}")
async def api_logs(name: str, lines: int = 300):
    if name not in LOG_FILES:
        raise HTTPException(404)
    return {"text": await asyncio.to_thread(tail, LOG_DIR / LOG_FILES[name], min(max(lines, 10), 3000))}


# ---------------------------------------------------------------- noVNC
@app.websocket("/websockify")
async def ws_vnc(ws: WebSocket):
    if not check_token(ws.cookies.get(COOKIE)):
        await ws.close(code=1008)
        return
    subprotocols = ws.scope.get("subprotocols") or []
    await ws.accept(subprotocol="binary" if "binary" in subprotocols else None)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", VNC_PORT)
    except OSError:
        await ws.close(code=1011)
        return

    async def ws_to_tcp():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes")
            if data is None and msg.get("text") is not None:
                data = msg["text"].encode()
            if data:
                writer.write(data)
                await writer.drain()

    async def tcp_to_ws():
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await ws.send_bytes(data)

    tasks = [asyncio.create_task(ws_to_tcp()), asyncio.create_task(tcp_to_ws())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        writer.close()
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if NOVNC_DIR.exists():
    app.mount("/novnc", StaticFiles(directory=NOVNC_DIR, html=True), name="novnc")
