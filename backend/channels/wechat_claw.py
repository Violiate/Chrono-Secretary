"""微信 iLink 长轮询适配器；平台未返回消息 ID 时绝不伪造引用绑定。"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import hashlib
import logging
import secrets
from typing import Any, Optional
import uuid

import httpx

from backend.core.schemas import BaseChannelAdapter, ChannelType, UnifiedIncomingMessage

logger = logging.getLogger(__name__)


class WeChatClawAdapter(BaseChannelAdapter):
    def __init__(self, config_manager, orchestrator=None):
        self.config_manager = config_manager
        self.orchestrator = orchestrator
        self.is_running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._token = ""
        self._base_url = "https://ilinkai.weixin.qq.com"
        self._cursor = ""
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()

    @staticmethod
    def _check_response(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("iLink 返回了非对象响应")
        for key in ("ret", "errcode"):
            if data.get(key) not in (None, 0, "0"):
                # 不回显可能含凭证、联系人信息的整个响应。
                raise RuntimeError(f"iLink API 请求失败 ({key}={data[key]})")
        return data

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(),
        }

    async def get_login_qrcode(self) -> dict[str, Any]:
        base = str(self.config_manager.get("wechat_claw_base_url", self._base_url)).rstrip("/")
        async with httpx.AsyncClient(timeout=40) as client:
            data = self._check_response(await client.get(
                f"{base}/ilink/bot/get_bot_qrcode", params={"bot_type": "3"}))
        identifier = data.get("qrcode_id") or data.get("qrcode")
        url = data.get("qrcode_url") or data.get("qrcode_img_content")
        if not identifier or not url:
            raise ValueError("iLink 二维码响应缺少标识或二维码链接")
        return {"qrcode_id": str(identifier), "qrcode_url": str(url)}

    async def poll_qrcode_status(self, qrcode_id: str) -> dict[str, Any]:
        if not qrcode_id:
            raise ValueError("qrcode_id 不能为空")
        base = str(self.config_manager.get("wechat_claw_base_url", self._base_url)).rstrip("/")
        async with httpx.AsyncClient(timeout=40) as client:
            data = self._check_response(await client.get(
                f"{base}/ilink/bot/get_qrcode_status", params={"qrcode": qrcode_id}))
        raw_status = str(data.get("status", "wait")).lower()
        status = {"wait": "waiting", "waiting": "waiting", "scaned": "scanned",
                  "scanned": "scanned", "confirmed": "confirmed", "expired": "expired"}.get(raw_status)
        if status is None:
            raise ValueError("iLink 返回了未知二维码状态")
        token = data.get("bot_token") if status == "confirmed" else None
        if status == "confirmed":
            if not token:
                raise ValueError("微信已确认扫码，但未返回 bot_token")
            await self.config_manager.update_configs({
                "wechat_claw_token": str(token), "wechat_claw_enabled": True})
        return {"status": status, "bot_token": str(token) if token else None}

    async def start(self) -> None:
        if self.is_running:
            return
        self._token = str(self.config_manager.get("wechat_claw_token", "") or "")
        if not self._token:
            raise ValueError("启用微信前需要扫码取得 wechat_claw_token")
        self._base_url = str(self.config_manager.get(
            "wechat_claw_base_url", "https://ilinkai.weixin.qq.com")).rstrip("/")
        from backend.core.repository import repo
        persisted = (await repo.get_all_configs()).get("wechat_claw_poll_state", {})
        fingerprint = hashlib.sha256(self._token.encode()).hexdigest()
        self._cursor = str(persisted.get("cursor", "")) if (
            isinstance(persisted, dict) and persisted.get("account") == fingerprint) else ""
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))
        self.is_running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="wechat-ilink-poll")

    async def stop(self) -> None:
        self.is_running = False
        task, self._poll_task = self._poll_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.debug("微信轮询已停止")
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    @staticmethod
    def _reply_id(value: Any, depth: int = 0) -> Optional[str]:
        """兼容消息顶层、text_item、ref_msg/refer_msg 与 item_list 内引用。"""
        if depth > 8:
            return None
        if isinstance(value, dict):
            for key in ("quote_msg_id", "reply_to_msg_id", "repliedMsgId"):
                if value.get(key) is not None:
                    return str(value[key])
            for key in ("refer_msg", "ref_msg", "quote_msg", "reply_to_message"):
                reference = value.get(key)
                if isinstance(reference, dict):
                    identifier = reference.get("msg_id", reference.get("message_id"))
                    if identifier is not None:
                        return str(identifier)
            for child in value.values():
                result = WeChatClawAdapter._reply_id(child, depth + 1)
                if result:
                    return result
        elif isinstance(value, list):
            for child in value:
                result = WeChatClawAdapter._reply_id(child, depth + 1)
                if result:
                    return result
        return None

    @staticmethod
    def _text(payload: dict[str, Any]) -> str:
        text = payload.get("text")
        if isinstance(text, str):
            return text.strip()
        if isinstance(text, dict) and isinstance(text.get("content"), str):
            return text["content"].strip()
        parts = []
        for item in payload.get("item_list", []) or []:
            if isinstance(item, dict):
                text_item = item.get("text_item", {})
                if isinstance(text_item, dict) and isinstance(text_item.get("text"), str):
                    parts.append(text_item["text"])
        return "\n".join(parts).strip()

    async def _handle_payload(self, payload: dict[str, Any]) -> None:
        # 只接受用户完整消息，不消费机器人回显或流式中间片段。
        if payload.get("message_type") not in (None, 1, "1"):
            return
        if payload.get("message_state") not in (None, 2, "2"):
            return
        sender = payload.get("from_user_id")
        identifier = payload.get("msg_id", payload.get("message_id"))
        text = self._text(payload)
        if not sender or identifier is None or not text:
            return
        key = (str(sender), str(identifier))
        if key in self._seen:
            return
        msg = UnifiedIncomingMessage(
            channel=ChannelType.WECHAT_CLAW, sender_id=str(sender), message_id=str(identifier),
            text=text, reply_to_msg_id=self._reply_id(payload),
            context_token=payload.get("context_token"), raw_payload=payload)
        handler = self.orchestrator
        if handler is None:
            from backend.core.orchestrator import orchestrator
            handler = orchestrator
        await handler.handle_message(msg)
        self._seen[key] = None
        while len(self._seen) > 4096:
            self._seen.popitem(last=False)

    async def _poll_loop(self) -> None:
        from backend.core.repository import repo
        delay = 1.0
        try:
            while self.is_running:
                try:
                    client = self._client
                    if client is None:
                        return
                    data = self._check_response(await client.post(
                        f"{self._base_url}/ilink/bot/getupdates", headers=self._headers(), json={
                            "get_updates_buf": self._cursor,
                            "base_info": {"channel_version": "1.0.0"}}))
                    messages = data.get("msgs", data.get("messages", [])) or []
                    if not isinstance(messages, list):
                        raise ValueError("iLink 消息列表格式无效")
                    for payload in messages:
                        if isinstance(payload, dict):
                            await self._handle_payload(payload)
                    # 全批处理完成才推进游标，避免业务异常导致消息被跳过。
                    cursor = data.get("get_updates_buf", data.get("sync_buf", self._cursor))
                    if cursor is not None and str(cursor) != self._cursor:
                        new_cursor = str(cursor)
                        await repo.update_configs({"wechat_claw_poll_state": {
                            "account": hashlib.sha256(self._token.encode()).hexdigest(),
                            "cursor": new_cursor}})
                        self._cursor = new_cursor
                    delay = 1.0
                    if not messages:
                        await asyncio.sleep(0.25)
                except httpx.ReadTimeout:
                    # 正常长轮询超时立即进入下一轮。
                    continue
                except Exception as exc:
                    logger.warning("微信轮询失败 (%s)，%.1f 秒后重试", type(exc).__name__, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        finally:
            self.is_running = False

    async def send_text(self, recipient_id: str, text: str,
                        context_token: Optional[str] = None) -> Optional[str]:
        if not text or not recipient_id:
            raise ValueError("收件人和消息文本不能为空")
        if not self.is_running or self._client is None:
            return None
        from backend.core.repository import repo
        if not context_token:
            context_token = await repo.get_context_token("wechat_claw", recipient_id)
        if not context_token:
            logger.warning("缺少微信 context_token，无法发送主动提醒")
            return None
        payload = {"msg": {
            "from_user_id": "", "to_user_id": recipient_id,
            "client_id": uuid.uuid4().hex, "message_type": 2, "message_state": 2,
            "context_token": context_token,
            "item_list": [{"type": 1, "text_item": {"text": text}}]},
            "base_info": {"channel_version": "1.0.0"}}
        data = self._check_response(await self._client.post(
            f"{self._base_url}/ilink/bot/sendmessage", headers=self._headers(), json=payload))
        identifier = data.get("msg_id", data.get("message_id"))
        for key in ("msg", "data", "result"):
            nested = data.get(key)
            if identifier is None and isinstance(nested, dict):
                identifier = nested.get("msg_id", nested.get("message_id"))
        if identifier is None:
            logger.warning("微信发送成功但平台未返回消息 ID；此条消息无法建立引用绑定")
            return None
        return str(identifier)
