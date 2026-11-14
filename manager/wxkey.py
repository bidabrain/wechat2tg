"""微信 Linux 版（4.10+）数据库密钥提取与解密。

微信把 SQLCipher 的 `x'<64hex密钥><32hex盐>'` 字面量用一个固定 32 字节掩码
循环 XOR 混淆后存在 WCDB Config.Cipher 对象里，内存里既扫不到明文密钥，也扫不到
明文 x'...'。这里用固定掩码对可读内存做滑窗 XOR 解码，解出 x'...' 即候选，再用各库
page1 的 AES/HMAC 校验匹配。每个库一把独立密钥。

参考：github.com/fangbm/wxdb（CONFIG_XOR_MASK，4.1.12.55 验证），对 4.1.13.26 同样有效。
需要 root 或 CAP_SYS_PTRACE 读取 /proc/<pid>/mem。
"""
from __future__ import annotations

import glob
import hashlib
import hmac
import os
import re
import struct
import subprocess
from pathlib import Path

from Crypto.Cipher import AES

PAGE = 4096
# 固定 32 字节掩码（微信 Weixin 模块内一段 x86 代码，被复用作混淆掩码）
MASK = bytes([
    0xd2, 0xc7, 0x44, 0x24, 0x58, 0x02, 0x00, 0x00, 0x00, 0x48, 0x89, 0x44, 0x24, 0x50, 0x48, 0x8b,
    0x45, 0x00, 0x48, 0x84, 0x4c, 0x24, 0x48, 0x48, 0x89, 0x44, 0x25, 0x40, 0x48, 0x58, 0x4c, 0x24,
])
# 解码后 x'...' 的前两字节 'x'(0x78) '\''(0x27) 经掩码后的特征，用于快速定位
_SIG = bytes([0x78 ^ MASK[0], 0x27 ^ MASK[1]])
_HEX99 = re.compile(rb"^x'([0-9a-fA-F]{96})'$")
WECHAT_PROC = "/usr/bin/wechat"


def find_wechat_pid() -> int | None:
    """遍历 /proc 找微信主进程 pid。纯文件操作、不起子进程——
    bridge 是 asyncio 多线程进程，其 child-watcher 会和 subprocess(pgrep)
    抢着 reap 子进程导致 waitpid ESRCH、调用偶发失败（表现为「未取到密钥」），
    故这里不用 pgrep。"""
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    for d in pids:
        try:
            with open(f"/proc/{d}/cmdline", "rb") as f:
                argv0 = f.read().split(b"\0", 1)[0].decode("utf-8", "ignore")
        except OSError:
            continue
        if argv0 == WECHAT_PROC or argv0.startswith(WECHAT_PROC + " "):
            return int(d)
    return None


def _db_heads(db_dir: str) -> dict[str, bytes]:
    """返回 {库路径: page1}，跳过非加密库。"""
    heads: dict[str, bytes] = {}
    for db in glob.glob(f"{db_dir}/**/*.db", recursive=True):
        try:
            with open(db, "rb") as f:
                p1 = f.read(PAGE)
        except OSError:
            continue
        if len(p1) == PAGE and not p1.startswith(b"SQLite format 3"):
            heads[db] = p1
    return heads


def _reserve_of(p1: bytes, key: bytes) -> int | None:
    """用 key 解密 page1 第一块，命中 SQLite 页头则返回 reserve 大小。"""
    for res in (48, 64, 80):
        iv = p1[PAGE - res:PAGE - res + 16]
        o = AES.new(key, AES.MODE_CBC, iv).decrypt(p1[16:PAGE - res])
        if o[0] == 0x10 and o[1] == 0 and o[4] == res and o[5:8] == b"\x40\x20\x20":
            return res
    return None


def _scan_candidates(pid: int) -> dict[str, str]:
    """滑窗 XOR 解码内存，返回 {key_hex: salt_hex}。"""
    cands: dict[str, str] = {}
    try:
        mem = open(f"/proc/{pid}/mem", "rb", 0)
    except OSError:
        return cands
    with mem:
        for line in open(f"/proc/{pid}/maps"):
            f = line.split()
            a, b = (int(x, 16) for x in f[0].split("-"))
            if f[1][0] != "r" or b - a >= 1 << 30:
                continue
            try:
                mem.seek(a)
                buf = mem.read(b - a)
            except OSError:
                continue
            pos = 0
            while True:
                j = buf.find(_SIG, pos)
                if j < 0 or j + 99 > len(buf):
                    break
                pos = j + 1
                dec = bytes(buf[j + k] ^ MASK[k % 32] for k in range(99))
                m = _HEX99.match(dec)
                if m:
                    h = m.group(1).decode().lower()
                    cands[h[:64]] = h[64:]
    return cands


def find_keys(db_dir: str, pid: int | None = None) -> dict[str, str]:
    """返回 {库路径: 64位十六进制密钥}。未登录 / 未找到进程时返回空。"""
    pid = pid or find_wechat_pid()
    if not pid:
        return {}
    heads = _db_heads(db_dir)
    by_salt: dict[str, list[tuple[str, bytes]]] = {}
    for db, p1 in heads.items():
        by_salt.setdefault(p1[:16].hex(), []).append((db, p1))
    cands = _scan_candidates(pid)
    keys: dict[str, str] = {}
    for key_hex, salt_hex in cands.items():
        key = bytes.fromhex(key_hex)
        for db, p1 in by_salt.get(salt_hex, []):
            if _reserve_of(p1, key):
                keys[db] = key_hex
    return keys


def decrypt_db(src: str, key_hex: str, out: str) -> int:
    """把加密库解密成明文 sqlite。返回 reserve 大小。"""
    key = bytes.fromhex(key_hex)
    data = Path(src).read_bytes()
    n = len(data) // PAGE
    res = _reserve_of(data[:PAGE], key) or 80
    with open(out, "wb") as f:
        f.write(b"SQLite format 3\x00")
        for p in range(n):
            page = data[p * PAGE:(p + 1) * PAGE]
            off = 16 if p == 0 else 0
            iv = page[PAGE - res:PAGE - res + 16]
            f.write(AES.new(key, AES.MODE_CBC, iv).decrypt(page[off:PAGE - res]))
            f.write(page[PAGE - res:])
    return res


def wechat_db_dir(home: str) -> str | None:
    """定位 db_storage 目录（home/xwechat_files/<wxid>/db_storage）。"""
    hits = glob.glob(f"{home}/xwechat_files/*/db_storage")
    return hits[0] if hits else None


if __name__ == "__main__":  # 手动自测：python -m wxkey /data/home
    import json
    import sys

    home = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WECHAT_HOME", "/data/home")
    ddir = wechat_db_dir(home)
    if not ddir:
        print("未找到 db_storage，微信可能未登录")
        raise SystemExit(1)
    ks = find_keys(ddir)
    print(f"db_storage={ddir}\n解出密钥 {len(ks)} 个")
    print(json.dumps({os.path.basename(k): v[:8] + "…" for k, v in ks.items()}, ensure_ascii=False, indent=1))
