"""配置：环境变量优先，其次 /data/config.json（面板写入）。"""
from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("DATA_DIR", "/data")) / "config.json"

DEFAULTS = {
    "tg_bot_token": "",
    "tg_chat_id": "",          # 转发目标（你的 user id 或群 -100... id）
    "wechat_home": os.environ.get("WECHAT_HOME", "/data/home"),
    "forward_enabled": True,   # 微信 → Telegram 转发开关
    "reply_enabled": True,     # Telegram → 微信 回发开关
}
# 环境变量名 → 配置键
_ENV = {
    "TG_BOT_TOKEN": "tg_bot_token",
    "TG_CHAT_ID": "tg_chat_id",
    "WECHAT_HOME": "wechat_home",
}


def load() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text("utf-8")))
        except Exception:  # noqa: BLE001
            pass
    for env, key in _ENV.items():
        v = os.environ.get(env)
        if v:
            cfg[key] = v
    return cfg


def save(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    cur = {}
    if CONFIG_PATH.exists():
        try:
            cur = json.loads(CONFIG_PATH.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            cur = {}
    cur.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(CONFIG_PATH)
