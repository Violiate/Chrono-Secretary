"""Best-effort calendar fan-out with provider-specific remote identifiers.

The frozen database contract supplies a single string column. A versioned JSON
envelope preserves both CalDAV UIDs and Google event IDs without schema changes.
The providers read current configuration on every operation, so edits are hot.
Remote-to-local reconciliation is intentionally not implied by these CRUD APIs.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
from typing import Any, TYPE_CHECKING

from backend.core.config_manager import config_manager
from backend.calendars.caldav_provider import CalDAVProvider
from backend.calendars.google_provider import GoogleCalendarProvider

if TYPE_CHECKING:
    from backend.core.database import TaskItem

logger = logging.getLogger(__name__)


class CalendarManager:
    def __init__(self, config_manager=config_manager):
        self.config_manager = config_manager

    def _providers(self, config: dict[str, Any]) -> dict[str, Any]:
        providers: dict[str, Any] = {}
        if config.get("caldav_enabled", False):
            providers["caldav"] = CalDAVProvider(config)
        if config.get("google_calendar_enabled", False):
            providers["google"] = GoogleCalendarProvider(config)
        return providers

    @staticmethod
    def _decode(value: str, providers: dict[str, Any]) -> dict[str, str]:
        if value.startswith("chrono-cal-v1:"):
            data = json.loads(value[len("chrono-cal-v1:"):])
            if not isinstance(data, dict) or any(
                key not in ("caldav", "google") or not isinstance(uid, str) or not uid
                for key, uid in data.items()
            ):
                raise ValueError("无效的日历事件标识映射")
            return data
        # Compatibility with a pre-existing single-provider installation.
        if len(providers) == 1:
            return {next(iter(providers)): value}
        raise ValueError("无法确定旧日历标识所属平台，请仅启用其原始日历平台")

    async def sync_create_event(self, task: TaskItem) -> str | None:
        providers = self._providers(self.config_manager.get_all())
        if not providers:
            return None
        results = await asyncio.gather(
            *(provider.create_event(task) for provider in providers.values()),
            return_exceptions=True,
        )
        identifiers = {}
        for name, result in zip(providers, results):
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                logger.warning("Calendar create failed: provider=%s error=%s", name, type(result).__name__)
            elif result:
                identifiers[name] = str(result)
        return "chrono-cal-v1:" + json.dumps(identifiers, separators=(",", ":")) if identifiers else None

    async def sync_update_event(self, task: TaskItem) -> bool:
        providers = self._providers(self.config_manager.get_all())
        if not task.cal_event_uid:
            return not providers
        try:
            identifiers = self._decode(task.cal_event_uid, providers)
        except (ValueError, TypeError):
            logger.warning("Calendar update skipped: invalid or ambiguous event identifier")
            return False
        success = True
        for name, uid in identifiers.items():
            provider = providers.get(name)
            if provider is None:
                success = False
                continue
            try:
                # Do not mutate the repository's task object or leak a Google ID
                # to CalDAV when both providers are enabled.
                local = copy.copy(task)
                local.cal_event_uid = uid
                success = bool(await provider.update_event(local)) and success
            except Exception as exc:
                logger.warning("Calendar update failed: provider=%s error=%s", name, type(exc).__name__)
                success = False
        return success

    async def sync_delete_event(self, cal_event_uid: str) -> bool:
        if not cal_event_uid:
            return True
        providers = self._providers(self.config_manager.get_all())
        try:
            identifiers = self._decode(cal_event_uid, providers)
        except (ValueError, TypeError):
            logger.warning("Calendar delete skipped: invalid or ambiguous event identifier")
            return False
        success = True
        for name, uid in identifiers.items():
            provider = providers.get(name)
            if provider is None:
                success = False
                continue
            try:
                success = bool(await provider.delete_event(uid)) and success
            except Exception as exc:
                logger.warning("Calendar delete failed: provider=%s error=%s", name, type(exc).__name__)
                success = False
        return success

    async def test_connection(self, provider: str, config_override: dict | None = None) -> tuple[bool, str]:
        config = self.config_manager.get_all()
        if config_override:
            config.update(config_override)
        normalized = provider.lower().strip()
        if normalized in ("caldav", "icloud"):
            instance = CalDAVProvider(config)
        elif normalized in ("google", "google_calendar"):
            instance = GoogleCalendarProvider(config)
        else:
            return False, "不支持的日历平台，请选择 caldav 或 google"
        try:
            return await instance.test_connection()
        except Exception as exc:
            # Avoid returning exception bodies that may contain credentials/URLs.
            logger.warning("Calendar connection test failed: provider=%s error=%s", normalized, type(exc).__name__)
            return False, f"日历连接失败（{type(exc).__name__}），请检查凭证、网络及日历权限"


calendar_manager = CalendarManager()
