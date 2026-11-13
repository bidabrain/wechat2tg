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


# 危险区：左侧边栏/头像列 (x<62) 会误触个人资料→退出登录、切到联系人页；
# 顶部标题栏 (y<26) 有最小化/关闭。任何 click 落在这些区域一律拒绝。
SAFE_MIN_X = 62
SAFE_MIN_Y = 26


def _pointer() -> tuple[int, int]:
    out = subprocess.run(["xdotool", "getmouselocation", "--shell"],
                         capture_output=True, text=True, env=_env()).stdout
    x = y = 0
    for line in out.splitlines():
        if line.startswith("X="):
            x = int(line[2:])
        elif line.startswith("Y="):
            y = int(line[2:])
    return x, y


def _x(*args: str) -> subprocess.CompletedProcess:
    # 点击前的安全护栏：指针在危险区则拒绝点击，避免误触退出登录/切页/关窗
    if args and args[0] == "click":
        px, py = _pointer()
        if px < SAFE_MIN_X or py < SAFE_MIN_Y:
            print(f"[send] 安全护栏：拒绝在危险区点击 ({px},{py})", flush=True)
            return subprocess.CompletedProcess(args, 0, "", "")
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


DROPDOWN_REGION = (56, 85, 262, 560)   # 搜索下拉区域
_SKIP_ROWS = ("搜索网络结果", "群聊", "联系人", "聊天记录", "收藏", "查看全部", "更多", "网络查找")


def _ocr_lines(region) -> list[tuple[str, int, int]]:
    """OCR 区域，返回 [(行文本, 行中心x绝对, 行中心y绝对)]。"""
    fd, png = tempfile.mkstemp(suffix=".png"); os.close(fd)
    out = []
    try:
        if subprocess.run(["scrot", "-a", ",".join(map(str, region)), "-o", png],
                          capture_output=True, env=_env()).returncode != 0:
            return out
        base = png[:-4]
        subprocess.run(["tesseract", png, base, "-l", "chi_sim+eng", "tsv"], capture_output=True, env=_env())
        tsv = base + ".tsv"
        if not os.path.exists(tsv):
            return out
        lines = {}
        with open(tsv, encoding="utf-8") as f:
            next(f, None)
            for row in f:
                c = row.rstrip("\n").split("\t")
                if len(c) < 12 or not c[11].strip():
                    continue
                lines.setdefault(tuple(c[1:5]), []).append((int(c[6]), int(c[7]), int(c[8]), int(c[9]), c[11]))
        os.remove(tsv)
        for parts in lines.values():
            text = "".join(p[4] for p in parts)
            x0 = min(p[0] for p in parts); x1 = max(p[0] + p[2] for p in parts)
            y0 = min(p[1] for p in parts); y1 = max(p[1] + p[3] for p in parts)
            out.append((text, region[0] + (x0 + x1) // 2, region[1] + (y0 + y1) // 2))
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    return out


def _find_result_row(chat_name: str) -> tuple[int, int] | None:
    """OCR 搜索下拉，找与 chat_name 匹配的结果行坐标（兼容联系人/群，联系人常在固定位，群靠 OCR）。"""
    g = _norm(chat_name)
    best = None
    skip_from = -1          # 收藏/聊天记录 区起始 y，之后的行一律跳过（不是会话本身）
    for text, cx, cy in sorted(_ocr_lines(DROPDOWN_REGION), key=lambda r: r[2]):
        if ("收藏" in text or "聊天记录" in text) and skip_from < 0:
            skip_from = cy
        t = _norm(text)
        if not t or any(s in text for s in _SKIP_ROWS) or "包含" in text or "来自" in text:
            continue        # 跳过分区标题、"包含:xxx"/"来自:xxx" 预览
        if skip_from >= 0 and cy > skip_from:
            continue        # 收藏/聊天记录区的项目不是会话，跳过
        if cy < 110:
            continue        # 顶部查询回显
        if t == g:
            return (cx, cy)                      # 完全匹配优先
        if best is None and len(g) >= 2 and (g in t or t in g):
            best = (cx, cy)
    return best


def _open_chat(win: str, chat_name: str) -> bool:
    """搜索并打开会话：OCR 下拉找匹配行点击（兼容群），联系人用固定位兜底，OCR 标题核对。"""
    _click(*SEARCH_XY)
    _x("key", "ctrl+a")
    _x("key", "Delete")
    _clip(chat_name)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(1.6)                 # 等下拉结果稳定
    pos = _find_result_row(chat_name)
    _click(*(pos or RESULT_XY))    # OCR 找到就点它，否则点固定首位（联系人）
    time.sleep(0.8)
    title = _ocr_title()
    g = _norm(chat_name)
    if bool(title) and (title == g or (len(g) >= 2 and (g in title or title in g))):
        return True
    # 兜底失败时再试另一种：之前点了 OCR 位就补点固定位，反之亦然
    if pos:
        _click(*SEARCH_XY); _x("key", "ctrl+a"); _x("key", "Delete")
        _clip(chat_name); time.sleep(0.1); _x("key", "ctrl+v"); time.sleep(1.6)
        _click(*RESULT_XY); time.sleep(0.8)
        title = _ocr_title()
        return bool(title) and (title == g or (len(g) >= 2 and (g in title or title in g)))
    return False


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
