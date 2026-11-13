"""微信 ⇄ Telegram 桥接。

微信 → Telegram：运行 Collector 监听新消息，格式化后发到 TG_CHAT_ID。
Telegram → 微信：在目标 chat 里【回复】某条转发消息并写内容，即把回复发回对应微信会话
                 （用 send.py 打开该会话再发送；定位用 OCR）。

配置来自 config.load()：tg_bot_token / tg_chat_id / wechat_home。
未配置 token 时进程空转（便于 supervisord 常驻，待面板配置后自动生效）。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

try:
    from . import config, send
    from .collector import Collector, Message
except ImportError:  # 独立运行
    import config
    import send
    from collector import Collector, Message

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

POLL_CONFIG_SEC = 5          # 未配置时多久重新读一次配置


def format_wx(m: Message) -> str:
    head = f"【{m.chat_name}】"
    if m.is_group and m.sender_name:
        head += f"{m.sender_name}:\n"
    body = m.text or ""
    return head + body


class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.chat_id = int(cfg["tg_chat_id"]) if str(cfg.get("tg_chat_id", "")).lstrip("-").isdigit() else None
        self.home = cfg["wechat_home"]
        self.loop: asyncio.AbstractEventLoop | None = None
        self.app: Application | None = None
        # tg 转发消息 id → 微信会话（用于回复路由）
        self.route: dict[int, dict] = {}
        self.col = Collector(self.home, on_message=self._on_wx)

    # -------------------------------------------------- 微信 → TG
    def _on_wx(self, m: Message):
        """Collector 线程回调：把消息投递到事件循环发送。"""
        if not self.cfg.get("forward_enabled", True) or not self.loop or not self.app:
            return
        asyncio.run_coroutine_threadsafe(self._send_tg(m), self.loop)

    async def _send_tg(self, m: Message):
        try:
            msg = await self.app.bot.send_message(self.chat_id, format_wx(m))
            # 记录路由：在 TG 里回复这条，就会发回该微信会话
            self.route[msg.message_id] = {"chat_id": m.chat_id, "chat_name": m.chat_name}
            # 限制内存
            if len(self.route) > 5000:
                for k in list(self.route)[:1000]:
                    self.route.pop(k, None)
        except Exception as e:  # noqa: BLE001
            print("发送到 TG 失败:", e, flush=True)

    # -------------------------------------------------- TG → 微信
    async def on_tg_reply(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self.cfg.get("reply_enabled", True):
            return
        msg = update.effective_message
        if not msg or not msg.reply_to_message or not msg.text:
            return
        target = self.route.get(msg.reply_to_message.message_id)
        if not target:
            await msg.reply_text("⚠️ 找不到这条消息对应的微信会话（重启后路由会丢失）。请回复较新的转发消息。")
            return
        # send 是阻塞的 xdotool 操作，放线程里
        r = await asyncio.to_thread(send.send_to_chat, target["chat_name"], target["chat_id"], msg.text)
        await msg.reply_text(("✅ 已发送到：" if r.get("ok") else "❌ 发送失败：") + f"{target['chat_name']}"
                             + ("" if r.get("ok") else f"（{r.get('msg')}）"))

    # -------------------------------------------------- 运行
    async def run(self):
        self.app = Application.builder().token(self.cfg["tg_bot_token"]).build()
        self.app.add_handler(MessageHandler(
            filters.Chat(self.chat_id) & filters.TEXT & filters.REPLY, self.on_tg_reply))
        self.loop = asyncio.get_running_loop()
        # 采集线程
        threading.Thread(target=self._collector_loop, daemon=True).start()
        print(f"[bridge] 已启动，转发目标 chat_id={self.chat_id}", flush=True)
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        try:
            await self.app.bot.send_message(self.chat_id, "✅ wechat2tg 桥接已连接。")
        except Exception as e:  # noqa: BLE001
            print("连接测试消息发送失败（检查 chat_id / 是否已对 bot 发过 /start）:", e, flush=True)
        # 常驻
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
