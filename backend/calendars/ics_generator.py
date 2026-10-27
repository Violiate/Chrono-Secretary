"""
backend/calendars/ics_generator.py
==================================
手机本地日历 ICS 订阅源生成器（DeepSeek 负责 / 阶段 D1）。

设计思路：
- 只输出 ``status == "pending"`` 的任务，已完成/已取消的东西不该继续骚扰用户；
- 每条任务生成一个标准 ``VEVENT``，并挂上 ``VALARM`` 原生提醒；
- 输出是**纯文本**（``text/calendar``），手机系统日历通过 URL 订阅后会自动定期拉取，
  所以「网页端改期 -> 手机日历自动更新」这条链路是零配置、零成本的。

时间约定：DTSTART / DTEND 使用不带 TZID 的「floating time」，
由手机按本机时区解释，这样国内外用户都不用填时区。
"""

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from icalendar import Alarm, Calendar, Event
from icalendar.prop import vRecur

from backend.core.database import TaskItem
from backend.core.repository import repo

logger = logging.getLogger(__name__)

PRODID = "-//ChronoSecretary//CN"
CAL_NAME = "AI私人秘书日程与DDL"
CAL_DESC = "由 Chrono Secretary 自动生成的日程与 DDL 截止提醒订阅源（动态刷新）"

# 兜底提醒策略（当任务本身与运行时配置都没给提醒分钟数时使用）
DEFAULT_SCHEDULE_REMIND = (10, 0)          # 提前 10 分钟 + 到点
DEFAULT_DDL_REMIND = (1440, 720)           # 提前 1 天 + 提前半天

EVENT_DEFAULT_DURATION = timedelta(minutes=30)


def _load_runtime_defaults() -> Tuple[List[int], List[int]]:
    """延迟导入 config_manager，读取用户在前端配置的默认提醒提前量。

    延迟导入 + 全异常兜底：即使 config_manager 尚未初始化（例如单元测试、
    或 GPT 侧模块导入顺序不同），ICS 生成也必须能用。
    """
    try:
        from backend.core.config_manager import config_manager  # 延迟导入，避免循环依赖

        schedule_defaults = config_manager.get("default_schedule_remind_minutes", list(DEFAULT_SCHEDULE_REMIND))
        ddl_defaults = config_manager.get("default_ddl_advances", list(DEFAULT_DDL_REMIND))
    except Exception as exc:  # pragma: no cover - 仅在配置总线不可用时触发
        logger.debug("读取默认提醒配置失败，使用内置兜底值: %s", exc)
        return list(DEFAULT_SCHEDULE_REMIND), list(DEFAULT_DDL_REMIND)

    def _normalize(value, fallback: Tuple[int, ...]) -> List[int]:
        result: List[int] = []
        if isinstance(value, (list, tuple, set)):
            for item in value:
                try:
                    minutes = int(float(str(item).strip()))
                except (TypeError, ValueError):
                    continue
                if minutes >= 0 and minutes not in result:
                    result.append(minutes)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            result = [int(value)]
        return result or list(fallback)

    return _normalize(schedule_defaults, DEFAULT_SCHEDULE_REMIND), _normalize(ddl_defaults, DEFAULT_DDL_REMIND)


def _build_rrule(rrule_str: Optional[str]):
    """把 ``FREQ=DAILY;INTERVAL=1`` 之类的字符串转成 icalendar 的 vRecur。

    优先用官方 ``vRecur.from_ical``；失败时手动解析，保证一个坏 RRULE
    （比如用户手写错的）不会让整个订阅源崩掉。
    """
    if not rrule_str:
        return None

    text = str(rrule_str).strip()
    if not text:
        return None
    if text.upper().startswith("RRULE:"):
        text = text[6:].strip()

    try:
        return vRecur.from_ical(text)
    except Exception:
        parts = {}
        for chunk in text.split(";"):
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            key = key.strip().upper()
            if not key:
                continue
            parts[key] = [item.strip() for item in value.split(",") if item.strip()]
        if not parts:
            logger.warning("无法解析 RRULE，已忽略: %r", rrule_str)
            return None
        try:
            return vRecur(parts)
        except Exception:
            logger.warning("无法构建 RRULE，已忽略: %r", rrule_str)
            return None


def _as_datetime(value, fallback: datetime) -> datetime:
    """确保 dtstart/dtend 是 naive datetime（icalendar 不接受 None）。"""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None)
        return value
    return fallback


class ICSGenerator:
    """把数据库中的待办任务渲染成一份可被手机日历订阅的 iCalendar 文档。"""

    def __init__(self) -> None:
        self._last_generated_at: Optional[datetime] = None

    # ------------------------------------------------------------------ #
    # 单个任务 -> VEVENT
    # ------------------------------------------------------------------ #
    def _task_to_event(
        self,
        task: TaskItem,
        schedule_defaults: List[int],
        ddl_defaults: List[int],
    ) -> Optional[Event]:
        now = datetime.now()
        is_ddl = (task.task_type or "").lower() == "memo_ddl"

        if is_ddl:
            anchor = task.deadline or task.start_time
            if anchor is None:
                # 没有截止时间的备忘录不进日历（否则会变成一条没有意义的「现在」事件）
                logger.debug("任务 #%s 是 DDL 但没有截止时间，跳过 ICS 输出", task.id)
                return None
            start = _as_datetime(anchor, now)
            end = start + EVENT_DEFAULT_DURATION
            summary = f"[DDL截止] {task.title or '未命名任务'}"
            default_advances = ddl_defaults
        else:
            anchor = task.start_time or task.deadline
            if anchor is None:
                logger.debug("任务 #%s 是日程但没有开始时间，跳过 ICS 输出", task.id)
                return None
            start = _as_datetime(anchor, now)
            end_value = task.end_time if isinstance(task.end_time, datetime) else None
            end = _as_datetime(end_value, start + EVENT_DEFAULT_DURATION) if end_value else start + EVENT_DEFAULT_DURATION
            if end <= start:
                end = start + EVENT_DEFAULT_DURATION
            summary = task.title or "未命名日程"
            default_advances = schedule_defaults

        event = Event()
        uid = (task.cal_event_uid or "").strip() or f"chrono-task-{task.id}@chrono-secretary"
        event.add("uid", uid)
        event.add("summary", summary)
        event.add("dtstart", start)
        event.add("dtend", end)
        event.add("dtstamp", now)
        event.add("created", task.created_at or now)
        event.add("last-modified", task.updated_at or now)
        event.add("sequence", 0)
        event.add("status", "CONFIRMED")
        event.add("transp", "OPAQUE")

        description_parts: List[str] = []
        if task.description:
            description_parts.append(str(task.description).strip())
        description_parts.append(f"类型：{'DDL 备忘录' if is_ddl else '日程安排'}")
        if task.rrule_str:
            description_parts.append(f"重复规则：{task.rrule_str}")
        description_parts.append(f"来源渠道：{task.source_channel or 'web'}")
        description_parts.append(f"由 Chrono Secretary 生成 · 任务 #{task.id}")
        description_parts.append("💡 在网页端或 IM 中引用机器人消息即可改期 / 销项，日历会自动同步。")
        event.add("description", "\n".join(part for part in description_parts if part))

        categories = ["DDL截止"] if is_ddl else ["日程"]
        event.add("categories", categories)

        rrule = _build_rrule(task.rrule_str)
        if rrule is not None:
            event.add("rrule", rrule)

        # ---- VALARM：原生提醒，手机系统日历到点自己弹 ----
        advances = task.remind_advance_minutes or list(default_advances)
        for minutes in sorted({int(m) for m in advances if int(m) >= 0}, reverse=True):
            alarm = Alarm()
            alarm.add("action", "DISPLAY")
            alarm.add("description", f"⏰ {summary}")
            alarm.add("trigger", timedelta(minutes=-minutes))
            event.add_component(alarm)

        return event

    # ------------------------------------------------------------------ #
    # 全量订阅源
    # ------------------------------------------------------------------ #
    async def generate_ics_feed(self) -> str:
        """生成完整的 ``.ics`` 文本（UTF-8 字符串）。"""
        tasks = await repo.list_tasks(status="pending")
        schedule_defaults, ddl_defaults = _load_runtime_defaults()

        calendar = Calendar()
        calendar.add("prodid", PRODID)
        calendar.add("version", "2.0")
        calendar.add("calscale", "GREGORIAN")
        calendar.add("method", "PUBLISH")
        calendar.add("x-wr-calname", CAL_NAME)
        calendar.add("x-wr-caldesc", CAL_DESC)
        calendar.add("x-published-ttl", "PT30M")  # 建议客户端 30 分钟刷新一次

        now = datetime.now()
        calendar.add("dtstamp", now)

        emitted = 0
        for task in tasks:
            try:
                event = self._task_to_event(task, schedule_defaults, ddl_defaults)
            except Exception as exc:  # 单条坏数据不影响整份订阅源
                logger.warning("任务 #%s 生成 VEVENT 失败，已跳过: %s", getattr(task, "id", "?"), exc)
                continue
            if event is not None:
                calendar.add_component(event)
                emitted += 1

        self._last_generated_at = now
        ics_bytes = calendar.to_ical()
        logger.info("ICS 订阅源已生成：%s 条事件（共 %s 条待办）", emitted, len(tasks))
        return ics_bytes.decode("utf-8")

    async def generate_ics_bytes(self) -> bytes:
        """字节版本，方便直接塞进 ``Response``。"""
        return (await self.generate_ics_feed()).encode("utf-8")


# 全局单例：routes.py 里 `from backend.calendars.ics_generator import ics_generator`
ics_generator = ICSGenerator()

__all__ = ["ICSGenerator", "ics_generator", "PRODID", "CAL_NAME"]
