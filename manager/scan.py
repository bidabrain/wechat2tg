"""扫码上传：网页上传二维码图片 → 解码 → 推送到模拟器相册 / 设为虚拟摄像头画面。"""

import io
import secrets
import time

import qrcode
from PIL import Image, ImageOps

from common import ADB, ADB_SERIAL, SCAN_DIR, run, sh, sh_quote

REMOTE_DIR = "/sdcard/Pictures/wx2tg"
POSTER = SCAN_DIR / "poster.png"
UPLOADS: dict[str, dict] = {}  # id -> {"path", "codes", "created"}


def decode(img: Image.Image) -> list[dict]:
    try:
        import zxingcpp
    except ImportError:
        return []
    out = []
    for candidate in (img, ImageOps.grayscale(img), ImageOps.autocontrast(ImageOps.grayscale(img))):
        for r in zxingcpp.read_barcodes(candidate):
            item = {"text": r.text, "format": str(r.format).split(".")[-1]}
            if item not in out:
                out.append(item)
        if out:
            break
    return out


def save_upload(data: bytes) -> dict:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"无法识别的图片：{e}") from e
    uid = time.strftime("%Y%m%d%H%M%S") + secrets.token_hex(3)
    path = SCAN_DIR / f"upload_{uid}.png"
    img.save(path, "PNG")
    codes = decode(img)
    UPLOADS[uid] = {"path": path, "codes": codes, "created": time.time()}
    _cleanup_local()
    return {"id": uid, "codes": codes, "width": img.width, "height": img.height}


def regenerate(text: str) -> Image.Image:
    """用解码出来的内容重新生成一张清晰的大二维码，提升识别率。"""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=14, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


def resolve(uid: str, use_regenerated: bool) -> Image.Image:
    up = UPLOADS.get(uid)
    if not up or not up["path"].exists():
        raise ValueError("图片不存在或已过期，请重新上传")
    if use_regenerated:
        if not up["codes"]:
            raise ValueError("这张图片没有解码出二维码，无法重新生成；请直接使用原图")
        return regenerate(up["codes"][0]["text"])
    return Image.open(up["path"]).convert("RGB")


def push_to_gallery(uid: str, use_regenerated: bool) -> dict:
    img = resolve(uid, use_regenerated)
    name = f"qr_{int(time.time())}.png"
    local = SCAN_DIR / name
    img.save(local, "PNG")
    remote = f"{REMOTE_DIR}/{name}"
    sh(f"mkdir -p {REMOTE_DIR}", 10)
    res = run([ADB, "-s", ADB_SERIAL, "push", str(local), remote], 60)
    local.unlink(missing_ok=True)
    if res.returncode != 0:
        raise ValueError(f"推送到模拟器失败：{(res.stdout + res.stderr).strip()[-200:]}")
    # 触发媒体库扫描，让图片出现在相册里
    sh(f"am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d {sh_quote('file://' + remote)}", 15)
    indexed = False
    where = f"_data LIKE '%/{name}'"
    for _ in range(6):
        q = sh(f"content query --uri content://media/external/images/media --projection _data --where {sh_quote(where)}", 15)
        if name in q.stdout:
            indexed = True
            break
        time.sleep(1)
    return {"remote": remote, "indexed": indexed}


def cleanup_gallery(max_age_minutes: int = 10) -> int:
    """删除模拟器相册里超过一定时间的扫码图片。"""
    res = sh(f"ls {REMOTE_DIR} 2>/dev/null", 10)
    now = int(time.time())
    removed = 0
    for name in res.stdout.split():
        if not name.startswith("qr_") or not name.endswith(".png"):
            continue
        try:
            ts = int(name[3:-4])
        except ValueError:
            continue
        if now - ts > max_age_minutes * 60:
            remote = f"{REMOTE_DIR}/{name}"
            where = f"_data='{remote}'"
            sh(f"rm -f {remote}; content delete --uri content://media/external/images/media --where {sh_quote(where)}", 15)
            removed += 1
    return removed


def set_camera_poster(uid: str, use_regenerated: bool) -> dict:
    """设为虚拟摄像头（virtualscene）墙上的海报；模拟器需重启后生效。"""
    img = resolve(uid, use_regenerated)
    # 四周加白边，便于在 3D 场景中被识别
    img = ImageOps.expand(img, border=max(img.size) // 8, fill="white")
    img.save(POSTER, "PNG")
    return {"path": str(POSTER)}


def _cleanup_local(keep_hours: int = 6):
    now = time.time()
    for uid, up in list(UPLOADS.items()):
        if now - up["created"] > keep_hours * 3600:
            up["path"].unlink(missing_ok=True)
            UPLOADS.pop(uid, None)
