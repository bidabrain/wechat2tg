"""微信消息采集：解密消息库，解析消息，映射联系人/会话名，监听新消息。

链路：
  会话表 Msg_<md5(会话username)>  ← session.db 的 SessionTable.username 做 md5 对上
  发送者 real_sender_id → 消息库自带 Name2Id.user_name(wxid) → contact.db 取 备注/昵称
  类型 local_type & 0xFFFFFFFF（高 32 位是 appmsg 子类型）

监听：轮询消息库 WAL 的 mtime/size，一有变化就重新解密并按 (表,local_id) 去重，
产出新消息回调。延迟下限取决于微信自身落库节奏（数秒级），而非轮询频率。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field

try:
    from . import wxkey
except ImportError:  # 独立运行（自测）
    import wxkey

try:
    import zstandard as _zstd
    _ZD = _zstd.ZstdDecompressor()
except Exception:  # noqa: BLE001
    _ZD = None

MESSAGE_DBS = ("message/message_0.db", "message/biz_message_0.db")
PLAIN_DIR = os.environ.get("PLAIN_DIR", "/data/p0/plain")

# local_type 低 32 位 → 标签
TYPE_LABEL = {
    1: "文字", 3: "图片", 34: "语音", 43: "视频", 47: "表情", 42: "名片",
    48: "位置", 49: "链接/文件", 10000: "系统", 10002: "撤回",
}


@dataclass
class Message:
    chat_id: str                 # 会话标识（wxid 或 群id@chatroom）
    chat_name: str               # 会话显示名
    is_group: bool
    sender_id: str               # 发送者 wxid（群聊为成员）
    sender_name: str             # 发送者显示名
    type: int                    # 低 32 位类型
    sub_type: int                # appmsg 子类型（高位）
    text: str                    # 可读文本或占位（[图片] 等）
    time: int                    # create_time
    local_id: int
    table: str
    is_self: bool = False        # 是否自己发出（用于避免 TG↔微信 回声）
    raw: str = field(default="", repr=False)   # 原始 message_content（xml 等）


def _decompress(v):
    if isinstance(v, (bytes, bytearray)):
        if _ZD:
            try:
                return _ZD.decompress(bytes(v)).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
        return bytes(v).decode("utf-8", "replace")
    return v


def _appmsg_title(xml: str) -> str:
    m = re.search(r"<title>(.*?)</title>", xml, re.S)
    if m:
        return re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", m.group(1), flags=re.S).strip()
    return ""


class Collector:
    def __init__(self, home: str, on_message=None):
        self.home = home
        self.db_dir = wxkey.wechat_db_dir(home)
        self.on_message = on_message
        self.keys: dict[str, str] = {}
        self._contacts: dict[str, dict] = {}
        self._contacts_ts = 0.0
        self._chat_map: dict[str, str] = {}   # 表名 → 会话 username
        self._chat_ts = 0.0
        os.makedirs(PLAIN_DIR, exist_ok=True)

    def self_id(self) -> str:
        """当前登录账号标识（= db_storage 上级目录名去掉末尾 _xxxx），用于识别自己发的消息。"""
        if not self.db_dir:
            return ""
        acct = os.path.basename(os.path.dirname(self.db_dir))   # 如 bidabrain_b740
        return acct.rsplit("_", 1)[0] if "_" in acct else acct

    # -------------------------------------------------------------- 密钥/解密
    def ready(self) -> bool:
        return bool(self.db_dir and wxkey.find_wechat_pid())

    def refresh_keys(self) -> bool:
        if not self.db_dir:
            self.db_dir = wxkey.wechat_db_dir(self.home)
        if not self.db_dir:
            return False
        self.keys = wxkey.find_keys(self.db_dir)
        return bool(self.keys)

    def _path(self, rel: str) -> str:
        return os.path.join(self.db_dir, rel)

    def _decrypt(self, rel: str) -> str | None:
        src = self._path(rel)
        if not os.path.exists(src) or src not in self.keys:
            # 密钥可能未含该库（刚登录/换号），刷新一次
            if src not in self.keys:
                self.refresh_keys()
            if src not in self.keys:
                return None
        out = os.path.join(PLAIN_DIR, os.path.basename(rel))
        wxkey.decrypt_db(src, self.keys[src], out)
        return out

    # -------------------------------------------------------------- 映射
    def contacts(self, ttl: float = 300) -> dict[str, dict]:
        if self._contacts and time.time() - self._contacts_ts < ttl:
            return self._contacts
        out = self._decrypt("contact/contact.db")
        if not out:
            return self._contacts
        m: dict[str, dict] = {}
        c = sqlite3.connect(out)
        try:
            cur = c.execute("SELECT username, remark, nick_name, chat_room_type FROM contact")
        except sqlite3.OperationalError:
            c.close()
            return self._contacts
        for uname, remark, nick, ctype in cur:
            if not uname:
                continue
            name = (remark or "").strip() or (nick or "").strip() or uname
            m[uname] = {"name": name, "is_group": uname.endswith("@chatroom")}
        c.close()
        self._contacts, self._contacts_ts = m, time.time()
        return m

    def chat_map(self, ttl: float = 120) -> dict[str, str]:
        """{Msg 表名: 会话 username}。"""
        if self._chat_map and time.time() - self._chat_ts < ttl:
            return self._chat_map
        out = self._decrypt("session/session.db")
        if not out:
            return self._chat_map
        m: dict[str, str] = {}
        c = sqlite3.connect(out)
        try:
            for (uname,) in c.execute("SELECT username FROM SessionTable"):
                if uname:
                    h = hashlib.md5(uname.encode()).hexdigest()
                    m[f"Msg_{h}"] = uname
        except sqlite3.OperationalError:
            pass
        c.close()
        self._chat_map, self._chat_ts = m, time.time()
        return m

    def _display(self, wxid: str) -> str:
        c = self.contacts().get(wxid)
        return c["name"] if c else wxid

    def search_contacts(self, keyword: str, limit: int = 30) -> list[dict]:
        """在【全部联系人/群】里按关键词搜（备注/昵称/微信号），类似微信搜索框。"""
        kw = (keyword or "").strip()
        if not kw:
            return []
        out = self._decrypt("contact/contact.db")
        if not out:
            return []
        like = f"%{kw}%"
        c = sqlite3.connect(out)
        try:
            cur = c.execute(
                "SELECT username, remark, nick_name, alias FROM contact "
                "WHERE delete_flag=0 AND (remark LIKE ? OR nick_name LIKE ? OR alias LIKE ? OR username LIKE ?) "
                "LIMIT ?", (like, like, like, like, limit * 2))
        except sqlite3.OperationalError:
            c.close()
            return []
        rows: list[dict] = []
        for uname, remark, nick, alias in cur:
            if not uname or uname.startswith(("gh_", "@")) or "placeholder" in uname:
                continue
            name = (remark or "").strip() or (nick or "").strip() or (alias or "") or uname
            rows.append({"username": uname, "name": name, "is_group": uname.endswith("@chatroom")})
            if len(rows) >= limit:
                break
        c.close()
        return rows

    def list_sessions(self, keyword: str = "", limit: int = 40) -> list[dict]:
        """列出最近会话 [{username, name, is_group}]，可按关键词（名字或 id）过滤。"""
        out = self._decrypt("session/session.db")
        if not out:
            return []
        contacts = self.contacts()
        kw = keyword.strip().lower()
        rows: list[dict] = []
        c = sqlite3.connect(out)
        try:
            cur = c.execute("SELECT username FROM SessionTable ORDER BY sort_timestamp DESC")
        except sqlite3.OperationalError:
            c.close()
            return []
        SKIP = ("gh_", "@placeholder", "brandsessionholder", "notifymessage", "weixin")
        for (uname,) in cur:
            if not uname or any(uname.startswith(s) or s in uname for s in SKIP):
                continue
            name = contacts.get(uname, {}).get("name", uname)
            if kw and kw not in name.lower() and kw not in uname.lower():
                continue
            rows.append({"username": uname, "name": name, "is_group": uname.endswith("@chatroom")})
            if len(rows) >= limit:
                break
        c.close()
        return rows

    # -------------------------------------------------------------- 快照/解析
    def snapshot(self) -> dict[tuple[str, int], Message]:
        """返回 {(表, local_id): Message}，覆盖所有消息库。"""
        self.refresh_keys()
        chat_map = self.chat_map()
        msgs: dict[tuple[str, int], Message] = {}
        for rel in MESSAGE_DBS:
            out = self._decrypt(rel)
            if not out:
                continue
            c = sqlite3.connect(out)
            c.row_factory = sqlite3.Row
            try:
                id2name = {r[0]: r[1] for r in c.execute("SELECT rowid, user_name FROM Name2Id")}
            except sqlite3.OperationalError:
                id2name = {}          # 新账号/无消息时 Name2Id 表尚不存在
            tabs = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
            for t in tabs:
                chat_id = chat_map.get(t, t)
                cinfo = self.contacts().get(chat_id, {})
                is_group = cinfo.get("is_group", chat_id.endswith("@chatroom"))
                chat_name = cinfo.get("name", chat_id)
                for r in c.execute(
                    f"SELECT local_id, local_type, real_sender_id, create_time, "
                    f"message_content, compress_content FROM {t}"
                ):
                    msgs[(t, r["local_id"])] = self._row_to_msg(
                        t, r, id2name, chat_id, chat_name, is_group)
            c.close()
        return msgs

    def _row_to_msg(self, table, r, id2name, chat_id, chat_name, is_group) -> Message:
        lt = r["local_type"] or 0
        typ = lt & 0xFFFFFFFF
        sub = lt >> 32
        sender_wxid = id2name.get(r["real_sender_id"], "") if r["real_sender_id"] else ""
        is_self = bool(sender_wxid) and sender_wxid == self.self_id()
        if not sender_wxid and not is_group:
            sender_wxid = chat_id
        raw = _decompress(r["message_content"]) or _decompress(r["compress_content"]) or ""
        text = self._render(typ, sub, raw)
        return Message(
            chat_id=chat_id, chat_name=chat_name, is_group=is_group,
            sender_id=sender_wxid, sender_name=self._display(sender_wxid) if sender_wxid else "",
            type=typ, sub_type=sub, text=text, time=r["create_time"] or 0,
            local_id=r["local_id"], table=table, is_self=is_self,
            raw=raw if isinstance(raw, str) else "",
        )

    def _render(self, typ: int, sub: int, raw) -> str:
        if not isinstance(raw, str):
            raw = ""
        # 群消息正文常是 "wxid:\n实际内容"，去掉前缀
        body = re.sub(r"^[\w\-@.]+:\n", "", raw, count=1)
        if typ == 1:
            return body
        if typ == 49:
            return f"[{TYPE_LABEL[49]}] {_appmsg_title(raw)}".strip()
        label = TYPE_LABEL.get(typ, f"类型{typ}")
        return f"[{label}]"

    # -------------------------------------------------------------- 监听
    def _wal_sig(self):
        sig = []
        for rel in MESSAGE_DBS:
            w = self._path(rel) + "-wal"
            try:
                st = os.stat(w)
                sig.append((st.st_mtime, st.st_size))
            except OSError:
                sig.append((0, 0))
        return tuple(sig)

    def poll_loop(self, interval: float = 0.5):
        """建立基线后持续监听，新消息经 on_message 回调。阻塞运行。"""
        if not self.refresh_keys():
            raise RuntimeError("未取到密钥，微信可能未登录")
        prev = set(self.snapshot().keys())
        sig = self._wal_sig()
        while True:
            cur_sig = self._wal_sig()
            if cur_sig != sig:
                sig = cur_sig
                cur = self.snapshot()
                new = [k for k in cur if k not in prev]
                for k in sorted(new, key=lambda k: cur[k].time):
                    if self.on_message:
                        try:
                            self.on_message(cur[k])
                        except Exception as e:  # noqa: BLE001
                            print("on_message 异常:", e, flush=True)
                prev = set(cur.keys())
            time.sleep(interval)


if __name__ == "__main__":  # 自测：python -m collector /data/home
    import datetime
    import sys

    home = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("WECHAT_HOME", "/data/home")
    col = Collector(home)
    snap = col.snapshot()
    rows = sorted(snap.values(), key=lambda m: m.time, reverse=True)[:15]
    print(f"消息库共 {len(snap)} 条，最近 15 条：")
    for m in rows:
        t = datetime.datetime.fromtimestamp(m.time).strftime("%m-%d %H:%M") if m.time else "?"
        who = f"{m.chat_name}" + (f"/{m.sender_name}" if m.is_group else "")
        print(f"{t} [{who}] {m.text[:50].replace(chr(10), ' ')}")
