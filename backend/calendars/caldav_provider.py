"""CalDAV event CRUD; all blocking library work runs in worker threads."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from backend.core.schemas import BaseCalendarProvider

if TYPE_CHECKING:
    from backend.core.database import TaskItem


class CalDAVProvider(BaseCalendarProvider):
    def __init__(self, config: dict[str, Any]):
        self.config = dict(config)

    def _connect(self):
        import caldav

        return caldav.DAVClient(
            url=self.config.get("caldav_url") or "https://caldav.icloud.com",
            username=self.config.get("caldav_username") or "",
            password=self.config.get("caldav_password") or "",
            timeout=float(self.config.get("calendar_timeout_seconds", 30)),
        )

    def _calendar(self, client):
        calendars = client.principal().calendars()
        name = self.config.get("caldav_calendar_name")
        if name:
            calendars = [calendar for calendar in calendars if calendar.name == name]
        if not calendars:
            raise ValueError("未找到可用的 CalDAV 日历，请检查账号及日历名称")
        return calendars[0]

    def _aware(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=ZoneInfo(self.config.get("timezone", "Asia/Shanghai")))
        return value

    def _serialize(self, task: TaskItem, uid: str) -> bytes:
        from icalendar import Alarm, Calendar, Event, vRecur

        is_ddl = task.task_type == "memo_ddl"
        start = task.deadline if is_ddl else task.start_time
        if start is None:
            raise ValueError("同步日历需要日程开始时间或备忘录截止时间")
        start = self._aware(start)
        end = self._aware(task.end_time) if not is_ddl and task.end_time else start + timedelta(minutes=30)
        if end <= start:
            raise ValueError("日历事件结束时间必须晚于开始时间")
        calendar = Calendar()
        calendar.add("prodid", "-//ChronoSecretary//CN")
        calendar.add("version", "2.0")
        event = Event()
        event.add("uid", uid)
        event.add("dtstamp", datetime.now(timezone.utc))
        event.add("summary", ("[DDL截止] " if is_ddl else "") + task.title)
        event.add("description", task.description or "")
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("status", "CANCELLED" if task.status == "cancelled" else "CONFIRMED")
        if task.rrule_str:
            rule = task.rrule_str.strip()
            if rule.upper().startswith("RRULE:"):
                rule = rule[6:]
            event.add("rrule", vRecur.from_ical(rule))
        advances = task.remind_advance_minutes or self.config.get(
            "default_ddl_advances" if is_ddl else "default_schedule_remind_minutes",
            [1440, 720] if is_ddl else [10, 0],
        )
        for minutes in sorted(set(int(value) for value in advances)):
            if minutes < 0:
                raise ValueError("提前提醒分钟数不能为负数")
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", task.title)
            alarm.add("trigger", -timedelta(minutes=minutes))
            event.add_component(alarm)
        calendar.add_component(event)
        return calendar.to_ical()

    @staticmethod
    def _close(client) -> None:
        close = getattr(client, "close", None)
        if close is not None:
            close()

    def _create(self, task: TaskItem) -> str:
        # Stable UID makes retrying a failed local persistence step idempotent.
        identity = f"chrono:{task.id}:{task.created_at}:{task.source_channel}:{task.sender_id}"
        uid = f"{uuid5(NAMESPACE_URL, identity)}@chrono-secretary"
        data = self._serialize(task, uid)
        client = self._connect()
        try:
            self._calendar(client).save_event(data)
            return uid
        finally:
            self._close(client)

    async def create_event(self, task: TaskItem) -> str:
        return await asyncio.to_thread(self._create, task)

    def _update(self, task: TaskItem) -> bool:
        if not task.cal_event_uid:
            return False
        data = self._serialize(task, task.cal_event_uid)
        client = self._connect()
        try:
            event = self._calendar(client).event_by_uid(task.cal_event_uid)
            event.data = data
            event.save()
            return True
        finally:
            self._close(client)

    async def update_event(self, task: TaskItem) -> bool:
        return await asyncio.to_thread(self._update, task)

    def _delete(self, cal_event_uid: str) -> bool:
        from caldav.lib.error import NotFoundError

        client = self._connect()
        try:
            try:
                self._calendar(client).event_by_uid(cal_event_uid).delete()
            except NotFoundError:
                # Removing an already absent event is a successful delete.
                return True
            return True
        finally:
            self._close(client)

    async def delete_event(self, cal_event_uid: str) -> bool:
        return await asyncio.to_thread(self._delete, cal_event_uid)

    def _test(self) -> tuple[bool, str]:
        client = self._connect()
        try:
            self._calendar(client)
            return True, "CalDAV 连接成功，已找到目标日历"
        finally:
            self._close(client)

    async def test_connection(self) -> tuple[bool, str]:
        return await asyncio.to_thread(self._test)
