"""Google Calendar OAuth event CRUD without blocking the asyncio loop."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from backend.core.schemas import BaseCalendarProvider

if TYPE_CHECKING:
    from backend.core.database import TaskItem


class GoogleCalendarProvider(BaseCalendarProvider):
    def __init__(self, config: dict[str, Any]):
        self.config = dict(config)
        self.calendar_id = self.config.get("google_calendar_id") or "primary"

    def _service(self):
        import httplib2
        from google.oauth2.credentials import Credentials
        from google_auth_httplib2 import AuthorizedHttp
        from googleapiclient.discovery import build

        info = self.config.get("google_credentials") or {}
        if isinstance(info, str):
            info = json.loads(info)
        if not isinstance(info, dict):
            raise ValueError("Google OAuth 凭证必须是 JSON 对象")
        credentials = Credentials(
            token=info.get("token") or info.get("access_token"),
            refresh_token=info.get("refresh_token") or self.config.get("google_refresh_token"),
            token_uri=info.get("token_uri") or "https://oauth2.googleapis.com/token",
            client_id=info.get("client_id") or self.config.get("google_client_id"),
            client_secret=info.get("client_secret") or self.config.get("google_client_secret"),
            scopes=["https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/calendar.readonly"],
        )
        if not credentials.token and not credentials.refresh_token:
            raise ValueError("请配置 Google OAuth Refresh Token 或有效 Access Token")
        timeout = float(self.config.get("calendar_timeout_seconds", 30))
        # AuthorizedHttp refreshes expired tokens using the same bounded transport.
        http = AuthorizedHttp(credentials, http=httplib2.Http(timeout=timeout))
        return build("calendar", "v3", http=http, cache_discovery=False)

    def _aware(self, value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(self.config.get("timezone", "Asia/Shanghai")))

    def _body(self, task: TaskItem) -> dict[str, Any]:
        is_ddl = task.task_type == "memo_ddl"
        start = task.deadline if is_ddl else task.start_time
        if start is None:
            raise ValueError("同步日历需要日程开始时间或备忘录截止时间")
        start = self._aware(start)
        end = self._aware(task.end_time) if not is_ddl and task.end_time else start + timedelta(minutes=30)
        if end <= start:
            raise ValueError("日历事件结束时间必须晚于开始时间")
        timezone_name = self.config.get("timezone", "Asia/Shanghai")
        advances = task.remind_advance_minutes or self.config.get(
            "default_ddl_advances" if is_ddl else "default_schedule_remind_minutes",
            [1440, 720] if is_ddl else [10, 0],
        )
        # Google accepts at most five overrides, between zero and four weeks.
        minutes = sorted(set(int(value) for value in advances))
        if any(value < 0 or value > 40320 for value in minutes):
            raise ValueError("Google 日历提前提醒需在 0 至 40320 分钟内")
        if len(minutes) > 5:
            raise ValueError("Google 日历最多允许五个提醒")
        body: dict[str, Any] = {
            "summary": ("[DDL截止] " if is_ddl else "") + task.title,
            "description": task.description or "",
            "start": {"dateTime": start.isoformat(), "timeZone": timezone_name},
            "end": {"dateTime": end.isoformat(), "timeZone": timezone_name},
            "status": "cancelled" if task.status == "cancelled" else "confirmed",
            "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": value} for value in minutes]},
            "recurrence": [],
        }
        if task.rrule_str:
            rule = task.rrule_str.strip()
            body["recurrence"] = [rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"]
        return body

    @staticmethod
    def _close(service) -> None:
        close = getattr(service, "close", None)
        if close is not None:
            close()

    def _create(self, task: TaskItem) -> str:
        from googleapiclient.errors import HttpError

        body = self._body(task)
        identity = f"chrono:{task.id}:{task.created_at}:{task.source_channel}:{task.sender_id}"
        # Google IDs use base32hex characters; a UUID hex is a valid subset.
        event_id = uuid5(NAMESPACE_URL, identity).hex
        body["id"] = event_id
        service = self._service()
        try:
            try:
                event = service.events().insert(calendarId=self.calendar_id, body=body).execute()
            except HttpError as exc:
                if exc.resp.status != 409:
                    raise
                event = service.events().get(calendarId=self.calendar_id, eventId=event_id).execute()
            return str(event["id"])
        finally:
            self._close(service)

    async def create_event(self, task: TaskItem) -> str:
        return await asyncio.to_thread(self._create, task)

    def _update(self, task: TaskItem) -> bool:
        if not task.cal_event_uid:
            return False
        body = self._body(task)
        service = self._service()
        try:
            service.events().patch(calendarId=self.calendar_id, eventId=task.cal_event_uid, body=body).execute()
            return True
        finally:
            self._close(service)

    async def update_event(self, task: TaskItem) -> bool:
        return await asyncio.to_thread(self._update, task)

    def _delete(self, cal_event_uid: str) -> bool:
        from googleapiclient.errors import HttpError

        service = self._service()
        try:
            try:
                service.events().delete(calendarId=self.calendar_id, eventId=cal_event_uid).execute()
            except HttpError as exc:
                if exc.resp.status not in (404, 410):
                    raise
            return True
        finally:
            self._close(service)

    async def delete_event(self, cal_event_uid: str) -> bool:
        return await asyncio.to_thread(self._delete, cal_event_uid)

    def _test(self) -> tuple[bool, str]:
        service = self._service()
        try:
            service.events().list(calendarId=self.calendar_id, maxResults=1).execute()
            return True, "Google Calendar 连接成功"
        finally:
            self._close(service)

    async def test_connection(self) -> tuple[bool, str]:
        return await asyncio.to_thread(self._test)
