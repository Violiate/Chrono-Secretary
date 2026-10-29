"""Async reminders with durable recurrence anchors and quoted-message bindings.

APScheduler keeps only the next occurrence in memory. Repository tasks and the
reserved configuration key below are the durable source of truth on restart.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dateutil.rrule import rrulestr

from backend.core.config_manager import config_manager
from backend.core.repository import repo
from backend.core.schemas import ChannelType

if TYPE_CHECKING:
    from backend.core.database import TaskItem

logger = logging.getLogger(__name__)
_ANCHOR_KEY = "_scheduler_recurrence_anchors"


def _value(value: Any) -> str:
    return getattr(value, "value", value)


class SchedulerManager:
    def __init__(self) -> None:
        self._scheduler: AsyncIOScheduler | None = None
        self._lock = asyncio.Lock()
        self._anchors: dict[str, dict[str, str]] = {}
        self._anchors_loaded = False
        config_manager.register_listener(self._on_config_changed)

    def _aware(self, value: datetime) -> datetime:
        timezone = ZoneInfo(config_manager.get("timezone", "Asia/Shanghai"))
        return value.replace(tzinfo=timezone) if value.tzinfo is None else value.astimezone(timezone)

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(config_manager.get("timezone", "Asia/Shanghai")))

    async def _load_anchors(self) -> None:
        if not self._anchors_loaded:
            configs = await repo.get_all_configs()
            stored = configs.get(_ANCHOR_KEY, {})
            self._anchors = dict(stored) if isinstance(stored, dict) else {}
            self._anchors_loaded = True

    async def _save_anchors(self) -> None:
        # Called only while holding _lock, preventing concurrent map overwrites.
        await repo.update_configs({_ANCHOR_KEY: self._anchors})

    async def start(self) -> None:
        async with self._lock:
            if self._scheduler is not None and self._scheduler.running:
                return
            await self._load_anchors()
            scheduler = AsyncIOScheduler(timezone=config_manager.get("timezone", "Asia/Shanghai"))
            self._scheduler = scheduler
            scheduler.start(paused=True)
            try:
                tasks = await repo.list_tasks(status="pending")
                for task in tasks:
                    try:
                        await self._schedule(task)
                    except Exception:
                        logger.exception("Could not restore reminders for task %s", task.id)
                scheduler.resume()
            except Exception:
                scheduler.shutdown(wait=False)
                self._scheduler = None
                raise

    async def stop(self) -> None:
        async with self._lock:
            scheduler, self._scheduler = self._scheduler, None
            if scheduler is not None and scheduler.running:
                scheduler.shutdown(wait=False)
                # AsyncIOScheduler queues shutdown onto the running event loop.
                await asyncio.sleep(0)

    def _remove_jobs(self, task_id: int) -> None:
        if self._scheduler is None:
            return
        prefix = f"task:{task_id}:"
        for job in self._scheduler.get_jobs():
            if job.id.startswith(prefix):
                try:
                    self._scheduler.remove_job(job.id)
                except JobLookupError:
                    logger.debug("Reminder %s already fired", job.id)

    async def remove_task_jobs(self, task_id: int) -> None:
        async with self._lock:
            self._remove_jobs(task_id)
            await self._load_anchors()
            if self._anchors.pop(str(task_id), None) is not None:
                await self._save_anchors()

    async def schedule_task_jobs(self, task: TaskItem) -> None:
        async with self._lock:
            await self._load_anchors()
            await self._schedule(task)

    async def reschedule_task_jobs(self, task: TaskItem) -> None:
        await self.schedule_task_jobs(task)

    async def _ensure_anchor(self, task: TaskItem) -> dict[str, str]:
        current = self._aware(task.start_time).isoformat()
        key, rule = str(task.id), task.rrule_str.strip()
        anchor = self._anchors.get(key)
        if not isinstance(anchor, dict) or anchor.get("rule") != rule or anchor.get("current") != current:
            anchor = {"start": current, "current": current, "rule": rule}
            self._anchors[key] = anchor
            await self._save_anchors()
        return anchor

    def _next_occurrence(self, anchor: dict[str, str], after: datetime) -> datetime | None:
        start = self._aware(datetime.fromisoformat(anchor["start"]))
        rule = rrulestr(anchor["rule"], dtstart=start)
        result = rule.after(after, inc=False)
        return self._aware(result) if result else None

    async def _advance(self, task: TaskItem, after: datetime) -> TaskItem | None:
        anchor = await self._ensure_anchor(task)
        next_time = self._next_occurrence(anchor, after)
        if next_time is None:
            self._remove_jobs(task.id)
            self._anchors.pop(str(task.id), None)
            await self._save_anchors()
            # Exhausting a recurring series does not imply the user completed it.
            # Keep its final occurrence pending, but no longer recurring.
            return await repo.update_task(task.id, {"rrule_str": None})
        # The shared repository stores wall-clock datetimes without timezone.
        # Normalize to the configured timezone before removing tzinfo; otherwise
        # its host-local conversion would shift dates in a UTC Docker container.
        updates: dict[str, Any] = {"start_time": next_time.replace(tzinfo=None)}
        if task.end_time is not None:
            duration = self._aware(task.end_time) - self._aware(task.start_time)
            updates["end_time"] = (next_time + duration).replace(tzinfo=None)
        updated = await repo.update_task(task.id, updates)
        if updated is not None:
            anchor["current"] = next_time.isoformat()
            await self._save_anchors()
        return updated

    async def _schedule(self, task: TaskItem) -> None:
        if task.id is None:
            raise ValueError("A task must be persisted before scheduling")
        self._remove_jobs(task.id)
        if _value(task.status) != "pending":
            return
        is_schedule = _value(task.task_type) == "schedule"
        target = task.start_time if is_schedule else task.deadline
        if target is None:
            logger.warning("Task %s has no reminder timestamp", task.id)
            return
        target, now = self._aware(target), self._now()
        if is_schedule and task.rrule_str:
            await self._ensure_anchor(task)
            if target < now - timedelta(seconds=60):
                task = await self._advance(task, now)
                if task is None or not task.rrule_str:
                    return
                target = self._aware(task.start_time)
        if self._scheduler is None:
            # The persisted task is picked up by start(); never create an orphan scheduler.
            return
        configured = task.remind_advance_minutes
        if not configured:
            configured = config_manager.get(
                "default_schedule_remind_minutes" if is_schedule else "default_ddl_advances",
                [10, 0] if is_schedule else [1440, 720],
            )
        advances = sorted({int(value) for value in configured if isinstance(value, int) and not isinstance(value, bool) and value >= 0}, reverse=True)
        occurrence = target.isoformat()
        scheduled_any = False
        for advance in advances:
            # A single due callback sends the at-time notice, then advances recurrence.
            if is_schedule and advance == 0:
                continue
            run_date = target - timedelta(minutes=advance)
            if run_date < now:
                continue
            self._scheduler.add_job(self._remind, "date", run_date=run_date,
                args=[task.id, occurrence, advance], id=f"task:{task.id}:remind:{advance}",
                replace_existing=True, misfire_grace_time=300)
            scheduled_any = True
        if not is_schedule and not scheduled_any and target > now:
            # 若 DDL 距离截止已不足默认预警窗口（如不足12h），自动补一个截止前60分钟或到点提醒
            fallback_adv = 60 if (target - now) > timedelta(minutes=60) else 0
            run_date = max(now + timedelta(seconds=1), target - timedelta(minutes=fallback_adv))
            self._scheduler.add_job(self._remind, "date", run_date=run_date,
                args=[task.id, occurrence, fallback_adv], id=f"task:{task.id}:remind:{fallback_adv}",
                replace_existing=True, misfire_grace_time=300)
        if is_schedule and (task.rrule_str or 0 in advances) and target >= now - timedelta(seconds=60):
            self._scheduler.add_job(self._due, "date", run_date=max(target, now),
                args=[task.id, occurrence, 0 in advances], id=f"task:{task.id}:due",
                replace_existing=True, misfire_grace_time=None)

    async def _send_reminder(self, task: TaskItem, advance: int) -> None:
        from backend.channels.manager import channel_manager
        is_schedule = _value(task.task_type) == "schedule"
        target = self._aware(task.start_time if is_schedule else task.deadline)
        label = "日程提醒" if is_schedule else "DDL 截止预警"
        if advance == 0:
            remaining = "已到时间"
        elif advance % 1440 == 0:
            remaining = f"还有 {advance // 1440} 天"
        elif advance % 60 == 0:
            remaining = f"还有 {advance // 60} 小时"
        else:
            remaining = f"还有 {advance} 分钟"
        text = (f"⏰ {label} [#{task.id}]：{task.title}\n{target:%Y-%m-%d %H:%M}（{remaining}）\n"
                "💡 引用本条消息回复“推迟半小时”“已完成”，或发送“#编号 推迟半小时”即可调整。")
        # 收集投递目标：原渠道 + 已记录的其他活跃 IM 渠道（实现跨端同步提醒与双端可引用）
        targets: dict[str, str] = {}
        try:
            origin_ch = ChannelType(task.source_channel)
            if task.sender_id and task.sender_id not in ("web-simulator", "default"):
                targets[origin_ch.value] = task.sender_id
        except ValueError:
            pass
        all_cfgs = await repo.get_all_configs()
        known_map = all_cfgs.get("_scheduler_channel_recipients")
        if isinstance(known_map, dict):
            for ch_val, rec_id in known_map.items():
                if rec_id and rec_id not in ("web-simulator", "default"):
                    targets.setdefault(str(ch_val), str(rec_id))
        if not targets:
            logger.info("任务 #%s 无可用 IM 投递目标，跳过 IM 主动推送", task.id)
            return
        for ch_val, rec_id in targets.items():
            try:
                ch_enum = ChannelType(ch_val)
                sent = await channel_manager.send_message(ch_enum, rec_id, text)
                if sent:
                    await repo.bind_message_to_task(ch_enum.value, str(sent), task.id)
            except Exception:
                logger.debug("提醒推送至 %s 失败（已忽略）", ch_val)

    async def _matching_task(self, task_id: int, occurrence: str) -> TaskItem | None:
        task = await repo.get_task(task_id)
        if task is None or _value(task.status) != "pending":
            return None
        target = task.start_time if _value(task.task_type) == "schedule" else task.deadline
        if target is None or self._aware(target).isoformat() != occurrence:
            return None
        return task

    async def _remind(self, task_id: int, occurrence: str, advance: int) -> None:
        try:
            task = await self._matching_task(task_id, occurrence)
            if task is not None:
                await self._send_reminder(task, advance)
        except Exception:
            logger.exception("Reminder delivery failed for task %s", task_id)

    async def _due(self, task_id: int, occurrence: str, send_notice: bool) -> None:
        if send_notice:
            await self._remind(task_id, occurrence, 0)
        try:
            async with self._lock:
                task = await self._matching_task(task_id, occurrence)
                if task is not None and task.rrule_str:
                    updated = await self._advance(task, max(self._aware(task.start_time), self._now()))
                    if updated is not None and updated.rrule_str:
                        await self._schedule(updated)
        except Exception:
            logger.exception("Recurring task advancement failed for task %s", task_id)

    async def _on_config_changed(self, changes: dict[str, Any]) -> None:
        if not {"timezone", "default_schedule_remind_minutes", "default_ddl_advances"}.intersection(changes):
            return
        async with self._lock:
            if self._scheduler is None:
                return
            for task in await repo.list_tasks(status="pending"):
                try:
                    await self._schedule(task)
                except Exception:
                    logger.exception("Could not reload reminders for task %s", task.id)


scheduler_manager = SchedulerManager()
