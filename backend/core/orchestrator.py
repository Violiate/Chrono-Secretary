"""自然语言业务入口：持久化任务、同步日历、安排提醒并绑定回复。"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta
import logging
from typing import Optional
from zoneinfo import ZoneInfo

from backend.core.config_manager import config_manager
from backend.core.nlu_engine import nlu_engine
from backend.core.repository import repo
from backend.core.schemas import IntentType, UnifiedIncomingMessage

logger = logging.getLogger(__name__)
HINT = "💡 你可以随时【引用】本条消息回复‘推迟半小时’、‘改到明天3点’或‘已完成’来动态调整。"


class Orchestrator:
    def __init__(self) -> None:
        # 消息处理串行化避免重复投递同时创建及引用更新互相覆盖。
        self._lock = asyncio.Lock()
        self._seen: OrderedDict[tuple[str, str, str], str] = OrderedDict()

    def _local(self, value: datetime) -> datetime:
        tz = ZoneInfo(config_manager.get("timezone", "Asia/Shanghai"))
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)

    async def handle_message(self, msg: UnifiedIncomingMessage) -> Optional[str]:
        key = (msg.channel.value, msg.sender_id, msg.message_id)
        async with self._lock:
            if msg.context_token:
                await repo.save_context_token(msg.channel.value, msg.sender_id, msg.context_token)
            if msg.message_id and key in self._seen:
                return self._seen[key]
            try:
                text, task = await self._process(msg)
            except ValueError as exc:
                text, task = f"无法保存：{exc}。请补充或修正任务信息。", None
            except Exception:
                logger.exception("消息处理失败 channel=%s message=%s", msg.channel.value, msg.message_id)
                text, task = "处理时遇到错误，请先查询待办列表确认是否已保存，再重试。", None
            # 即使渠道离线，仍返回文案供网页模拟接口展示。
            from backend.channels.manager import channel_manager
            try:
                sent_id = await channel_manager.send_message(
                    msg.channel, msg.sender_id, text, context_token=msg.context_token)
                if sent_id and task is not None:
                    await repo.bind_message_to_task(msg.channel.value, str(sent_id), task.id)
            except Exception:
                logger.exception("回复发送或引用绑定失败 message=%s", msg.message_id)
            if msg.message_id:
                self._seen[key] = text
                if len(self._seen) > 2048:
                    self._seen.popitem(last=False)
            return text

    async def _process(self, msg):
        from backend.calendars.manager import calendar_manager
        from backend.core.scheduler import scheduler_manager

        task = None
        if msg.reply_to_msg_id:
            task = await repo.get_task_by_msg_id(msg.channel.value, msg.reply_to_msg_id)
            if task is None:
                return "未找到这条引用对应的任务，请引用机器人发出的任务确认或提醒消息。", None
            # 平台消息ID可能可猜测，必须同时验证任务归属。
            if task.sender_id != msg.sender_id or task.source_channel != msg.channel.value:
                return "这条引用不属于你的当前渠道任务。", None
        result = await nlu_engine.parse(msg.text, has_reply=task is not None)
        if result.intent == IntentType.QUERY_PENDING:
            return await self._pending(msg), None
        if task is not None:
            if result.intent in (IntentType.COMPLETE_TASK, IntentType.CANCEL_TASK):
                status = "completed" if result.intent == IntentType.COMPLETE_TASK else "cancelled"
                updated = await repo.update_task(task.id, {"status": status})
                if updated is None:
                    return "任务已不存在。", None
                await scheduler_manager.remove_task_jobs(task.id)
                remote_ok = True
                if task.cal_event_uid:
                    remote_ok = await calendar_manager.sync_delete_event(task.cal_event_uid)
                label = "已完成" if status == "completed" else "已取消"
                suffix = "\n远端日历删除失败，请稍后重试。" if not remote_ok else ""
                return f"✅ {label}：{task.title}{suffix}", updated
            if result.intent != IntentType.MODIFY_TASK:
                return "请引用任务回复‘推迟半小时’、‘改到明天下午3点’、‘已完成’或‘取消’。", task
            if task.status != "pending":
                return "这项任务已经完成或取消，无法继续改期，请新建任务。", task
            updates = {}
            is_ddl = task.task_type == "memo_ddl"
            field = "deadline" if is_ddl else "start_time"
            old_time = getattr(task, field)
            if result.snooze_minutes is not None:
                if result.snooze_minutes <= 0 or old_time is None:
                    return "请提供有效的推迟时长或新的具体时间。", task
                updates[field] = self._local(old_time) + timedelta(minutes=result.snooze_minutes)
            elif result.deadline or result.start_time:
                updates[field] = self._local((result.deadline or result.start_time) if is_ddl
                                            else (result.start_time or result.deadline))
            if result.title:
                updates["title"] = result.title
            if result.description is not None:
                updates["description"] = result.description
            if result.rrule_str is not None:
                updates["rrule_str"] = result.rrule_str
            if result.remind_advance_minutes is not None:
                updates["remind_advance_minutes"] = result.remind_advance_minutes
            if not is_ddl:
                if result.end_time:
                    updates["end_time"] = self._local(result.end_time)
                elif field in updates and old_time and task.end_time:
                    updates["end_time"] = updates[field] + (self._local(task.end_time) - self._local(old_time))
            if not updates:
                return "请说明要改到的时间或提前提醒分钟数。", task
            self._validate(updates, task)
            self._storage_times(updates)
            task = await repo.update_task(task.id, updates)
            if task is None:
                return "任务已不存在。", None
            remote_ok = await calendar_manager.sync_update_event(task) if task.cal_event_uid else True
            await scheduler_manager.reschedule_task_jobs(task)
            suffix = "\n远端日历更新失败，本地改期已保存。" if not remote_ok else ""
            return f"📅 已更新：{task.title}\n时间：{self._local(getattr(task, field)):%Y-%m-%d %H:%M}{suffix}\n{HINT}", task
        if result.intent not in (IntentType.CREATE_SCHEDULE, IntentType.CREATE_MEMO_DDL):
            return "你可以说‘提醒我30分钟后拿快递’，或告诉我事项及截止时间。修改已有任务时请先引用对应消息。", None
        is_ddl = result.intent == IntentType.CREATE_MEMO_DDL
        when = result.deadline if is_ddl else result.start_time
        if when is None or not result.title or not result.title.strip():
            return "请补充事项名称和具体时间（备忘录请说明截止时间）。", None
        data = {
            "task_type": "memo_ddl" if is_ddl else "schedule", "title": result.title.strip(),
            "description": result.description, "start_time": result.start_time,
            "end_time": result.end_time, "deadline": result.deadline,
            "rrule_str": result.rrule_str, "status": "pending",
            "remind_advance_minutes": result.remind_advance_minutes or config_manager.get(
                "default_ddl_advances" if is_ddl else "default_schedule_remind_minutes"),
            "source_channel": msg.channel.value, "sender_id": msg.sender_id,
        }
        for field in ("start_time", "end_time", "deadline"):
            if data[field] is not None:
                data[field] = self._local(data[field])
        self._validate(data)
        self._storage_times(data)
        task = await repo.create_task(data)
        uid = await calendar_manager.sync_create_event(task)
        if uid:
            task = await repo.update_task(task.id, {"cal_event_uid": uid}) or task
        await scheduler_manager.schedule_task_jobs(task)
        label = "截止" if is_ddl else "时间"
        rrule_info = f"\n重复：{task.rrule_str}" if task.rrule_str else ""
        return f"✅ 已记录：{task.title}\n{label}：{self._local(when):%Y-%m-%d %H:%M}{rrule_info}\n{HINT}", task

    def _storage_times(self, data):
        # Repository 使用本地 naive datetime；显式按配置时区剥离，
        # 避免部署机器为 UTC 时由仓库隐式转换造成提醒偏移。
        for field in ("start_time", "end_time", "deadline"):
            if data.get(field) is not None:
                data[field] = self._local(data[field]).replace(tzinfo=None)

    def _validate(self, data, task=None):
        def value(key):
            return data.get(key, getattr(task, key, None))
        start, end = value("start_time"), value("end_time")
        if start and end and self._local(end) <= self._local(start):
            raise ValueError("结束时间必须晚于开始时间")
        advances = value("remind_advance_minutes")
        if advances is not None and any(type(v) is not int or v < 0 for v in advances):
            raise ValueError("提醒提前量必须为非负整数")
        if value("rrule_str"):
            from dateutil.rrule import rrulestr
            rrulestr(value("rrule_str"), dtstart=self._local(start or value("deadline")))

    async def _pending(self, msg) -> str:
        tasks = await repo.get_pending_tasks_sorted(msg.sender_id)
        # 仓库按sender过滤；再隔离不同平台同名/同数字账户。
        tasks = [t for t in tasks if (t.sender_id == msg.sender_id and t.source_channel == msg.channel.value)
                 or (t.sender_id == "default" and t.source_channel == "web")]
        if not tasks:
            return "🎉 当前没有未完成事项。"
        now = datetime.now(ZoneInfo(config_manager.get("timezone", "Asia/Shanghai")))
        groups = [[], [], []]
        tasks.sort(key=lambda t: self._local(t.deadline or t.start_time) if t.deadline or t.start_time
                   else datetime.max.replace(tzinfo=now.tzinfo))
        for task in tasks:
            when = task.deadline or task.start_time
            if when is None:
                groups[2].append(f"• {task.title}（尚未设置时间）")
                continue
            when = self._local(when)
            hours = (when - now).total_seconds() / 3600
            remaining = f"逾期 {-hours:.1f} 小时" if hours < 0 else (
                f"剩余 {hours:.1f} 小时" if hours < 72 else f"剩余 {hours / 24:.1f} 天")
            index = 0 if hours <= 24 else (1 if hours <= 72 else 2)
            groups[index].append(f"• {task.title} — {when:%m-%d %H:%M}（{remaining}）")
        headings = ("🔴 24小时内紧急事项（含逾期）", "🟡 3天内事项", "🟢 远期备忘与日程")
        return "📋 未完成事项\n\n" + "\n\n".join(
            heading + "\n" + "\n".join(items) for heading, items in zip(headings, groups) if items)


orchestrator = Orchestrator()
