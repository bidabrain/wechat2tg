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

try:
    from PIL import Image, ImageOps
except ImportError:  # 没装 PIL 时跳过预处理（OCR 仍可用，只是精度差）
    Image = None


OCR_SCALE = 3


def _prep_ocr(png: str):
    """OCR 预处理：灰度 + 放大 + 轻度二值化，显著提升小字/彩色文字识别率。
    放大后 tsv 坐标会乘以 OCR_SCALE，调用方需除回去。"""
    if Image is None:
        return
    try:
        im = Image.open(png).convert("L")
        im = im.resize((im.width * OCR_SCALE, im.height * OCR_SCALE), Image.LANCZOS)
        im = ImageOps.autocontrast(im)
        im = im.point(lambda p: 0 if p < 170 else 255)   # 阈值：深色(含绿字)→黑，浅底→白
        im.save(png)
    except Exception:  # noqa: BLE001
        pass

DISPLAY = ":1"
WIN_W, WIN_H = 1280, 800
# 规整到 (0,0) 后的绝对坐标（实测校准）
SEARCH_XY = (168, 60)      # 搜索框
RESULT_XY = (165, 141)     # 搜索下拉第一个匹配项（联系人/会话）
INPUT_XY = (768, 700)      # 聊天输入框
FILE_BTN_XY = (419, 768)   # 底部工具栏「发送文件」按钮
DLG_NAME_XY = (628, 568)   # 文件对话框 文件名字段
SEND_BTN_XY = (1232, 768)  # 「发送」按钮
TITLE_REGION = (326, 45, 320, 32)   # 聊天标题栏（OCR 核对用）：x, y, 宽, 高
_LOCK = threading.Lock()


def _env() -> dict:
    return {**os.environ, "DISPLAY": DISPLAY}


# 动作面包屑：每个 UI 动作覆盖写一次，看门狗抓到注销框时读它，定位触发步骤。
try:
    from common import set_action as _set_action
except ImportError:
    try:
        from .common import set_action as _set_action
    except ImportError:
        def _set_action(label: str) -> None:  # 独立跑且无 common 时退化为空操作
            pass


def _act(label: str) -> None:
    try:
        _set_action(label)
    except Exception:  # noqa: BLE001
        pass


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


# 所有会"按下"的指针动作都要查危险区（不止 click：还有拖拽用的 mousedown/mouseup）。
# mousemove 不算——移动无害，真正落子的按键动作才拦。
_BTN_ACTIONS = ("click", "mousedown", "mouseup")


def _x(*args: str) -> subprocess.CompletedProcess:
    # 点击/按下前的安全护栏：指针在危险区则拒绝，避免误触退出登录/切页/关窗
    if args and args[0] in _BTN_ACTIONS:
        px, py = _pointer()
        if px < SAFE_MIN_X or py < SAFE_MIN_Y:
            _act(f"拒绝危险区{args[0]}({px},{py})")
            print(f"[send] 安全护栏：拒绝在危险区 {args[0]} ({px},{py})", flush=True)
            return subprocess.CompletedProcess(args, 0, "", "")
        _act(f"{args[0]}({px},{py})")          # 面包屑：记下这次按键动作的坐标
    elif args and args[0] in ("key", "type"):
        _act(f"{args[0]} {' '.join(args[1:])[:40]}")
    return subprocess.run(["xdotool", *args], capture_output=True, text=True, env=_env())


# ---- C: 注销框探针。发送各步后就地探测「退出登录」框，命中即存全屏图+记步骤名，
#         用于定位到底哪一步误触（logout_guard 会很快关掉它，故探针紧贴每步之后跑）。
LOGOUT_REGION = (380, 270, 540, 260)


def _logout_dialog_present() -> bool:
    fd, png = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        if subprocess.run(["scrot", "-a", ",".join(map(str, LOGOUT_REGION)), "-o", png],
                          capture_output=True, env=_env()).returncode != 0:
            return False
        out = subprocess.run(["tesseract", png, "-", "-l", "chi_sim"],
                             capture_output=True, text=True, env=_env()).stdout
        return "退出登录" in out
    except Exception:  # noqa: BLE001
        return False
    finally:
        try:
            os.remove(png)
        except OSError:
            pass


def _log_event(source: str, detail: str) -> None:
    try:
        try:
            from common import log_event
        except ImportError:
            from .common import log_event
        log_event(source, detail)
    except Exception:  # noqa: BLE001
        pass


def _dbg_logout(step: str) -> None:
    try:
        if _logout_dialog_present():
            full = f"/data/logout_caught_{step}_{int(time.time())}.png"
            subprocess.run(["scrot", "-o", full], capture_output=True, env=_env())
            print(f"[send][!!注销框] 触发步骤={step}，已存 {full}", flush=True)
            _log_event("send-probe", f"step={step} shot={full}")
    except Exception as e:  # noqa: BLE001
        print("[send] _dbg_logout 异常:", e, flush=True)


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
        # 标题栏是粗黑大字，直接 OCR 即可；放大+二值化预处理反而会毁掉短名
        # （如「塔子」被读成 ay），故此处不做 _prep_ocr。
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
        _prep_ocr(png)              # 放大 OCR_SCALE 倍 + 二值化（坐标随之放大）
        s = OCR_SCALE if Image is not None else 1
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
            out.append((text, region[0] + (x0 + x1) // (2 * s), region[1] + (y0 + y1) // (2 * s)))
    finally:
        try:
            os.remove(png)
        except OSError:
            pass
    return out


def _first_match_row(region=DROPDOWN_REGION) -> int | None:
    """定位要打开的会话行：同时满足【左侧有照片头像】且【名字整段高亮成品牌绿(完整命中)】，
    取最靠上的这种行。绕开中文 OCR。查询回显/分区标题没有照片头像；收藏的「来自xxx」行
    在结果行下方、名字非全绿，都被排除。返回该行中心 y 绝对坐标。
    首次抓屏下拉可能还在渲染，故最多重试 2 次。"""
    for attempt in range(2):
        r = _scan_match_row(region)
        if r is not None:
            return r
        time.sleep(0.5)                            # 等下拉渲染完再重扫
    return None


def _scan_match_row(region=DROPDOWN_REGION) -> int | None:
    if Image is None:
        return None
    fd, png = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        if subprocess.run(["scrot", "-a", ",".join(map(str, region)), "-o", png],
                          capture_output=True, env=_env()).returncode != 0:
            return None
        im = Image.open(png).convert("RGB"); px = im.load(); W, H = im.size
        AX0, AX1 = 4, 52               # 头像列（相对 region；绝对约 x60-108）
        TX0 = 74                       # 名字列起点（跳过头像）
        green = []                     # 每行绿字像素数
        dark = []                      # 每行文字像素数（非浅色背景）
        for y in range(H):
            gn = 0; dk = 0
            for x in range(TX0, W, 2):
                r, g, b = px[x, y]
                if (r + g + b) / 3 < 205:                      # 文字像素（非背景）
                    dk += 1
                    if g > 120 and g - r > 45 and g - b > 30 and r < 150:
                        gn += 1
            green.append(gn); dark.append(dk)

        def has_avatar(y: int) -> bool:
            vals = [px[x, y + dy] for x in range(AX0, min(AX1, W), 6)
                    for dy in range(-6, 24, 6) if 0 <= y + dy < H]
            if not vals:
                return False
            var = sum(max(v) - min(v) for v in vals) / len(vals)
            avg = sum(sum(v) for v in vals) / (len(vals) * 3)
            return var > 22 and avg < 248          # 照片头像（非空白/非纯色图标）

        y = 44                                     # 跳过搜索框/查询回显
        while y < H:
            if green[y] >= 4:                      # 绿字聚集 = 命中名字
                y0 = y
                while y < H and green[y] >= 2:
                    y += 1
                sg = sum(green[y0:y]); sd = sum(dark[y0:y])
                cy = (y0 + y) // 2
                # 名字整段几乎全绿(完整命中，而非只命中一段) 且 该行左侧有头像 → 真正的会话结果
                if sd > 0 and sg >= 6 and sg / sd >= 0.5 and has_avatar(cy):
                    return region[1] + cy
            else:
                y += 1
        return None
    finally:
        try:
            os.remove(png)
        except OSError:
            pass


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


def _title_ok(chat_name: str) -> bool:
    title = _ocr_title()
    g = _norm(chat_name)
    return bool(title) and (title == g or (len(g) >= 2 and (g in title or title in g)))


def _search(chat_name: str) -> None:
    _click(*SEARCH_XY)
    _x("key", "ctrl+a")
    _x("key", "Delete")
    _clip(chat_name)
    time.sleep(0.1)
    _x("key", "ctrl+v")
    time.sleep(1.6)                 # 等下拉结果稳定


def _open_chat(win: str, chat_name: str) -> bool:
    """搜索并打开会话，按候选位依次点击并用标题栏 OCR 核对，命中即止。
    定位候选顺序：①头像+名字整段全绿命中行(绕开下拉里不可靠的中文 OCR) ②下拉文本 OCR
    匹配 ③固定首位。每个候选点开后都用标题栏黑字 OCR(_title_ok)核对，核对不过继续下一候选；
    全部不过返回 False、绝不发送，避免误发到当前停留的会话。"""
    tried: list[int] = []
    for build in (
        lambda: (RESULT_XY[0], _first_match_row()),    # ①头像+名字整段全绿定位
        lambda: _find_result_row(chat_name),           # ②下拉 OCR 文本匹配
        lambda: RESULT_XY,                             # ③固定首位兜底
    ):
        _search(chat_name)
        pos = build()
        if not pos or pos[1] is None:
            continue
        y = pos[1]
        if any(abs(y - t) <= 6 for t in tried):        # 这一行已点过，跳过
            continue
        tried.append(y)
        _click(pos[0], y)
        time.sleep(0.8)
        if _title_ok(chat_name):                       # 标题栏核对通过才放行
            return True
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
        _dbg_logout("img_after_open")
        _clip_image(image_path)
        time.sleep(0.3)
        _click(*INPUT_XY)
        time.sleep(0.2)
        _x("key", "ctrl+v")
        time.sleep(0.8)
        _dbg_logout("img_after_paste")
        _x("key", "Return")
        time.sleep(0.6)
        _dbg_logout("img_after_return")
        return {"ok": True, "step": "sent", "msg": "已发送图片"}


def _file_dialog_open() -> bool:
    """等待「选择文件」对话框出现，最多 ~3s。"""
    for _ in range(10):
        if _x("search", "--name", "选择文件").stdout.strip():
            return True
        time.sleep(0.3)
    return False


def send_file_to_chat(chat_name: str, chat_id: str, file_path: str) -> dict:
    """打开会话（OCR 标题核对）→ 发送文件按钮 → 文件对话框输入绝对路径 → 发送。
    微信会按文件类型发送（.mp4 以视频形式、图片以图片形式）。核对不通过不发。"""
    with _LOCK:
        win = _wechat_win()
        if not win:
            return {"ok": False, "step": "find_window", "msg": "找不到微信窗口"}
        _normalize(win)
        if not _open_chat(win, chat_name):
            return {"ok": False, "step": "verify", "msg": f"未能定位会话「{chat_name}」（已避免误发）"}
        _dbg_logout("file_after_open")
        _click(*FILE_BTN_XY)
        if not _file_dialog_open():
            _dbg_logout("file_no_dialog")
            return {"ok": False, "step": "dialog", "msg": "未弹出文件选择对话框"}
        _click(*DLG_NAME_XY)        # 文件名字段
        _x("key", "ctrl+a")
        _x("key", "Delete")
        _x("type", "--delay", "12", file_path)
        time.sleep(0.3)
        _x("key", "Return")         # 选中文件 → 附加到输入框
        time.sleep(1.0)
        _dbg_logout("file_after_attach")
        _click(*SEND_BTN_XY)        # 发送
        time.sleep(0.5)
        _dbg_logout("file_after_send")
        return {"ok": True, "step": "sent", "msg": "已发送文件"}


def send_text(target: str, text: str) -> dict:
    return send_to_chat(target, target, text)


if __name__ == "__main__":  # 自测：python send.py searchon "你好"
    import sys
    tgt = sys.argv[1] if len(sys.argv) > 1 else "searchon"
    msg = sys.argv[2] if len(sys.argv) > 2 else f"wechat2tg send.py 测试 {time.strftime('%H:%M:%S')}"
    print(send_text(tgt, msg))
