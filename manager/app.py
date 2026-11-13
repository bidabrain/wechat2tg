"""wechat2tg 管理面板（P0 可行性验证版）。

职责：
  * 通过 supervisord 常驻运行安卓模拟器（Xvfb + x11vnc + emulator），看门狗自动拉起
  * noVNC 网页画面（内置 WebSocket→VNC 转发，单端口 + 统一登录）
  * 设备配置：手机 / 平板（微信 Pad 模式）、内存、核数、摄像头模式
  * 安装微信、adb root
  * P0 验证：数据库解密与实时读取、界面自动化发送、扫码上传（相册 / 虚拟摄像头）
"""

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import scan
import uiops
import wxdb
from common import (ADB, ADB_SERIAL, APK_DIR, DATA, DEVICE_ENV, LOG_DIR, STATE, WX_PKG, adb, arun, log, run,
                    save_state, sh, tail)

import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATIC_DIR = Path(__file__).parent / "static"
NOVNC_DIR = Path(os.environ.get("NOVNC_DIR", "/usr/share/novnc"))
SUPERVISOR_CONF = os.environ.get("SUPERVISOR_CONF", "/etc/supervisor/wx.conf")
VNC_PORT = int(os.environ.get("VNC_PORT", "5900"))
COOKIE = "wx2tg_session"
SESSION_TTL = 7 * 86400

RUNTIME = {"boot_seen": False, "rooted": False, "job": None, "root_error": None}


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


# ---------------------------------------------------------------- supervisor / 安卓
def sv(*args: str) -> subprocess.CompletedProcess:
    return run(["supervisorctl", "-c", SUPERVISOR_CONF, *args], 90)


def sv_status() -> dict[str, str]:
    out = {}
    for line in sv("status").stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out[parts[0]] = parts[1]
    return out


def kvm_ok() -> bool:
    return os.path.exists("/dev/kvm") and os.access("/dev/kvm", os.R_OK | os.W_OK)


def android_running() -> bool:
    return sv_status().get("android:emulator") in ("RUNNING", "STARTING")


def boot_completed() -> bool:
    res = adb("shell", "getprop", "sys.boot_completed", timeout=8)
    return res.returncode == 0 and res.stdout.strip() == "1"


async def start_android():
    RUNTIME.update(boot_seen=False, rooted=False)
    uiops.reset()
    res = await asyncio.to_thread(sv, "start", "android:*")
    STATE["metrics"]["emulator_starts"] += 1
    STATE["metrics"]["since"] = STATE["metrics"]["since"] or int(time.time())
    save_state()
    return res


async def stop_android():
    await arun([ADB, "-s", ADB_SERIAL, "emu", "kill"], 15)
    for _ in range(30):
        if not android_running():
            break
        await asyncio.sleep(1)
    res = await asyncio.to_thread(sv, "stop", "android:*")
    RUNTIME.update(boot_seen=False, rooted=False)
    uiops.reset()
    return res


def ensure_root() -> bool:
    """google_apis 镜像支持 adb root；之后 adb shell 即为 root，可读取 /data/data/com.tencent.mm。"""
    if adb("shell", "id", "-u", timeout=8).stdout.strip() == "0":
        RUNTIME["rooted"] = True
        return True
    res = adb("root", timeout=20)
    run([ADB, "-s", ADB_SERIAL, "wait-for-device"], 40)
    for _ in range(20):
        if adb("shell", "id", "-u", timeout=8).stdout.strip() == "0":
            RUNTIME.update(rooted=True, root_error=None)
            uiops.reset()
            return True
        time.sleep(1)
    RUNTIME["root_error"] = (res.stdout + res.stderr).strip()[-200:] or "adb root 失败（是否为 google_apis 镜像？）"
    return False


def device_props() -> dict:
    out = {}
    for key, prop in (("abilist", "ro.product.cpu.abilist"), ("native_bridge", "ro.dalvik.vm.native.bridge"),
                      ("release", "ro.build.version.release"), ("sdk", "ro.build.version.sdk"),
                      ("build_type", "ro.build.type")):
        out[key] = adb("shell", "getprop", prop, timeout=8).stdout.strip()
    size = adb("shell", "wm", "size", timeout=8).stdout.strip().splitlines()
    dens = adb("shell", "wm", "density", timeout=8).stdout.strip().splitlines()
    out["wm_size"] = size[-1].split(":")[-1].strip() if size else ""
    out["wm_density"] = dens[-1].split(":")[-1].strip() if dens else ""
    return out


# ---------------------------------------------------------------- 设备配置（device.env）
DEVICE_DEFAULTS = {"DEVICE_PROFILE": "phone", "EMU_RAM_MB": "4096", "EMU_CORES": "4", "CAMERA_BACK": "none",
                   "EMU_WIDTH": "", "EMU_HEIGHT": "", "EMU_DENSITY": "", "WRITABLE_SYSTEM": "0"}


def read_device() -> dict:
    cfg = dict(DEVICE_DEFAULTS)
    if DEVICE_ENV.exists():
        for line in DEVICE_ENV.read_text("utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                if k.strip() in cfg:
                    cfg[k.strip()] = v.strip().strip('"')
    return cfg


def write_device(body: dict) -> dict:
    cfg = read_device()
    prof = str(body.get("DEVICE_PROFILE", cfg["DEVICE_PROFILE"]))
    if prof not in ("phone", "tablet"):
        raise HTTPException(400, "机型只能是 phone 或 tablet")
    cam = str(body.get("CAMERA_BACK", cfg["CAMERA_BACK"]))
    if cam not in ("none", "virtualscene", "webcam0"):
        raise HTTPException(400, "摄像头模式只能是 none / virtualscene / webcam0")
    cfg["DEVICE_PROFILE"], cfg["CAMERA_BACK"] = prof, cam
    cfg["WRITABLE_SYSTEM"] = "1" if str(body.get("WRITABLE_SYSTEM", cfg["WRITABLE_SYSTEM"])) in ("1", "true", "True") else "0"
    for key, lo, hi in (("EMU_RAM_MB", 2048, 16384), ("EMU_CORES", 1, 16),
                        ("EMU_WIDTH", 480, 3000), ("EMU_HEIGHT", 480, 3000), ("EMU_DENSITY", 120, 640)):
        val = str(body.get(key, cfg[key]) or "").strip()
        if val == "" and key in ("EMU_WIDTH", "EMU_HEIGHT", "EMU_DENSITY"):
            cfg[key] = ""
            continue
        if not val.isdigit() or not lo <= int(val) <= hi:
            raise HTTPException(400, f"{key} 必须是 {lo}~{hi} 之间的整数")
        cfg[key] = val
    lines = ["# 由 wechat2tg 管理面板生成；修改后需重启模拟器生效"]
    lines += [f"{k}={v}" for k, v in cfg.items() if v != ""]
    DEVICE_ENV.write_text("\n".join(lines) + "\n", "utf-8")
    return cfg


# ---------------------------------------------------------------- 微信
def wechat_info() -> dict:
    res = adb("shell", "dumpsys", "package", WX_PKG, timeout=20)
    info = {"installed": False, "version": None, "abi": None}
    for line in res.stdout.splitlines():
        line = line.strip()
        if line.startswith("versionName=") and not info["version"]:
            info["version"] = line.split("=", 1)[1]
            info["installed"] = True
        elif line.startswith("primaryCpuAbi=") and not info["abi"]:
            info["abi"] = line.split("=", 1)[1]
    if info["installed"]:
        info["running"] = bool(adb("shell", "pidof", WX_PKG, timeout=8).stdout.strip())
        if RUNTIME["rooted"]:
            info["uins"] = wxdb.read_uins()
            info["logged_in"] = bool(info["uins"])
    return info


async def launch_wechat():
    await arun([ADB, "-s", ADB_SERIAL, "shell", "monkey", "-p", WX_PKG, "-c", "android.intent.category.LAUNCHER", "1"], 20)


async def install_apk(path: Path, name: str) -> bool:
    RUNTIME["job"] = {"name": name, "status": "running", "message": "正在安装到模拟器…"}
    res = await arun([ADB, "-s", ADB_SERIAL, "install", "-r", "-g", str(path)], 900)
    detail = (res.stdout + res.stderr).strip()[-500:]
    if res.returncode == 0 and "Success" in res.stdout:
        info = await asyncio.to_thread(wechat_info)
        RUNTIME["job"] = {"name": name, "status": "done",
                          "message": f"安装成功：版本 {info.get('version') or '?'}，ABI {info.get('abi') or '?'}"}
        return True
    if "NO_MATCHING_ABIS" in detail:
        msg = "安装失败：没有匹配的 ABI。该系统镜像不支持 ARM 指令转译（需要 API ≥ 30 的 x86_64 镜像），或 APK 只含 32 位 ARM。"
    elif "VERSION_DOWNGRADE" in detail:
        msg = "安装失败：APK 比已安装的版本旧，不能降级。"
    elif "UPDATE_INCOMPATIBLE" in detail:
        msg = "安装失败：签名与已安装版本不一致，请使用官方 APK。"
    else:
        msg = "安装失败：" + detail
    RUNTIME["job"] = {"name": name, "status": "error", "message": msg}
    return False


async def download_and_install(url: str):
    dest = APK_DIR / "wechat.apk"
    RUNTIME["job"] = {"name": "微信", "status": "running", "message": "正在下载…"}

    def _download():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14)"})
        tmp = dest.with_suffix(".part")
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
                RUNTIME["job"]["message"] = (f"正在下载… {got * 100 // total}%" if total
                                             else f"正在下载… {got // 1048576} MB")
        tmp.replace(dest)

    try:
        await asyncio.to_thread(_download)
    except Exception as e:  # noqa: BLE001
        RUNTIME["job"] = {"name": "微信", "status": "error", "message": f"下载失败：{e}"}
        return
    await install_apk(dest, "微信")


# ---------------------------------------------------------------- 看门狗
async def monitor_loop():
    last_cleanup = 0
    while True:
        try:
            await monitor_tick()
            if time.time() - last_cleanup > 300 and RUNTIME["boot_seen"]:
                last_cleanup = time.time()
                await asyncio.to_thread(scan.cleanup_gallery)
        except Exception as e:  # noqa: BLE001
            log.exception("看门狗出错：%s", e)
        await asyncio.sleep(10)


async def monitor_tick():
    m = STATE["metrics"]
    if not await asyncio.to_thread(android_running):
        RUNTIME.update(boot_seen=False, rooted=False)
        if STATE.get("android_desired") and kvm_ok() and not (RUNTIME["job"] or {}).get("status") == "restarting":
            log.info("模拟器未运行，看门狗自动启动")
            await start_android()
        return
    if not await asyncio.to_thread(boot_completed):
        return
    if not RUNTIME["boot_seen"]:
        RUNTIME["boot_seen"] = True
        m["boots"] += 1
        m["last_boot"] = int(time.time())
        save_state()
        await asyncio.to_thread(ensure_root)
        # 关闭微信的电池优化，减少后台被杀
        await asyncio.to_thread(sh, f"dumpsys deviceidle whitelist +{WX_PKG}", 10)
        await launch_wechat()
        return
    if not RUNTIME["rooted"]:
        await asyncio.to_thread(ensure_root)
    if STATE.get("wechat_watchdog"):
        installed = "package:" + WX_PKG in (await arun([ADB, "-s", ADB_SERIAL, "shell", "pm", "list", "packages", WX_PKG], 15)).stdout
        running = bool((await arun([ADB, "-s", ADB_SERIAL, "shell", "pidof", WX_PKG], 8)).stdout.strip())
        if installed and not running and not (RUNTIME["job"] and RUNTIME["job"]["status"] == "running"):
            log.warning("微信进程不在，看门狗重新拉起")
            m["wechat_restarts"] += 1
            m["last_wechat_restart"] = int(time.time())
            save_state()
            await launch_wechat()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(monitor_loop())
    yield
    task.cancel()
    wxdb.stop_watch()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path in PUBLIC_PATHS or check_token(request.cookies.get(COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    return RedirectResponse("/login")


# ---------------------------------------------------------------- 页面 & 登录
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
    emu = st.get("android:emulator", "STOPPED")
    booted, props, wx = False, {}, {}
    if emu == "RUNNING":
        booted = await asyncio.to_thread(boot_completed)
        if booted:
            props = await asyncio.to_thread(device_props)
            wx = await asyncio.to_thread(wechat_info)
    return {
        "kvm": kvm_ok(),
        "android": {"state": emu, "booted": booted, "desired": STATE.get("android_desired"),
                    "rooted": RUNTIME["rooted"], "root_error": RUNTIME["root_error"],
                    "vnc": st.get("android:vnc"), "xvfb": st.get("android:xvfb"), "props": props},
        "device": read_device(),
        "wechat": wx,
        "wechat_watchdog": STATE.get("wechat_watchdog"),
        "wx": {k: v for k, v in STATE["wx"].items()},
        "metrics": STATE["metrics"],
        "watch": wxdb.watch_status() | {"feed": None},
        "job": RUNTIME["job"],
        "now": int(time.time()),
    }


# ---------------------------------------------------------------- 安卓
@app.post("/api/android/{action}")
async def api_android(action: str):
    if action in ("start", "restart") and not kvm_ok():
        raise HTTPException(400, "容器内没有可用的 /dev/kvm，无法运行安卓模拟器。")
    if action == "start":
        STATE["android_desired"] = True
        save_state()
        res = await start_android()
    elif action == "stop":
        STATE["android_desired"] = False
        save_state()
        res = await stop_android()
    elif action == "restart":
        STATE["android_desired"] = True
        save_state()
        RUNTIME["job"] = {"name": "模拟器", "status": "restarting", "message": "正在重启模拟器…"}
        try:
            await stop_android()
            res = await start_android()
        finally:
            RUNTIME["job"] = None
    elif action == "root":
        ok = await asyncio.to_thread(ensure_root)
        return {"ok": ok, "error": RUNTIME["root_error"]}
    else:
        raise HTTPException(404)
    return {"ok": True, "output": (res.stdout + res.stderr).strip()}


@app.post("/api/key/{key}")
async def api_key(key: str):
    codes = {"home": "KEYCODE_HOME", "back": "KEYCODE_BACK", "recent": "KEYCODE_APP_SWITCH",
             "wake": "KEYCODE_WAKEUP", "enter": "KEYCODE_ENTER", "del": "KEYCODE_DEL"}
    if key == "wechat":
        await launch_wechat()
        return {"ok": True}
    if key not in codes:
        raise HTTPException(400, "未知按键")
    res = await arun([ADB, "-s", ADB_SERIAL, "shell", "input", "keyevent", codes[key]], 15)
    return {"ok": res.returncode == 0}


@app.post("/api/text")
async def api_text(request: Request):
    """把文字输入到模拟器当前输入框；含中文时走 uiautomator2 输入法。"""
    text = str((await request.json()).get("text", ""))
    if not text:
        return {"ok": True}
    if re.fullmatch(r"[\x20-\x7e]+", text):
        escaped = "".join("%s" if c == " " else ("\\" + c if c in "\\'\"`$&|;<>()*?!#~" else c) for c in text)
        res = await arun([ADB, "-s", ADB_SERIAL, "shell", "input", "text", escaped], 30)
        return {"ok": res.returncode == 0}
    try:
        return await asyncio.to_thread(uiops.input_text, text)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"输入失败：{e}") from e


@app.get("/api/screenshot")
async def api_screenshot():
    res = await asyncio.to_thread(
        lambda: subprocess.run([ADB, "-s", ADB_SERIAL, "exec-out", "screencap", "-p"], capture_output=True, timeout=15))
    if res.returncode != 0 or not res.stdout:
        raise HTTPException(400, "截图失败")
    return Response(res.stdout, media_type="image/png")


# ---------------------------------------------------------------- 设备配置
@app.get("/api/device")
async def api_get_device():
    return read_device()


@app.put("/api/device")
async def api_put_device(request: Request):
    body = await request.json()
    cfg = write_device(body)
    if body.get("restart"):
        asyncio.create_task(api_android("restart"))
    return {"ok": True, "device": cfg}


@app.put("/api/settings")
async def api_settings(request: Request):
    body = await request.json()
    if "wechat_watchdog" in body:
        STATE["wechat_watchdog"] = bool(body["wechat_watchdog"])
    save_state()
    return {"ok": True}


# ---------------------------------------------------------------- 微信安装
def _require_booted():
    if not boot_completed():
        raise HTTPException(400, "安卓模拟器尚未启动完成")


def _require_no_job():
    if RUNTIME["job"] and RUNTIME["job"]["status"] in ("running", "restarting"):
        raise HTTPException(409, "已有任务进行中")


@app.post("/api/wechat/upload-apk")
async def api_upload_apk(file: UploadFile = File(...)):
    await asyncio.to_thread(_require_booted)
    _require_no_job()
    if not (file.filename or "").lower().endswith(".apk"):
        raise HTTPException(400, "请上传 .apk 文件")
    dest = APK_DIR / "uploaded.apk"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    asyncio.create_task(install_apk(dest, file.filename))
    return {"ok": True}


@app.post("/api/wechat/download")
async def api_download(request: Request):
    url = str((await request.json()).get("url", "")).strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "请输入以 http(s):// 开头的 APK 下载地址")
    await asyncio.to_thread(_require_booted)
    _require_no_job()
    asyncio.create_task(download_and_install(url))
    return {"ok": True}


# ---------------------------------------------------------------- P0：数据库
def _db_call(fn, *args):
    try:
        return fn(*args)
    except wxdb.DBError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/p0/db/probe")
async def api_db_probe(request: Request):
    body = await request.json()
    await asyncio.to_thread(_require_booted)
    if not await asyncio.to_thread(ensure_root):
        raise HTTPException(400, f"adb root 失败：{RUNTIME['root_error']}")
    return await asyncio.to_thread(wxdb.probe, str(body.get("imei", "")).strip())


@app.get("/api/p0/db/messages")
async def api_db_messages(limit: int = 30):
    return {"items": await asyncio.to_thread(_db_call, wxdb.recent_messages, min(max(limit, 1), 200))}


@app.get("/api/p0/db/conversations")
async def api_db_conversations(limit: int = 30):
    return {"items": await asyncio.to_thread(_db_call, wxdb.conversations, min(max(limit, 1), 200))}


@app.post("/api/p0/db/watch")
async def api_db_watch(request: Request):
    body = await request.json()
    if body.get("on"):
        try:
            wxdb.start_watch(float(body.get("interval", 2)))
        except wxdb.DBError as e:
            raise HTTPException(400, str(e)) from e
    else:
        wxdb.stop_watch()
    return wxdb.watch_status()


@app.get("/api/p0/db/watch")
async def api_db_watch_status():
    return wxdb.watch_status()


# ---------------------------------------------------------------- P0：发送
@app.post("/api/p0/send")
async def api_send(request: Request):
    body = await request.json()
    talker, text = str(body.get("talker", "")).strip(), str(body.get("text", ""))
    if not talker or not text:
        raise HTTPException(400, "请填写会话 ID（wxid 或 xxx@chatroom）和要发送的文字")
    await asyncio.to_thread(_require_booted)
    await asyncio.to_thread(ensure_root)
    return await asyncio.to_thread(uiops.send_text, talker, text)


# ---------------------------------------------------------------- 扫码上传
@app.post("/api/scan/upload")
async def api_scan_upload(file: UploadFile = File(...)):
    data = await file.read()
    if len(data) > 20 * 1048576:
        raise HTTPException(400, "图片太大（上限 20MB）")
    try:
        return await asyncio.to_thread(scan.save_upload, data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/scan/image/{uid}")
async def api_scan_image(uid: str, regenerated: bool = False):
    try:
        img = await asyncio.to_thread(scan.resolve, uid, regenerated)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    import io
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.post("/api/scan/push")
async def api_scan_push(request: Request):
    body = await request.json()
    await asyncio.to_thread(_require_booted)
    await asyncio.to_thread(ensure_root)
    try:
        res = await asyncio.to_thread(scan.push_to_gallery, str(body.get("id")), bool(body.get("regenerated")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if body.get("open_scanner"):
        res["scanner"] = await asyncio.to_thread(uiops.open_scanner, bool(body.get("auto_album", True)))
    return res


@app.post("/api/scan/camera")
async def api_scan_camera(request: Request):
    body = await request.json()
    try:
        res = await asyncio.to_thread(scan.set_camera_poster, str(body.get("id")), bool(body.get("regenerated")))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    cfg = read_device()
    if cfg["CAMERA_BACK"] != "virtualscene":
        write_device({"CAMERA_BACK": "virtualscene"})
    if body.get("restart"):
        asyncio.create_task(api_android("restart"))
    return res | {"camera": "virtualscene", "restarting": bool(body.get("restart"))}


@app.post("/api/scan/open-scanner")
async def api_open_scanner(request: Request):
    body = await request.json()
    await asyncio.to_thread(_require_booted)
    return await asyncio.to_thread(uiops.open_scanner, bool(body.get("auto_album")))


# ---------------------------------------------------------------- 日志
LOG_FILES = {"manager": "manager.log", "emulator": "emulator.log", "vnc": "vnc.log", "supervisord": "supervisord.log"}


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
