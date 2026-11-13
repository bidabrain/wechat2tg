"""微信 ⇄ Telegram 桥接（Topic 模式）。

每个微信会话对应 Telegram 群里的一个 Topic（话题）：
  微信 → TG：新消息发到对应会话的 topic（没有就自动建）。
  TG → 微信：你直接在某个 topic 里发言，就发回该 topic 对应的微信会话（无需回复某条消息）。

topic 映射持久化到 /data/topics.json，桥接重启不丢。
前提：目标群需开启 Topics（论坛模式），且 bot 为管理员并有「管理话题」权限。

配置来自 config.load()：tg_bot_token / tg_chat_id / wechat_home。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

try:
    from . import config, send
    from .collector import Collector, Message
    from .common import DATA
except ImportError:  # 独立运行
    import config
    import send
    from collector import Collector, Message
    from common import DATA

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

POLL_CONFIG_SEC = 5
TOPICS_PATH = DATA / "topics.json"


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chat_id = int(cfg["tg_chat_id"]) if str(cfg.get("tg_chat_id", "")).lstrip("-").isdigit() else None
        self.home = cfg["wechat_home"]
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app: Application | None = None
        self._topic_lock = asyncio.Lock()
        self._forum_ok = True              # 群是否为论坛（建 topic 失败则置 False，降级发到 General）
        # 持久化映射：wechat_chat_id -> {topic_id, name}
        self.map: dict[str, dict] = {}
        self.rev: dict[int, str] = {}
        self._load_topics()
        # 桥接刚发到微信的消息 [(chat_id, 规范化文本, 时间)]，用于只过滤桥接自身的回声
        self._recent_sent: list[tuple[str, str, float]] = []
        self.col = Collector(self.home, on_message=self._on_wx)

    @staticmethod
    def _norm(s: str) -> str:
        return "".join((s or "").split())

    def _is_bridge_echo(self, m: "Message") -> bool:
        """自己发出的消息里，只有与桥接刚发出的 (会话, 文本) 匹配的才算回声。"""
        now = time.time()
        self._recent_sent = [e for e in self._recent_sent if now - e[2] < 120]
        key = self._norm(m.text)
        for i, (cid, txt, _) in enumerate(self._recent_sent):
            if cid == m.chat_id and txt == key:
                self._recent_sent.pop(i)
                return True
        return False

    # -------------------------------------------------- 映射持久化
    def _load_topics(self):
        if TOPICS_PATH.exists():
            try:
                self.map = json.loads(TOPICS_PATH.read_text("utf-8"))
                self.rev = {v["topic_id"]: k for k, v in self.map.items()}
            except Exception:  # noqa: BLE001
                self.map, self.rev = {}, {}

    def _save_topics(self):
        try:
            tmp = TOPICS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.map, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(TOPICS_PATH)
        except Exception as e:  # noqa: BLE001
            print("[bridge] 保存 topics 失败:", e, flush=True)

    async def _topic_for(self, chat_id: str, chat_name: str) -> int | None:
        """返回该微信会话的 topic 线程 id；没有则创建。群非论坛时返回 None（发到 General）。"""
        hit = self.map.get(chat_id)
        if hit:
            return hit["topic_id"]
        if not self._forum_ok:
            return None
        async with self._topic_lock:
            hit = self.map.get(chat_id)
            if hit:
                return hit["topic_id"]
            try:
                t = await self.app.bot.create_forum_topic(self.chat_id, name=(chat_name or chat_id)[:120])
                tid = t.message_thread_id
            except Exception as e:  # noqa: BLE001
                self._forum_ok = False
                print(f"[bridge] 建 topic 失败（群是否开启 Topics？bot 是否管理员且有管理话题权限？）: {e}", flush=True)
                return None
            self.map[chat_id] = {"topic_id": tid, "name": chat_name}
            self.rev[tid] = chat_id
            self._save_topics()
            return tid

    # -------------------------------------------------- 微信 → TG
    def _on_wx(self, m: Message):
        if not self.cfg.get("forward_enabled", True) or not self.loop or not self.app:
            return
        if m.is_self and self._is_bridge_echo(m):
            return                         # 只跳过桥接自己发的那条（避免回声）；
                                           # 从微信客户端/手机发的自己消息照常转发
        asyncio.run_coroutine_threadsafe(self._send_tg(m), self.loop)

    async def _send_tg(self, m: Message):
        try:
            tid = await self._topic_for(m.chat_id, m.chat_name)
            text = m.text or ""
            if m.is_group and m.sender_name:
                text = f"{m.sender_name}:\n{text}"
            elif m.is_self:
                text = f"🟢 我：{text}"           # 自己从微信客户端/手机发的
            elif tid is None:
                text = f"【{m.chat_name}】{text}"   # 没有 topic 时带上会话名
            await self.app.bot.send_message(self.chat_id, text, message_thread_id=tid)
        except Exception as e:  # noqa: BLE001
            print("发送到 TG 失败:", e, flush=True)

    # -------------------------------------------------- TG → 微信
    async def on_tg_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.cfg.get("reply_enabled", True):
            return
        msg = update.effective_message
        if not msg or not msg.text:
            return
        tid = msg.message_thread_id
        chat_id = self.rev.get(tid) if tid else None
        if not chat_id:
            return                         # General 或未映射的 topic，忽略
        name = self.map[chat_id]["name"]
        r = await asyncio.to_thread(send.send_to_chat, name, chat_id, msg.text)
        if r.get("ok"):
            # 记下这条，稍后采集到同样的「自己发出」消息时识别为回声并跳过
            self._recent_sent.append((chat_id, self._norm(msg.text), time.time()))
        else:
            await msg.reply_text(f"❌ 发送失败：{r.get('msg')}")

    # -------------------------------------------------- 运行
    async def run(self):
        self.app = Application.builder().token(self.cfg["tg_bot_token"]).build()
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.TEXT & ~filters.COMMAND, self.on_tg_message))
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self._collector_loop, daemon=True).start()
        print(f"[bridge] 已启动，目标群 chat_id={self.chat_id}，已载入 {len(self.map)} 个 topic 映射", flush=True)
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        try:
            await self.app.bot.send_message(self.chat_id, "✅ wechat2tg 桥接已连接（Topic 模式）。")
        except Exception as e:  # noqa: BLE001
            print("连接测试消息发送失败（检查 chat_id / bot 是否在群里）:", e, flush=True)
        while True:
            await asyncio.sleep(3600)

    def _collector_loop(self):
        while True:
            try:
                if self.col.ready():
                    self.col.poll_loop()
                else:
                    print("[bridge] 微信未登录/未就绪，等待…", flush=True)
            except Exception as e:  # noqa: BLE001
                print("[bridge] 采集循环异常，5s 后重试:", e, flush=True)
            time.sleep(5)


def main():
    while True:
        cfg = config.load()
        if not cfg.get("tg_bot_token") or not cfg.get("tg_chat_id"):
            print("[bridge] 未配置 TG_BOT_TOKEN / TG_CHAT_ID，等待面板配置…", flush=True)
            time.sleep(POLL_CONFIG_SEC)
            continue
        try:
            asyncio.run(Bridge(cfg).run())
        except Exception as e:  # noqa: BLE001
            print("[bridge] 退出，10s 后重启:", e, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
