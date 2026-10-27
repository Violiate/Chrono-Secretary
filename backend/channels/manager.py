"""多渠道生命周期及配置热更新；外部适配器按冻结构造签名延迟加载。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from backend.core.config_manager import config_manager
from backend.core.schemas import BaseChannelAdapter, ChannelType
from backend.channels.wechat_claw import WeChatClawAdapter

logger = logging.getLogger(__name__)


class ChannelManager:
    def __init__(self, config_manager=config_manager, orchestrator=None):
        self.config_manager = config_manager
        self.orchestrator = orchestrator
        self.wechat_adapter = WeChatClawAdapter(config_manager, orchestrator)
        self._adapters: dict[ChannelType, BaseChannelAdapter] = {
            ChannelType.WECHAT_CLAW: self.wechat_adapter}
        self._locks = {channel: asyncio.Lock() for channel in ChannelType}
        self._snapshot = config_manager.get_all()
        self._closed = False
        config_manager.register_listener(self._on_config_changed)

    def _construct(self, channel: ChannelType) -> BaseChannelAdapter:
        handler = self.orchestrator
        if handler is None:
            from backend.core.orchestrator import orchestrator
            handler = orchestrator
        if channel == ChannelType.WECHAT_CLAW:
            return WeChatClawAdapter(config_manager=self.config_manager, orchestrator=handler)
        if channel == ChannelType.DINGTALK:
            from backend.channels.dingtalk_bot import DingTalkChannelAdapter
            return DingTalkChannelAdapter(config_manager=self.config_manager, orchestrator=handler)
        from backend.channels.telegram_bot import TelegramChannelAdapter
        return TelegramChannelAdapter(config_manager=self.config_manager, orchestrator=handler)

    async def start_all(self) -> None:
        self._closed = False
        self._snapshot = self.config_manager.get_all()
        for channel in ChannelType:
            await self.reload_channel(channel)

    async def stop_all(self) -> None:
        self._closed = True
        for channel in ChannelType:
            async with self._locks[channel]:
                adapter = self._adapters.get(channel)
                if adapter is not None:
                    try:
                        await adapter.stop()
                    except Exception as exc:
                        logger.error("停止 %s 失败 (%s)", channel.value, type(exc).__name__)

    async def reload_channel(self, channel: ChannelType) -> None:
        channel = ChannelType(channel)
        async with self._locks[channel]:
            old = self._adapters.get(channel)
            if old is not None:
                try:
                    await old.stop()
                except Exception as exc:
                    logger.error("停止旧 %s 适配器失败 (%s)", channel.value, type(exc).__name__)
                    return  # 旧连接未清理时不启动第二条消费连接。
            if self._closed or not self.config_manager.get(f"{channel.value}_enabled", False):
                return
            try:
                adapter = self._construct(channel)
                self._adapters[channel] = adapter
                if channel == ChannelType.WECHAT_CLAW:
                    self.wechat_adapter = adapter
                await adapter.start()
            except Exception as exc:
                logger.error("启动 %s 失败 (%s)", channel.value, type(exc).__name__)
                failed = self._adapters.get(channel)
                if failed is not None:
                    try:
                        await failed.stop()
                    except Exception as cleanup_exc:
                        logger.error("清理 %s 失败 (%s)", channel.value, type(cleanup_exc).__name__)

    async def _on_config_changed(self, updates: dict[str, Any]) -> None:
        # 回调兼容增量和全量参数；以当前实际配置比较，避免无关修改重启渠道。
        current = self.config_manager.get_all()
        previous, self._snapshot = self._snapshot, current
        changed = {key for key in previous.keys() | current.keys()
                   if previous.get(key) != current.get(key)}
        if self._closed:
            return
        for channel in ChannelType:
            if any(key.startswith(channel.value + "_") and key != "wechat_claw_poll_state"
                   for key in changed):
                await self.reload_channel(channel)

    def get_status(self) -> dict[str, bool]:
        return {channel.value: bool(self._adapters.get(channel) and
                                   self._adapters[channel].is_running) for channel in ChannelType}

    async def send_message(self, channel: ChannelType, recipient_id: str, text: str,
                           context_token: Optional[str] = None) -> Optional[str]:
        try:
            channel = ChannelType(channel)
        except ValueError:
            logger.warning("不支持的消息渠道")
            return None
        adapter = self._adapters.get(channel)
        if adapter is None or not adapter.is_running:
            return None
        try:
            return await adapter.send_text(recipient_id, text, context_token=context_token)
        except Exception as exc:
            logger.error("%s 消息发送失败 (%s)", channel.value, type(exc).__name__)
            return None


channel_manager = ChannelManager()
