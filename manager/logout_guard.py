"""登出看门狗：后台持续检测微信「退出登录」确认框，自动点「取消」。

UI 自动化（抓媒体、打开会话）偶发误触到个人资料会弹出「退出登录后将无法收到新消息，
确定退出登录？」。本守护进程每秒扫屏中央区域，一旦识别到该对话框就点「取消」，
保证登录永不因自动化误触而丢失。「取消」按钮在屏幕中央（非危险区），点击安全。
"""
import os
import subprocess
import tempfile
import time

DISPLAY = os.environ.get("DISPLAY", ":1")
ENV = {**os.environ, "DISPLAY": DISPLAY}
# 对话框居中出现，扫中央一条带
REGION = (380, 270, 540, 260)   # x, y, w, h


def _ocr(region):
    """OCR 区域，返回 (全文, 「取消」按钮中心坐标 或 None)。"""
    fd, png = tempfile.mkstemp(suffix=".png"); os.close(fd)
    try:
        if subprocess.run(["scrot", "-a", ",".join(map(str, region)), "-o", png],
                          capture_output=True, env=ENV).returncode != 0:
            return "", None
        base = png[:-4]
        subprocess.run(["tesseract", png, base, "-l", "chi_sim", "tsv"], capture_output=True, env=ENV)
        tsv = base + ".tsv"
        if not os.path.exists(tsv):
            return "", None
        lines = {}
        with open(tsv, encoding="utf-8") as f:
            next(f, None)
            for row in f:
                c = row.rstrip("\n").split("\t")
                if len(c) < 12 or not c[11].strip():
                    continue
                lines.setdefault(tuple(c[1:5]), []).append((int(c[6]), int(c[7]), int(c[8]), int(c[9]), c[11]))
        os.remove(tsv)
        alltext, cancel = "", None
        for parts in lines.values():
            t = "".join(p[4] for p in parts)
            alltext += t
            if "取消" in t:
                x0 = min(p[0] for p in parts); x1 = max(p[0] + p[2] for p in parts)
                y0 = min(p[1] for p in parts); y1 = max(p[1] + p[3] for p in parts)
                cancel = (region[0] + (x0 + x1) // 2, region[1] + (y0 + y1) // 2)
        return alltext, cancel
    finally:
        try:
            os.remove(png)
        except OSError:
            pass


def main():
    print("[logout-guard] 启动，持续守护登出确认框", flush=True)
    while True:
        try:
            text, cancel = _ocr(REGION)
            if "退出登录" in text:
                if cancel and cancel[0] >= 62:
                    subprocess.run(["xdotool", "mousemove", str(cancel[0]), str(cancel[1])], env=ENV)
                    time.sleep(0.1)
                    subprocess.run(["xdotool", "click", "1"], env=ENV)
                    print(f"[logout-guard] 检测到登出框 → 已点「取消」{cancel}", flush=True)
                else:
                    subprocess.run(["xdotool", "key", "Escape"], env=ENV)   # 兜底
                    print("[logout-guard] 检测到登出框 → Escape 兜底", flush=True)
                time.sleep(0.5)
        except Exception as e:  # noqa: BLE001
            print("[logout-guard] 异常:", e, flush=True)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
