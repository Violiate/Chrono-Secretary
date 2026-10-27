"""
backend/core/database.py
========================
SQLite + SQLModel 异步数据层（DeepSeek 负责 / 阶段 D1）。

职责：
1. 建立 ``sqlite+aiosqlite`` 异步引擎，数据库文件固定落盘在 ``<项目根>/data/chrono.db``；
2. 定义 4 张核心表：``SystemConfig`` / ``TaskItem`` / ``MessageBinding`` / ``ChannelSession``；
3. 提供 ``TaskItem`` <-> ``dict`` 的序列化工具，供 ``repository.py`` / ``api/routes.py`` /
   ``calendars/ics_generator.py`` 复用，避免各处重复写样板代码。

约定（全项目统一）：
- 所有时间都是「本地 naive datetime」（``datetime.now()``），不做 UTC 转换。
  用户嘴里的「明天下午 3 点」永远是本机时区的时间，转换成 UTC 只会制造误差。
- ``TaskItem.remind_advance_minutes`` 在数据库里以 JSON 字符串存储（列名
  ``remind_advance_minutes_json``），但对上层暴露为 ``list[int]``，
  通过 property 自动序列化 / 反序列化（任务书硬性要求）。
"""

from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional

from sqlalchemy import Column, DateTime
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import Field, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import json
import logging

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# 引擎与连接
# --------------------------------------------------------------------------- #

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
DATA_DIR: Path = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE: Path = DATA_DIR / "chrono.db"

# 用 URL.create 而不是手拼字符串：Windows 中文路径 / 空格不会被 URL 解析破坏。
DATABASE_URL = URL.create(drivername="sqlite+aiosqlite", database=str(DB_FILE))

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    # aiosqlite 在线程池里跑，必须允许跨线程；
    # timeout=30 让并发写时等待锁而不是立刻抛 "database is locked"。
    connect_args={"check_same_thread": False, "timeout": 30},
)

# expire_on_commit=False：commit 之后仍然能直接读对象属性，
# 否则 async session 关闭后再访问 lazy 属性会触发 IO 异常。
async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def create_db_and_tables() -> None:
    """创建全部表（幂等）。"""
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    logger.info("SQLite 表结构已就绪: %s", DB_FILE)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖注入用：``Depends(get_session)``。"""
    async with async_session_factory() as session:
        yield session


# --------------------------------------------------------------------------- #
# 通用时间工具
# --------------------------------------------------------------------------- #

_DATETIME_FALLBACK_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y/%m/%d",
)


def as_naive_datetime(value: Any) -> Optional[datetime]:
    """把前端 / LLM / 平台传来的各种时间形态统一成本地 naive ``datetime``。

    支持：``datetime``（aware 会先转本地再剥掉 tzinfo）、ISO 字符串（含 ``Z`` 结尾）、
    常见 ``YYYY-MM-DD HH:MM(:SS)`` 字符串、Unix 时间戳。无法解析返回 ``None``。
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone().replace(tzinfo=None, microsecond=0)
        return value.replace(microsecond=0)

    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None

    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # Python 3.11+ 的 fromisoformat 支持 "Z"，这里兼容更早的小版本差异
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
            for fmt in _DATETIME_FALLBACK_FORMATS:
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            if parsed is None:
                logger.warning("无法解析时间字符串: %r", value)
                return None
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return parsed

    return None


def datetime_to_iso(value: Optional[datetime]) -> Optional[str]:
    """``datetime`` -> ``"2026-11-05T23:59:00"``（前端 datetime-local 直接可用）。"""
    if not isinstance(value, datetime):
        return None
    return value.isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# 表 1：系统配置（键值对 + JSON 值）
# --------------------------------------------------------------------------- #

class SystemConfig(SQLModel, table=True):
    """运行时配置持久化表：一行一个配置项，``value`` 是 JSON 字符串。"""

    __tablename__ = "system_config"

    key: str = Field(primary_key=True, description="配置键，如 llm_model")
    value: str = Field(default="null", description="JSON 序列化后的配置值")
    updated_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime, nullable=False),
        description="最后更新时间",
    )


# --------------------------------------------------------------------------- #
# 表 2：任务（日程 / DDL 备忘录）
# --------------------------------------------------------------------------- #

class TaskItem(SQLModel, table=True):
    """统一任务模型：``schedule``（强时间点日程）与 ``memo_ddl``（截止期备忘录）。"""

    __tablename__ = "task_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    task_type: str = Field(default="schedule", index=True, description="schedule | memo_ddl")
    title: str = Field(default="", description="任务标题")
    description: Optional[str] = Field(default=None, description="补充说明")
    start_time: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime, nullable=True, index=True), description="开始时间"
    )
    end_time: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime, nullable=True), description="结束时间"
    )
    deadline: Optional[datetime] = Field(
        default=None, sa_column=Column(DateTime, nullable=True, index=True), description="DDL 截止时间"
    )
    rrule_str: Optional[str] = Field(default=None, description="RFC 5545 RRULE，如 FREQ=DAILY")
    remind_advance_minutes_json: str = Field(
        default="[]", description="提前提醒分钟数（JSON 数组字符串）"
    )
    status: str = Field(default="pending", index=True, description="pending | completed | cancelled")
    source_channel: str = Field(default="web", description="来源渠道")
    sender_id: str = Field(default="default", index=True, description="发送者 ID")
    cal_event_uid: Optional[str] = Field(default=None, description="远端日历事件 UID")
    created_at: datetime = Field(default_factory=datetime.now, sa_column=Column(DateTime, nullable=False))
    updated_at: datetime = Field(default_factory=datetime.now, sa_column=Column(DateTime, nullable=False))

    # ---- remind_advance_minutes：数据库存 JSON，Python 层是 list[int] ---- #
    @property
    def remind_advance_minutes(self) -> List[int]:
        """反序列化提醒提前量；脏数据一律降级为空列表，绝不抛异常打断调度。"""
        raw = self.remind_advance_minutes_json or "[]"
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("remind_advance_minutes_json 非法 JSON，已忽略: %r", raw)
            return []

        if isinstance(data, bool):
            return []
        if isinstance(data, (int, float)):
            return [int(data)]
        if isinstance(data, str):
            # 容错：有人可能直接存了 "10,0"
            items: Iterator[str] = iter([p for p in data.replace("，", ",").split(",")])
        elif isinstance(data, (list, tuple, set)):
            items = iter(list(data))
        else:
            return []

        result: List[int] = []
        for item in items:
            try:
                minutes = int(float(str(item).strip()))
            except (TypeError, ValueError):
                continue
            if minutes < 0:
                continue
            if minutes not in result:
                result.append(minutes)
        return result

    @remind_advance_minutes.setter
    def remind_advance_minutes(self, value: Any) -> None:
        """序列化写入。支持 list / tuple / 逗号字符串 / 单个数字，非法项自动跳过。"""
        cleaned: List[int] = []
        raw_items: List[Any]

        if value is None:
            raw_items = []
        elif isinstance(value, bool):
            raw_items = []
        elif isinstance(value, (int, float)):
            raw_items = [value]
        elif isinstance(value, str):
            raw_items = [p for p in value.replace("，", ",").split(",") if p.strip()]
        elif isinstance(value, (list, tuple, set)):
            raw_items = list(value)
        else:
            raw_items = []

        for item in raw_items:
            try:
                minutes = int(float(str(item).strip()))
            except (TypeError, ValueError):
                continue
            if minutes < 0:
                continue
            if minutes not in cleaned:
                cleaned.append(minutes)

        self.remind_advance_minutes_json = json.dumps(cleaned, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 表 3：消息绑定（方法 A：引用回复检测的核心索引表）
# --------------------------------------------------------------------------- #

class MessageBinding(SQLModel, table=True):
    """``platform_msg_id`` -> ``task_id`` 映射，用户引用某条消息时据此反查任务。"""

    __tablename__ = "message_bindings"

    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(index=True, description="渠道标识，如 telegram / dingtalk")
    platform_msg_id: str = Field(index=True, description="平台侧消息唯一 ID")
    task_id: int = Field(index=True, foreign_key="task_items.id", description="关联任务 ID")
    created_at: datetime = Field(default_factory=datetime.now, sa_column=Column(DateTime, nullable=False))


# --------------------------------------------------------------------------- #
# 表 4：渠道会话（微信 iLink 主动推送必须带最新 context_token）
# --------------------------------------------------------------------------- #

class ChannelSession(SQLModel, table=True):
    """渠道会话上下文：保存每个用户最新的 ``context_token``（或钉钉 sessionWebhook）。"""

    __tablename__ = "channel_sessions"

    id: Optional[int] = Field(default=None, primary_key=True)
    channel: str = Field(index=True)
    sender_id: str = Field(index=True)
    context_token: str = Field(default="", description="平台会话上下文 Token")
    updated_at: datetime = Field(default_factory=datetime.now, sa_column=Column(DateTime, nullable=False))


# --------------------------------------------------------------------------- #
# 序列化工具
# --------------------------------------------------------------------------- #

def task_to_dict(task: TaskItem) -> Dict[str, Any]:
    """``TaskItem`` -> 前端 / API 友好的纯 dict（时间全部转 ISO 字符串）。"""
    return {
        "id": task.id,
        "task_type": task.task_type,
        "title": task.title,
        "description": task.description,
        "start_time": datetime_to_iso(task.start_time),
        "end_time": datetime_to_iso(task.end_time),
        "deadline": datetime_to_iso(task.deadline),
        "rrule_str": task.rrule_str,
        "remind_advance_minutes": task.remind_advance_minutes,
        "status": task.status,
        "source_channel": task.source_channel,
        "sender_id": task.sender_id,
        "cal_event_uid": task.cal_event_uid,
        "created_at": datetime_to_iso(task.created_at),
        "updated_at": datetime_to_iso(task.updated_at),
    }


def tasks_to_dicts(tasks: List[TaskItem]) -> List[Dict[str, Any]]:
    """批量序列化。"""
    return [task_to_dict(task) for task in tasks]


__all__ = [
    "DATABASE_URL",
    "DATA_DIR",
    "DB_FILE",
    "ChannelSession",
    "MessageBinding",
    "SystemConfig",
    "TaskItem",
    "as_naive_datetime",
    "async_session_factory",
    "create_db_and_tables",
    "datetime_to_iso",
    "engine",
    "get_session",
    "task_to_dict",
    "tasks_to_dicts",
]
