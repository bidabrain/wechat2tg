"""微信 → 媒体提取：用 UI「右键→另存为」把图片/视频解密保存到临时目录，供转发。

不碰加密密钥（Linux 下密钥被混淆、无法抽取），而是让微信自己解密：
  图片：右键消息 → 另存为 → 文件名填绝对路径 → 保存。
  视频：先单击下载，等下载完成后 右键 → 另存为。
定位会话里的媒体气泡用图像识别（大块高色彩方差区域）。转发后删除临时文件。

坐标基于主窗口规整到 (0,0) 的 1280x800 布局（见 send.py）。
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time

from PIL import Image

try:
    from . import send
except ImportError:
    import send

MEDIA_DIR = os.environ.get("MEDIA_DIR", "/data/media")
# 对话消息【内容】区（绝对坐标，窗口规整到 0,0 后）。
# 两侧各留出头像列（发送方头像 ~x352-372、自己头像 ~x1215-1255），避免把头像当媒体点到，
# 误触个人资料/退出登录。接收的媒体从 ~x377 起，发送的媒体到 ~x1203 止。
MSG_AREA = (375, 95, 1205, 715)
MIN_MEDIA_PX = 100           # 媒体气泡至少这么大，排除头像(~40px)/小表情
DISPLAY = ":1"


def _env():
    return {**os.environ, "DISPLAY": DISPLAY}


def _scrot(path: str, region=None):
    cmd = ["scrot", "-o"]
    if region:
        cmd += ["-a", ",".join(map(str, region))]
    cmd.append(path)
    subprocess.run(cmd, capture_output=True, env=_env())


def find_media_regions(png: str) -> list[tuple[int, int, int, int]]:
    """在消息区检测图片/视频气泡，返回按底部 y 排序的 [(x,y,w,h), ...]。
    判据：大块、颜色方差高、非背景白、非绿色气泡。"""
    im = Image.open(png).convert("RGB")
    px = im.load()
    X0, Y0, X1, Y1 = MSG_AREA
    CELL = 20
    cols, rows = (X1 - X0) // CELL, (Y1 - Y0) // CELL

    def is_img(cx, cy):
        vals = [px[X0 + cx * CELL + dx, Y0 + cy * CELL + dy]
                for dx in range(0, CELL, 5) for dy in range(0, CELL, 5)]
        rs = [v[0] for v in vals]; gs = [v[1] for v in vals]; bs = [v[2] for v in vals]
        var = sum(max(c) - min(c) for c in (rs, gs, bs))
        avg = (sum(rs) / len(rs), sum(gs) / len(gs), sum(bs) / len(bs))
        bg = avg[0] > 235 and avg[1] > 235 and avg[2] > 235
        green = avg[1] > avg[0] + 30 and avg[1] > avg[2] + 30
        return var > 40 and not bg and not green

    grid = [[is_img(cx, cy) for cx in range(cols)] for cy in range(rows)]
    seen = [[False] * cols for _ in range(rows)]
    boxes = []
    for cy in range(rows):
        for cx in range(cols):
            if not grid[cy][cx] or seen[cy][cx]:
                continue
            st = [(cy, cx)]; seen[cy][cx] = True; cells = []
            while st:
                y, x = st.pop(); cells.append((y, x))
                for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < rows and 0 <= nx < cols and grid[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True; st.append((ny, nx))
            xs = [c[1] for c in cells]; ys = [c[0] for c in cells]
            w, h = (max(xs) - min(xs) + 1) * CELL, (max(ys) - min(ys) + 1) * CELL
            if w >= MIN_MEDIA_PX and h >= MIN_MEDIA_PX:   # 只认大块媒体
                boxes.append((X0 + min(xs) * CELL, Y0 + min(ys) * CELL, w, h))
    boxes.sort(key=lambda b: b[1] + b[3])   # 底部 y 升序，最后一个是最新
    return boxes


def _ocr_menu_item(region: tuple[int, int, int, int], target: str) -> tuple[int, int] | None:
    """在给定屏幕区域 OCR 找 target 文本，返回其中心绝对坐标。"""
    fd, png = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        _scrot(png, region)
        send._prep_ocr(png)         # 与 send 一致的 OCR 预处理（放大+二值化）
        s = send.OCR_SCALE if send.Image is not None else 1
        base = png[:-4]
        subprocess.run(["tesseract", png, base, "-l", "chi_sim", "tsv"], capture_output=True, env=_env())
        tsv = base + ".tsv"
        if not os.path.exists(tsv):
            return None
        lines: dict[tuple, list] = {}
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
            if target in text:
                x0 = min(p[0] for p in parts); x1 = max(p[0] + p[2] for p in parts)
                y0 = min(p[1] for p in parts); y1 = max(p[1] + p[3] for p in parts)
                return (region[0] + (x0 + x1) // (2 * s), region[1] + (y0 + y1) // (2 * s))
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    return None


def _X(*a):
    return send._x(*a)        # 复用 send 的点击安全护栏（拒绝危险区点击）


def _rightclick_find_save(cx: int, cy: int) -> tuple[int, int] | None:
    """右键 (cx,cy)，OCR 在菜单带内找「另存为」坐标。
    找到 → 返回坐标且【保留菜单打开】；找不到 → Escape 关菜单并返回 None。
    安全：OCR 结果必须落在光标正下方的菜单带内，否则视为误识别。"""
    _X("mousemove", str(cx), str(cy)); time.sleep(0.3)
    _X("click", "3"); time.sleep(0.8)
    region = (cx, cy, min(220, 1280 - cx), min(360, 800 - cy))
    pos = _ocr_menu_item(region, "另存")
    if pos and (pos[1] <= cy or pos[0] < cx - 10 or pos[0] > cx + 230):
        pos = None                            # 位置异常，视为误识别
    if not pos:
        _X("key", "Escape"); time.sleep(0.2)  # 没找到才关菜单
    return pos                                # 找到则保留菜单打开，供点击


def _save_dialog_open() -> bool:
    """等待「另存为」保存对话框出现，最多 ~3s。"""
    for _ in range(10):
        if _X("search", "--name", "另存为").stdout.strip():
            return True
        time.sleep(0.3)
    return False


def _save_via_menu(cx: int, cy: int, out_path: str, is_video: bool = False) -> bool:
    """右键消息 → 另存为 → 保存到 out_path。
    视频未下载时右键无「另存为」：安全地左键一次触发下载 → Escape 关播放器 → 固定等待
    → 再右键一次另存。全程不 windowclose、不循环乱点，避免误触个人资料/退出登录。
    关键安全：点「另存为」后必须确认保存对话框真打开，否则绝不往聊天框打字（避免把路径发出去）。"""
    pos = _rightclick_find_save(cx, cy)
    if not pos and is_video:
        _X("mousemove", str(cx), str(cy)); time.sleep(0.2)
        _X("click", "1"); time.sleep(1.0)     # 左键一次触发下载（可能开播放器）
        _X("key", "Escape"); time.sleep(0.5)  # 仅用 Escape 关播放器，绝不 windowclose
        for _ in range(8):                    # 等下载，最多 ~16s
            time.sleep(2.0)
            pos = _rightclick_find_save(cx, cy)
            if pos:
                break
    if not pos:
        return False
    _X("mousemove", str(pos[0]), str(pos[1])); time.sleep(0.2)
    _X("click", "1")                          # 菜单仍打开，点「另存为」→ 弹对话框
    if not _save_dialog_open():
        _X("key", "Escape"); time.sleep(0.2)  # 对话框没开：绝不在聊天框打字
        return False
    _X("key", "ctrl+a"); time.sleep(0.2)
    _X("type", "--delay", "12", out_path); time.sleep(0.3)
    _X("key", "Return"); time.sleep(1.5)
    if is_video:                              # 视频可能边下边存，等大小稳定
        last = -1
        for _ in range(30):
            if os.path.exists(out_path):
                sz = os.path.getsize(out_path)
                if sz > 0 and sz == last:
                    break
                last = sz
            time.sleep(0.5)
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


def _player_window() -> str | None:
    for wid in _X("search", "--name", "图片和视频").stdout.split():
        return wid
    return None


def _close_player():
    _X("key", "Escape"); time.sleep(0.3)
    for wid in _X("search", "--name", "图片和视频").stdout.split():
        _X("windowclose", wid)          # 只关播放器窗口（非主窗口），安全
    time.sleep(0.3)


def _save_video_via_player(cx: int, cy: int, out_path: str) -> bool:
    """按用户方案：左键视频 → 打开播放器 → 在播放器中央右键 → 另存为。
    在独立播放器里操作，远离边栏/头像，避免误触退出登录。"""
    _X("mousemove", str(cx), str(cy)); time.sleep(0.2)
    _X("click", "1")                     # 左键打开播放器（未下载则边播边下）
    # 等播放器窗口出现
    wid = None
    for _ in range(15):
        time.sleep(1.0)
        wid = _player_window()
        if wid:
            break
    if not wid:
        return False
    # 播放器中央右键
    g = _X("getwindowgeometry", "--shell", wid).stdout
    gx = gy = gw = gh = 0
    for line in g.splitlines():
        if line.startswith("X="): gx = int(line[2:])
        elif line.startswith("Y="): gy = int(line[2:])
        elif line.startswith("WIDTH="): gw = int(line[6:])
        elif line.startswith("HEIGHT="): gh = int(line[7:])
    pcx, pcy = gx + gw // 2, gy + gh // 2
    pos = None
    for _ in range(8):                   # 等视频下载好、菜单里出现「另存为」
        _X("mousemove", str(pcx), str(pcy)); time.sleep(0.2)
        _X("click", "3"); time.sleep(0.8)
        region = (max(pcx - 20, 0), pcy, min(260, 1280 - pcx), min(360, 800 - pcy))
        pos = _ocr_menu_item(region, "另存")
        if pos and pos[0] >= 62 and pos[1] > pcy - 10:
            break
        pos = None
        _X("key", "Escape"); time.sleep(1.5)
    if not pos:
        _close_player()
        return False
    _X("mousemove", str(pos[0]), str(pos[1])); time.sleep(0.2)
    _X("click", "1")                     # 菜单仍打开，点「另存为」
    if not _save_dialog_open():
        _X("key", "Escape"); _close_player()
        return False
    _X("key", "ctrl+a"); time.sleep(0.2)
    _X("type", "--delay", "12", out_path); time.sleep(0.3)
    _X("key", "Return"); time.sleep(1.5)
    last = -1
    for _ in range(30):                  # 等文件大小稳定（边下边存）
        if os.path.exists(out_path):
            sz = os.path.getsize(out_path)
            if sz > 0 and sz == last:
                break
            last = sz
        time.sleep(0.5)
    _close_player()
    return os.path.exists(out_path) and os.path.getsize(out_path) > 0


def save_latest_media(out_name: str, is_video: bool = False) -> str | None:
    """在【当前已打开的会话】里把最新的图片/视频另存到临时目录，返回路径。
    图片：聊天里右键→另存为。视频：左键开播放器→播放器内右键→另存为（更安全）。"""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    win = send._wechat_win()
    if not win:
        return None
    send._normalize(win)
    subprocess.run(["xdotool", "key", "End"], capture_output=True, env=_env())   # 滚到底
    time.sleep(0.6)
    fd, shot = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        _scrot(shot)
        boxes = find_media_regions(shot)
    finally:
        try:
            os.remove(shot)
        except OSError:
            pass
    if not boxes:
        return None
    bx, by, bw, bh = boxes[-1]                # 最新（最底部）的媒体气泡
    cx, cy = bx + bw // 2, by + bh // 2
    out_path = os.path.join(MEDIA_DIR, out_name)
    # 图片与视频都走「右键→另存为；视频未下载则先左键下载(仅下载不开播放器)→等→右键另存」。
    ok = _save_via_menu(cx, cy, out_path, is_video=is_video)
    return out_path if ok else None


def save_latest_image(out_name: str) -> str | None:   # 向后兼容
    return save_latest_media(out_name, is_video=False)
