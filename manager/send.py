"""TG → 微信 回发：用 xdotool 驱动 Linux 微信界面发送消息。

Linux 桌面版没有发送 API，只能模拟操作：搜索联系人 → 打开会话 → 粘贴文本 → 回车。
中文通过剪贴板（xclip）粘贴，xdotool type 无法可靠输入 CJK。

坐标用窗口相对坐标（xdotool --window），窗口会被先规整为固定尺寸，避免布局漂移。
"""
from __future__ import annotations

import subprocess
import threading
import time

DISPLAY = ":1"
WIN_W, WIN_H = 1280, 800
# 窗口相对坐标（基于规整后的 1280x800 布局，已实测校准）
SEARCH_XY = (168, 60)      # 搜索框
INPUT_XY = (768, 700)      # 聊天输入框
_LOCK = threading.Lock()

# 【已知限制 / 待加固】定位到「指定会话」是本方案最脆的一环：
#   微信的搜索结果排序（联系人 vs 聊天记录）和左侧会话列表都会动态重排，
#   纯固定坐标无法稳定命中目标会话。可靠的只有「向当前已打开的会话发送」。
#   后续加固方向（择一）：
#     a) OCR 下拉结果（微信自带 /opt/wechat/wxocr 或 tesseract）定位「联系人」行坐标后点击；
#     b) 开启 Qt 无障碍(AT-SPI)，按名称查找并点击联系人节点；
#     c) 桥接只支持「回复最近/当前会话」，目标会话由采集侧保证已打开。
# send_to_open() 是可靠原语；open_chat() 目前是尽力而为。


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


def open_chat(target: str) -> dict:
    """尽力而为：搜索 target 并打开最佳匹配（见文件顶部「已知限制」）。"""
    with _LOCK:
        win = _wechat_win()
        if not win:
            return {"ok": False, "step": "find_window", "msg": "找不到微信窗口"}
        _normalize(win)
        _click(win, SEARCH_XY)
        _x("key", "--window", win, "ctrl+a")
        _x("key", "--window", win, "Delete")
        _clip(target)
        time.sleep(0.1)
        _x("key", "--window", win, "ctrl+v")
        time.sleep(1.0)
        _x("key", "--window", win, "Down")
        time.sleep(0.3)
        _x("key", "--window", win, "Return")
        time.sleep(0.6)
        return {"ok": True, "step": "opened", "msg": f"已尝试打开「{target}」"}


def send_text(target: str, text: str) -> dict:
    """打开 target 会话并发送（open_chat 为尽力而为，务必结合采集侧校验目标正确）。"""
    r = open_chat(target)
    if not r["ok"]:
        return r
    return send_to_open(text)


if __name__ == "__main__":  # 自测：python send.py searchon "你好"
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else "searchon"
    msg = sys.argv[2] if len(sys.argv) > 2 else f"wechat2tg send.py 测试 {time.strftime('%H:%M:%S')}"
    print(send_text(tgt, msg))
