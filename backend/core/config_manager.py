"""数据库先提交、内存后发布的异步运行时配置总线。"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from backend.core.repository import repo

logger = logging.getLogger(__name__)
DEFAULT_CONFIGS: dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "admin_password": "",
    "llm_base_url": "https://open.bigmodel.cn/api/paas/v4/",
    "llm_api_key": "", "llm_model": "glm-4-flash", "llm_timeout_seconds": 30,
    "default_schedule_remind_minutes": [10, 0],
    "default_ddl_advances": [1440, 720],
    "wechat_claw_enabled": False, "wechat_claw_token": "",
    "wechat_claw_base_url": "https://ilinkai.weixin.qq.com",
    "dingtalk_enabled": False, "dingtalk_client_id": "", "dingtalk_client_secret": "",
    "telegram_enabled": False, "telegram_bot_token": "", "telegram_proxy_url": "",
    "caldav_enabled": False, "caldav_url": "https://caldav.icloud.com",
    "caldav_username": "", "caldav_password": "", "caldav_calendar_name": "",
    "google_calendar_enabled": False, "google_calendar_id": "primary",
    "google_credentials": {}, "google_credentials_json": "",
    "google_client_id": "", "google_client_secret": "",
    "google_refresh_token": "",
}


class RuntimeConfigManager:
    def __init__(self) -> None:
        self._configs = deepcopy(DEFAULT_CONFIGS)
        self._listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            stored = await repo.get_all_configs()
            self._configs = {**deepcopy(DEFAULT_CONFIGS), **deepcopy(stored)}

    def get(self, key: str, default: Any = None) -> Any:
        return deepcopy(self._configs.get(key, default))

    def get_all(self) -> dict[str, Any]:
        # 内部游标/周期锚点由各模块独立写仓库，不向Web表单暴露旧快照。
        return deepcopy({k: v for k, v in self._configs.items()
                         if not k.startswith("_scheduler_") and k != "wechat_claw_poll_state"})

    def register_listener(self, callback: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        if not callable(callback):
            raise TypeError("配置监听器必须可调用")
        if callback not in self._listeners:
            self._listeners.append(callback)

    async def update_configs(self, updates: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(updates, dict) or any(not isinstance(k, str) for k in updates):
            raise TypeError("配置必须是字符串键字典")
        updates = deepcopy(updates)
        updates = {k: v for k, v in updates.items()
                   if not k.startswith("_scheduler_") and k != "wechat_claw_poll_state"}
        for key, value in updates.items():
            if key.endswith("_enabled") and not isinstance(value, bool):
                raise ValueError(f"{key} 必须为布尔值")
            if key in ("default_schedule_remind_minutes", "default_ddl_advances"):
                if not isinstance(value, list) or any(type(v) is not int or v < 0 for v in value):
                    raise ValueError(f"{key} 必须为非负整数列表")
            if key == "timezone":
                ZoneInfo(value)
        async with self._lock:
            await repo.update_configs(updates)
            changed = {k: v for k, v in updates.items() if self._configs.get(k) != v}
            self._configs.update(updates)
        # 不持有写锁调用外部模块，监听器可以再次读取/更新配置。
        # 消费者应读取当前快照，以免并发更新的旧通知覆盖最新状态。
        if changed:
            for callback in tuple(self._listeners):
                try:
                    await callback(deepcopy(changed))
                except Exception:
                    logger.exception("配置已保存，但监听器热重载失败: %s", getattr(callback, "__qualname__", "listener"))
        return self.get_all()


config_manager = RuntimeConfigManager()
