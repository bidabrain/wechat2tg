"""管理面板公共工具：路径、命令执行、adb、持久状态。"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("manager")

DATA = Path(os.environ.get("DATA_DIR", "/data"))
LOG_DIR = DATA / "logs"
APK_DIR = DATA / "apk"
SCAN_DIR = DATA / "scan"
P0_DIR = DATA / "p0"
ANDROID_DIR = DATA / "android"
STATE_PATH = DATA / "manager.json"
DEVICE_ENV = ANDROID_DIR / "device.env"

ADB = os.environ.get("ADB_BIN", "adb")
ADB_SERIAL = os.environ.get("ADB_SERIAL", "emulator-5554")
WX_PKG = "com.tencent.mm"

for d in (DATA, LOG_DIR, APK_DIR, SCAN_DIR, P0_DIR, ANDROID_DIR):
    d.mkdir(parents=True, exist_ok=True)


def run(cmd: list[str], timeout: float = 30, input_text: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=input_text,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timeout")


async def arun(cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
    return await asyncio.to_thread(run, cmd, timeout)


def adb(*args: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return run([ADB, "-s", ADB_SERIAL, *args], timeout)


def sh(command: str, timeout: float = 30) -> subprocess.CompletedProcess:
    """在模拟器里执行 shell 命令（adb root 之后即为 root 身份）。"""
    return adb("shell", command, timeout=timeout)


def adb_bytes(*args: str, timeout: float = 30) -> bytes:
    try:
        res = subprocess.run([ADB, "-s", ADB_SERIAL, *args], capture_output=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return b""
    return res.stdout if res.returncode == 0 else b""


def sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


# ---------------------------------------------------------------- 持久状态
DEFAULT_STATE = {
    "android_desired": True,   # 模拟器应当常驻运行（点「关机」后为 False，避免看门狗再拉起）
    "wechat_watchdog": True,   # 微信进程退出后自动重新拉起
    "wx": {"imei": "", "key": "", "uin": "", "db": ""},
    "metrics": {"emulator_starts": 0, "boots": 0, "wechat_restarts": 0, "since": 0,
                "last_boot": 0, "last_wechat_restart": 0},
}


def load_state() -> dict:
    st = json.loads(json.dumps(DEFAULT_STATE))
    if STATE_PATH.exists():
        try:
            saved = json.loads(STATE_PATH.read_text("utf-8"))
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(st.get(k), dict):
                    st[k].update(v)
                else:
                    st[k] = v
        except Exception as e:  # noqa: BLE001
            log.warning("读取 manager.json 失败：%s", e)
    return st


STATE = load_state()


def save_state():
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(STATE_PATH)


def tail(path: Path, lines: int = 300) -> str:
    if not path.exists():
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 256 * 1024))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-lines:])
