"""
backend/core/repository.py
==========================
数据库异步 CRUD 仓库（DeepSeek 负责 / 阶段 D1）。

本文件是「跨模块冻结接口契约」里最硬的一环：GPT 侧的 ``config_manager`` /
``scheduler`` / ``orchestrator`` / ``wechat_claw`` 全部通过

    from backend.core.repository import repo

调用下面的 13 个方法。**方法名、参数名、返回值形态一律不许改**。
"""

import json
import logging
import secrets
import string
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, or_
from sqlmodel import select

from backend.core.database import (
    ChannelSession,
    MessageBinding,
    SystemConfig,
    TaskItem,
    as_naive_datetime,
    async_session_factory,
    create_db_and_tables,
)

logger = logging.getLogger(__name__)

# task_type / status / 时间字段等常量，供其它模块 import 复用
TASK_TYPE_SCHEDULE = "schedule"
TASK_TYPE_MEMO_DDL = "memo_ddl"
STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"

_DATETIME_FIELDS = ("start_time", "end_time", "deadline", "created_at", "updated_at")
# 允许被显式置为 None 的字段（其余字段传 None 时忽略，避免把 title 清空）
_NULLABLE_FIELDS = ("description", "start_time", "end_time", "deadline", "rrule_str", "cal_event_uid")


def _generate_feed_token(length: int = 16) -> str:
    """生成 16 位字母数字随机串，作为 ICS 订阅地址的保密 token。"""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _json_loads(raw: Any, fallback: Any = None) -> Any:
    if raw is None:
        return fallback
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return fallback


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


class Repository:
    """全局唯一的异步数据仓库。所有方法自带会话生命周期，上层无需管理 session。"""

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    async def init_db(self) -> None:
        """建表 + 首次启动时自动生成 ``ics_feed_token``。"""
        await create_db_and_tables()

        existing = await self.get_config("ics_feed_token")
        if not existing:
            token = _generate_feed_token(16)
            await self.update_configs({"ics_feed_token": token})
            logger.info("已生成新的 ICS 订阅 token: %s", token)
        else:
            logger.info("ICS 订阅 token 已存在，沿用旧值")

    # ------------------------------------------------------------------ #
    # 配置读写
    # ------------------------------------------------------------------ #
    async def get_all_configs(self) -> Dict[str, Any]:
        """读取全部配置，返回 ``{key: python_value}``（value 已反序列化）。"""
        async with async_session_factory() as session:
            result = await session.exec(select(SystemConfig))
            rows = result.all()
        return {row.key: _json_loads(row.value) for row in rows}

    async def get_config(self, key: str, default: Any = None) -> Any:
        """读取单个配置项（供 routes / 内部兜底使用）。"""
        async with async_session_factory() as session:
            row = await session.get(SystemConfig, key)
        if row is None:
            return default
        value = _json_loads(row.value, fallback=default)
        return default if value is None else value

    async def update_configs(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """批量 upsert 配置，返回更新后的全量配置。"""
        if not isinstance(updates, dict):
            raise TypeError("updates 必须是 dict")

        now = datetime.now()
        async with async_session_factory() as session:
            for key, value in updates.items():
                if not isinstance(key, str) or not key:
                    logger.warning("忽略非法配置键: %r", key)
                    continue
                row = await session.get(SystemConfig, key)
                payload = _json_dumps(value)
                if row is None:
                    session.add(SystemConfig(key=key, value=payload, updated_at=now))
                else:
                    row.value = payload
                    row.updated_at = now
                    session.add(row)
            await session.commit()

        return await self.get_all_configs()

    # ------------------------------------------------------------------ #
    # 任务 CRUD
    # ------------------------------------------------------------------ #
    async def create_task(self, task_data: Dict[str, Any]) -> TaskItem:
        """新建任务。

        ``task_data`` 可以带 ``remind_advance_minutes``（list[int]）或
        ``remind_advance_minutes_json``；未知键会被静默丢弃，时间字符串会被解析。
        """
        if isinstance(task_data, TaskItem):
            task_data = task_data.model_dump(exclude={"id"})

        data: Dict[str, Any] = dict(task_data or {})
        remind = data.pop("remind_advance_minutes", None)
        data.pop("id", None)
        data.pop("created_at", None)
        data.pop("updated_at", None)

        allowed = set(TaskItem.model_fields.keys())
        cleaned: Dict[str, Any] = {}
        for field, value in data.items():
            if field not in allowed:
                continue
            if field in _DATETIME_FIELDS:
                if field == "remind_advance_minutes_json":
                    continue
                parsed = as_naive_datetime(value)
                if parsed is not None:
                    cleaned[field] = parsed
                continue
            if field == "remind_advance_minutes_json":
                continue
            if value is None and field not in _NULLABLE_FIELDS:
                continue
            cleaned[field] = value

        now = datetime.now()
        task = TaskItem(**cleaned)
        task.task_type = task.task_type or TASK_TYPE_SCHEDULE
        task.status = task.status or STATUS_PENDING
        task.source_channel = task.source_channel or "web"
        task.sender_id = task.sender_id or "default"
        task.title = (task.title or "").strip() or "未命名任务"
        task.created_at = now
        task.updated_at = now

        if remind is not None:
            task.remind_advance_minutes = remind
        elif data.get("remind_advance_minutes_json"):
            task.remind_advance_minutes_json = str(data["remind_advance_minutes_json"])

        async with async_session_factory() as session:
            session.add(task)
            await session.commit()
            await session.refresh(task)

        logger.info("任务已创建 #%s [%s] %s", task.id, task.task_type, task.title)
        return task

    async def get_task(self, task_id: int) -> Optional[TaskItem]:
        """按 ID 取任务，不存在返回 ``None``。"""
        if task_id is None:
            return None
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return None
        async with async_session_factory() as session:
            return await session.get(TaskItem, task_id)

    async def update_task(self, task_id: int, updates: Dict[str, Any]) -> Optional[TaskItem]:
        """局部更新任务字段；任务不存在返回 ``None``。"""
        if isinstance(updates, TaskItem):
            updates = updates.model_dump(exclude={"id", "created_at"})

        data: Dict[str, Any] = dict(updates or {})
        remind = data.pop("remind_advance_minutes", None) if "remind_advance_minutes" in data else None
        raw_json = data.pop("remind_advance_minutes_json", None)

        async with async_session_factory() as session:
            task = await session.get(TaskItem, int(task_id))
            if task is None:
                return None

            allowed = set(TaskItem.model_fields.keys())
            for field, value in data.items():
                if field not in allowed or field in ("id", "created_at"):
                    continue
                if field in _DATETIME_FIELDS:
                    parsed = as_naive_datetime(value)
                    if parsed is None and value not in (None, ""):
                        continue
                    task.__setattr__(field, parsed)
                    continue
                if value is None and field not in _NULLABLE_FIELDS:
                    continue
                task.__setattr__(field, value)

            if remind is not None:
                task.remind_advance_minutes = remind
            elif raw_json is not None:
                task.remind_advance_minutes_json = str(raw_json)

            task.updated_at = datetime.now()
            session.add(task)
            await session.commit()
            await session.refresh(task)

        logger.info("任务已更新 #%s: %s", task_id, list(data.keys()) or ["remind_advance_minutes"])
        return task

    async def delete_task(self, task_id: int) -> bool:
        """删除任务并顺手清理它的消息绑定，返回是否真的删掉了。"""
        try:
            task_id = int(task_id)
        except (TypeError, ValueError):
            return False

        async with async_session_factory() as session:
            task = await session.get(TaskItem, task_id)
            if task is None:
                return False
            await session.delete(task)
            await session.execute(delete(MessageBinding).where(MessageBinding.task_id == task_id))
            await session.commit()

        logger.info("任务已删除 #%s", task_id)
        return True

    async def list_tasks(
        self,
        status: Optional[str] = None,
        task_type: Optional[str] = None,
    ) -> List[TaskItem]:
        """按状态 / 类型筛选任务，按有效时间升序（无时间的排最后）。"""
        statement = select(TaskItem)
        if status:
            statement = statement.where(TaskItem.status == status)
        if task_type:
            statement = statement.where(TaskItem.task_type == task_type)
        statement = statement.order_by(
            TaskItem.deadline.is_(None),
            TaskItem.start_time.is_(None),
            TaskItem.deadline.asc(),
            TaskItem.start_time.asc(),
            TaskItem.id.asc(),
        )
        async with async_session_factory() as session:
            result = await session.exec(statement)
            return list(result.all())

    async def get_pending_tasks_sorted(self, sender_id: Optional[str] = None) -> List[TaskItem]:
        """所有未完成任务，按「有效时间」升序。

        - 有效时间 = ``deadline`` 优先，其次 ``start_time``；
        - ``sender_id`` 传 ``None`` / ``"all"`` 表示不过滤；
        - 过滤时同时包含 ``sender_id == "default"`` 的网页端任务（用户在哪都能看到自己的网页待办）。
        """
        statement = select(TaskItem).where(TaskItem.status == STATUS_PENDING)

        if sender_id and str(sender_id).lower() not in ("all", "*", "any"):
            statement = statement.where(
                or_(TaskItem.sender_id == str(sender_id), TaskItem.sender_id == "default")
            )

        async with async_session_factory() as session:
            result = await session.exec(statement)
            tasks = list(result.all())

        far_future = datetime(9999, 12, 31, 23, 59, 59)

        def sort_key(task: TaskItem):
            effective = as_naive_datetime(task.deadline or task.start_time)
            return (effective is None, effective or far_future, task.id or 0)

        tasks.sort(key=sort_key)
        return tasks

    # ------------------------------------------------------------------ #
    # 消息绑定（方法 A）
    # ------------------------------------------------------------------ #
    async def bind_message_to_task(self, channel: str, platform_msg_id: str, task_id: int) -> None:
        """把平台消息 ID 绑定到任务；同一 (channel, msg_id) 重复绑定时覆盖 task_id。"""
        if not channel or not platform_msg_id or task_id is None:
            logger.warning(
                "bind_message_to_task 参数不完整，跳过: channel=%r msg_id=%r task_id=%r",
                channel,
                platform_msg_id,
                task_id,
            )
            return

        channel = str(channel)
        platform_msg_id = str(platform_msg_id)

        async with async_session_factory() as session:
            result = await session.exec(
                select(MessageBinding).where(
                    MessageBinding.channel == channel,
                    MessageBinding.platform_msg_id == platform_msg_id,
                )
            )
            binding = result.first()
            if binding is None:
                session.add(
                    MessageBinding(
                        channel=channel,
                        platform_msg_id=platform_msg_id,
                        task_id=int(task_id),
                        created_at=datetime.now(),
                    )
                )
            else:
                binding.task_id = int(task_id)
                session.add(binding)
            await session.commit()

    async def get_task_by_msg_id(self, channel: str, platform_msg_id: str) -> Optional[TaskItem]:
        """通过「被引用消息 ID」反查任务。取最新一条绑定。"""
        if not channel or not platform_msg_id:
            return None

        async with async_session_factory() as session:
            result = await session.exec(
                select(MessageBinding)
                .where(
                    MessageBinding.channel == str(channel),
                    MessageBinding.platform_msg_id == str(platform_msg_id),
                )
                .order_by(MessageBinding.id.desc())
                .limit(1)
            )
            binding = result.first()
            if binding is None:
                return None
            return await session.get(TaskItem, binding.task_id)

    async def remove_message_bindings(self, task_id: int) -> None:
        """清理某个任务的全部消息绑定（删除任务时调用）。"""
        async with async_session_factory() as session:
            await session.execute(delete(MessageBinding).where(MessageBinding.task_id == int(task_id)))
            await session.commit()

    # ------------------------------------------------------------------ #
    # 渠道会话上下文
    # ------------------------------------------------------------------ #
    async def save_context_token(self, channel: str, sender_id: str, context_token: str) -> None:
        """保存（upsert）某个用户在当前渠道的最新 context_token。"""
        if not channel or not sender_id:
            return

        channel = str(channel)
        sender_id = str(sender_id)
        context_token = "" if context_token is None else str(context_token)

        async with async_session_factory() as session:
            result = await session.exec(
                select(ChannelSession).where(
                    ChannelSession.channel == channel,
                    ChannelSession.sender_id == sender_id,
                )
            )
            row = result.first()
            if row is None:
                session.add(
                    ChannelSession(
                        channel=channel,
                        sender_id=sender_id,
                        context_token=context_token,
                        updated_at=datetime.now(),
                    )
                )
            else:
                row.context_token = context_token
                row.updated_at = datetime.now()
                session.add(row)
            await session.commit()

    async def get_context_token(self, channel: str, sender_id: str) -> Optional[str]:
        """取某用户最近一次会话上下文 Token（微信主动推送必备）。"""
        if not channel or not sender_id:
            return None

        async with async_session_factory() as session:
            result = await session.exec(
                select(ChannelSession)
                .where(
                    ChannelSession.channel == str(channel),
                    ChannelSession.sender_id == str(sender_id),
                )
                .order_by(ChannelSession.updated_at.desc())
                .limit(1)
            )
            row = result.first()
        if row is None or not row.context_token:
            return None
        return row.context_token


# 全局单例：GPT 侧统一 `from backend.core.repository import repo`
repo = Repository()

__all__ = [
    "Repository",
    "repo",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_PENDING",
    "TASK_TYPE_MEMO_DDL",
    "TASK_TYPE_SCHEDULE",
]
