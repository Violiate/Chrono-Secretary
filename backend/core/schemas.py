"""跨模块共享的数据契约；不依赖数据库或具体 SDK。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from enum import Enum
from typing import Any, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from backend.core.database import TaskItem


class ChannelType(str, Enum):
    WECHAT_CLAW = "wechat_claw"
    DINGTALK = "dingtalk"
    TELEGRAM = "telegram"


class TaskType(str, Enum):
    SCHEDULE = "schedule"
    MEMO_DDL = "memo_ddl"


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class IntentType(str, Enum):
    CREATE_SCHEDULE = "create_schedule"
    CREATE_MEMO_DDL = "create_memo_ddl"
    MODIFY_TASK = "modify_task"
    COMPLETE_TASK = "complete_task"
    CANCEL_TASK = "cancel_task"
    QUERY_PENDING = "query_pending"
    UNKNOWN = "unknown"


class UnifiedIncomingMessage(BaseModel):
    channel: ChannelType
    sender_id: str
    message_id: str
    text: str
    reply_to_msg_id: Optional[str] = None
    context_token: Optional[str] = None
    raw_payload: dict[str, Any] = Field(default_factory=dict)


class NLUResult(BaseModel):
    intent: IntentType = IntentType.UNKNOWN
    title: Optional[str] = None
    description: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    deadline: Optional[datetime] = None
    rrule_str: Optional[str] = None
    remind_advance_minutes: Optional[list[int]] = None
    snooze_minutes: Optional[int] = None


class BaseChannelAdapter(ABC):
    # 必须允许第三方适配器直接赋值，与 DeepSeek 冻结契约一致。
    is_running: bool = False

    @abstractmethod
    async def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_text(self, recipient_id: str, text: str,
                        context_token: Optional[str] = None) -> Optional[str]:
        raise NotImplementedError


class BaseCalendarProvider(ABC):
    @abstractmethod
    async def create_event(self, task: TaskItem) -> Optional[str]:
        raise NotImplementedError

    @abstractmethod
    async def update_event(self, task: TaskItem) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def delete_event(self, cal_event_uid: str) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        raise NotImplementedError
