"""TG → 微信 回发：用 xdotool 驱动 Linux 微信界面发送消息。

定位会话（send_to_chat）：在搜索框输入目标名 → 单击下拉里的匹配项，右侧即打开该会话
→ scrot 截标题栏 + tesseract OCR 核对标题是否为目标 → 匹配才发送。标题不符绝不发送。

关键：微信是 CEF 应用，`xdotool click --window` 的合成点击**不被识别**；必须用
**绝对坐标 mousemove + 普通 click**（物理移动光标再点）。窗口先规整到 (0,0)，
这样绝对坐标 == 窗口内坐标。中文用剪贴板（xclip）粘贴，xdotool type 不能可靠输入 CJK。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import threading
import time

DISPLAY = ":1"
WIN_W, WIN_H = 1280, 800
# 规整到 (0,0) 后的绝对坐标（实测校准）
SEARCH_XY = (168, 60)      # 搜索框
RESULT_XY = (165, 141)     # 搜索下拉第一个匹配项（联系人/会话）
INPUT_XY = (768, 700)      # 聊天输入框
TITLE_REGION = (326, 45, 320, 32)   # 聊天标题栏（OCR 核对用）：x, y, 宽, 高
_LOCK = threading.Lock()


def _env() -> dict:
    return {**os.environ, "DISPLAY": DISPLAY}


def _x(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["xdotool", *args], capture_output=True, text=True, env=_env())


def _wechat_win() -> str | None:
    """主窗口（标题 WeChat，几何面积最大的那个，排除 1x1 辅助窗口）。"""
    r = _x("search", "--name", "WeChat")
    best, best_area = None, 0
    for w in r.stdout.split():
        g = _x("getwindowgeometry", w).stdout
        for line in g.splitlines():
            if "Geometry" in line:
                try:
                    ww, hh = (int(x) for x in line.split(":")[1].strip().split("x"))
                    if ww * hh > best_area:
                        best, best_area = w, ww * hh
                except ValueError:
                    pass
    return best


def _normalize(win: str):
    _x("windowactivate", "--sync", win)
    _x("windowmove", win, "0", "0")
    _x("windowsize", win, str(WIN_W), str(WIN_H))
    time.sleep(0.2)


def _click(x: int, y: int):
    """绝对坐标：物理移动光标再点（CEF 认这种，不认 click --window）。"""
    _x("mousemove", str(x), str(y))
    time.sleep(0.12)
    _x("click", "1")
    time.sleep(0.15)


def _clip(text: str):
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(), env=_env())


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _ocr_title() -> str:
    """OCR 当前会话标题栏，去掉群成员数后缀。"""
    fd, png = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        r = subprocess.run(["scrot", "-a", ",".join(map(str, TITLE_REGION)), "-o", png],
                           capture_output=True, env=_env())
        if r.returncode != 0:
            return ""
        out = subprocess.run(["tesseract", png, "-", "-l", "chi_sim+eng"],
                             capture_output=True, text=True, env=_env())
        text = out.stdout.strip()
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    return _norm(re.sub(r"\(\d+\)\s*$", "", text))


def send_to_open(text: str) -> dict:
    """向当前已打开的会话发送 text（主窗口输入框）。"""
    win = _wechat_win()
    if not win:
        return {"ok": False, "step": "find_window", "msg": "找不到微信窗口（是否已登录）"}
    _normalize(win)
    _click(*INPUT_XY)
    _clip(text)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(0.3)
    _x("key", "Return")
    return {"ok": True, "step": "sent", "msg": "已发送到当前会话"}


def _open_chat(win: str, chat_name: str) -> bool:
    """搜索并单击匹配项打开会话，OCR 标题核对是否为目标。"""
    _click(*SEARCH_XY)
    _x("key", "ctrl+a")
    _x("key", "Delete")
    _clip(chat_name)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(1.6)                 # 等下拉结果稳定
    _click(*RESULT_XY)             # 单击第一个匹配项 → 右侧打开该会话
    time.sleep(0.8)
    title = _ocr_title()
    g = _norm(chat_name)
    return bool(title) and (title == g or (len(g) >= 2 and (g in title or title in g)))


def send_to_chat(chat_name: str, chat_id: str, text: str) -> dict:
    """打开 chat_name 对应会话（OCR 标题核对）并发送；核对不通过绝不发送，避免误发。"""
    with _LOCK:
        win = _wechat_win()
        if not win:
            return {"ok": False, "step": "find_window", "msg": "找不到微信窗口（是否已登录）"}
        _normalize(win)
        if not _open_chat(win, chat_name):
            return {"ok": False, "step": "verify",
                    "msg": f"未能定位会话「{chat_name}」（标题核对不通过，已避免误发）"}
        return send_to_open(text)


def _clip_image(path: str):
    subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-i", path], env=_env())


def _paste_send():
    _click(*INPUT_XY)
    time.sleep(0.2)
    _x("key", "ctrl+v")
    time.sleep(0.8)
    _x("key", "Return")


def send_image_to_chat(chat_name: str, chat_id: str, image_path: str) -> dict:
    """打开会话（OCR 标题核对）并把图片经剪贴板粘贴发送；核对不通过不发送。"""
    with _LOCK:
        win = _wechat_win()
        if not win:
            return {"ok": False, "step": "find_window", "msg": "找不到微信窗口"}
        _normalize(win)
        if not _open_chat(win, chat_name):
            return {"ok": False, "step": "verify", "msg": f"未能定位会话「{chat_name}」（已避免误发）"}
        _clip_image(image_path)
        time.sleep(0.3)
        _paste_send()
        return {"ok": True, "step": "sent", "msg": "已发送图片"}


def send_text(target: str, text: str) -> dict:
    return send_to_chat(target, target, text)


if __name__ == "__main__":  # 自测：python send.py searchon "你好"
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else "searchon"
    msg = sys.argv[2] if len(sys.argv) > 2 else f"wechat2tg send.py 测试 {time.strftime('%H:%M:%S')}"
    print(send_text(tgt, msg))
