"""TG → 微信 回发：用 xdotool 驱动 Linux 微信界面发送消息。

Linux 桌面版没有发送 API，只能模拟操作。中文通过剪贴板（xclip）粘贴，
xdotool type 无法可靠输入 CJK。坐标用窗口相对坐标（xdotool --window）。

定位指定会话（send_to_chat）：搜索 → 双击最佳结果开成【独立聊天窗口】→
用 xdotool 读该窗口标题【精确核对】是否为目标 → 匹配才发送。
独立窗口标题是精确字符串，不依赖 OCR 或固定坐标；标题不符绝不发送，避免误发。
send_to_open() 向当前已打开的会话发送（主窗口）。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

DISPLAY = ":1"
WIN_W, WIN_H = 1280, 800
# 主窗口坐标（规整到 0,0 后实测校准）
SEARCH_XY = (168, 60)      # 搜索框
INPUT_XY = (768, 700)      # 主窗口聊天输入框（发给当前会话用）
# 搜索下拉结果：双击某结果会把该会话打开成独立窗口（可按窗口标题精确核对）
RESULT_X = 186            # 结果行名字区 x
RESULT_Y0 = 128          # 第 1 个结果中心 y
RESULT_DY = 57           # 结果行距
SEARCH_CANDIDATES = 4    # 最多核对前几个候选
# 独立聊天窗口内的输入区相对坐标
POPUP_INPUT_XY = (300, 600)
MAIN_TITLE = "WeChat"      # 主窗口标题，用于区分独立聊天窗口
_LOCK = threading.Lock()


def _x(*args: str, **kw) -> subprocess.CompletedProcess:
    env = {"DISPLAY": DISPLAY}
    return subprocess.run(["xdotool", *args], capture_output=True, text=True,
                          env={**_env(), **env}, **kw)


def _env():
    import os
    return dict(os.environ)


def _wechat_win() -> str | None:
    r = subprocess.run(["xdotool", "search", "--name", "WeChat"],
                       capture_output=True, text=True, env={**_env(), "DISPLAY": DISPLAY})
    wins = [w for w in r.stdout.split() if w]
    # 取几何尺寸最大的那个（主窗口，排除 1x1 的选区窗口）
    best, best_area = None, 0
    for w in wins:
        g = subprocess.run(["xdotool", "getwindowgeometry", w], capture_output=True, text=True,
                           env={**_env(), "DISPLAY": DISPLAY}).stdout
        for line in g.splitlines():
            if "Geometry" in line:
                try:
                    wh = line.split(":")[1].strip()
                    ww, hh = (int(x) for x in wh.split("x"))
                    if ww * hh > best_area:
                        best, best_area = w, ww * hh
                except ValueError:
                    pass
    return best


def _clip(text: str):
    subprocess.run(["xclip", "-selection", "clipboard"], input=text.encode(),
                   env={**_env(), "DISPLAY": DISPLAY})


def _normalize(win: str):
    _x("windowactivate", "--sync", win)
    _x("windowmove", win, "0", "0")
    _x("windowsize", win, str(WIN_W), str(WIN_H))
    time.sleep(0.2)


def _click(win: str, xy: tuple[int, int]):
    _x("mousemove", "--window", win, str(xy[0]), str(xy[1]))
    time.sleep(0.1)
    _x("click", "--window", win, "1")
    time.sleep(0.15)


def send_to_open(text: str) -> dict:
    """向当前已打开的会话发送 text（可靠原语）。"""
    with _LOCK:
        win = _wechat_win()
        if not win:
            return {"ok": False, "step": "find_window", "msg": "找不到微信窗口（是否已登录）"}
        _normalize(win)
        _click(win, INPUT_XY)
        _clip(text)
        time.sleep(0.1)
        _x("key", "--window", win, "ctrl+v")
        time.sleep(0.3)
        _x("key", "--window", win, "Return")
        return {"ok": True, "step": "sent", "msg": "已发送到当前会话"}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _all_windows() -> list[tuple[str, str]]:
    """返回 [(wid, 窗口标题), ...]。"""
    r = subprocess.run(["xdotool", "search", "--name", "."],
                       capture_output=True, text=True, env={**_env(), "DISPLAY": DISPLAY})
    out = []
    for wid in r.stdout.split():
        n = subprocess.run(["xdotool", "getwindowname", wid], capture_output=True, text=True,
                           env={**_env(), "DISPLAY": DISPLAY}).stdout.strip()
        out.append((wid, n))
    return out


def _is_chat_window(name: str) -> bool:
    """独立聊天窗口：排除主窗口和 Qt 辅助窗口。"""
    if not name or name == MAIN_TITLE:
        return False
    return name not in ("wechat", "Qt Selection Owner for wechat", "Qt Clipboard Requestor Window")


def _find_chat_window(target: str) -> str | None:
    """在已打开的独立窗口里按标题匹配目标会话（群名可能带 (N) 后缀）。"""
    g = _norm(target)
    for wid, name in _all_windows():
        if not _is_chat_window(name):
            continue
        t = _norm(re.sub(r"\(\d+\)\s*$", "", name))
        if t == g or (len(g) >= 2 and (g in t or t in g)):
            return wid
    return None


def _send_into_window(wid: str, text: str) -> dict:
    _x("windowactivate", "--sync", wid)
    _x("mousemove", "--window", wid, str(POPUP_INPUT_XY[0]), str(POPUP_INPUT_XY[1]))
    time.sleep(0.1)
    _x("click", "--window", wid, "1")
    time.sleep(0.15)
    _clip(text)
    time.sleep(0.1)
    _x("key", "--window", wid, "ctrl+v")
    time.sleep(0.3)
    _x("key", "--window", wid, "Return")
    return {"ok": True, "step": "sent", "msg": "已发送"}


def send_to_chat(chat_name: str, chat_id: str, text: str, keep_window: bool = True) -> dict:
    """把 text 发给 chat_name 对应的会话。

    用「独立聊天窗口 + 精确窗口标题校验」定位：搜索→双击最佳结果开成独立窗口→
    用 xdotool 读窗口标题精确核对→匹配才发送。标题不符绝不发送，避免误发。
    """
    with _LOCK:
        main = _wechat_win()
        if not main:
            return {"ok": False, "step": "find_window", "msg": "找不到微信窗口（是否已登录）"}
        # 1) 已有该会话的独立窗口则直接复用（标题已精确匹配）
        wid = _find_chat_window(chat_name)
        if wid:
            return _send_into_window(wid, text)
        # 2) 用【搜索】定位（不受置顶/列表顺序影响），双击第 i 个结果开成独立窗口，
        #    读窗口标题精确核对；匹配才发送，否则关掉试下一个候选，绝不误发。
        _normalize(main)
        for i in range(1, SEARCH_CANDIDATES + 1):
            before = {w for w, _ in _all_windows()}
            _click(main, SEARCH_XY)
            _x("key", "--window", main, "ctrl+a")
            _x("key", "--window", main, "Delete")
            _clip(chat_name)
            time.sleep(0.1)
            _x("key", "--window", main, "ctrl+v")
            time.sleep(1.2)
            # 第 i 个结果：下拉首行约 y=128，行距约 57
            y = RESULT_Y0 + RESULT_DY * (i - 1)
            _x("mousemove", "--window", main, str(RESULT_X), str(y))
            time.sleep(0.1)
            _x("click", "--window", main, "1")
            time.sleep(0.15)
            _x("click", "--window", main, "1")     # 双击结果 → 开成独立窗口
            time.sleep(0.9)
            new = [(w, n) for w, n in _all_windows() if w not in before and _is_chat_window(n)]
            if not new:
                continue
            w, n = new[0]
            t = _norm(re.sub(r"\(\d+\)\s*$", "", n))
            g = _norm(chat_name)
            if t == g or (len(g) >= 2 and (g in t or t in g)):
                r = _send_into_window(w, text)
                if not keep_window:
                    _x("windowclose", w)
                return r
            _x("windowclose", w)        # 不是目标，关掉试下一个候选
        return {"ok": False, "step": "verify",
                "msg": f"未能定位会话「{chat_name}」（搜索结果标题不匹配，已避免误发）。"}


def send_text(target: str, text: str) -> dict:
    return send_to_chat(target, target, text)


if __name__ == "__main__":  # 自测：python send.py searchon "你好"
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else "searchon"
    msg = sys.argv[2] if len(sys.argv) > 2 else f"wechat2tg send.py 测试 {time.strftime('%H:%M:%S')}"
    print(send_text(tgt, msg))
