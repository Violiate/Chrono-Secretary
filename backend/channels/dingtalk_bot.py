"""
backend/channels/dingtalk_bot.py
================================
钉钉机器人适配器（Stream 模式长连接）—— DeepSeek 负责 / 阶段 D2。

为什么走 Stream（WebSocket）而不是 HTTP 回调：
- 不需要公网 IP、不需要内网穿透、不需要在钉钉后台配置回调地址；
- 纯协程实现，``stop()`` 一个取消就能秒级热重启（前端换 Client ID / Secret 后即时生效）；
- 线程零占用，不会像某些 C 扩展 SDK 那样把事件循环卡死。

引用回复（方法 A）：
钉钉机器人回调里，被引用消息的 ID 可能出现在 ``repliedMsgId``（顶层）或
``text.repliedMsgId``（文本对象内），两处都做兼容提取。
"""

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from backend.core.repository import repo
from backend.core.schemas import BaseChannelAdapter, ChannelType, UnifiedIncomingMessage

logger = logging.getLogger(__name__)

# ---- 钉钉开放平台接口常量 ----
TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
GATEWAY_OPEN_URL = "https://api.dingtalk.com/v1.0/gateway/connections/open"
ROBOT_OTO_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
BOT_CALLBACK_TOPIC = "/v1.0/im/bot/messages/get"

MAX_TEXT_LENGTH = 4000  # 钉钉单条文本上限内，留足余量


def _with_ticket(endpoint: str, ticket: str) -> str:
    """把 ticket 拼到 gateway 返回的 endpoint 上。"""
    separator = "&" if "?" in endpoint else "?"
    return f"{endpoint}{separator}ticket={quote(ticket, safe='')}"


class DingTalkChannelAdapter(BaseChannelAdapter):
    """钉钉 Stream 模式适配器。构造函数签名由「跨模块冻结接口契约」锁定。"""

    def __init__(self, config_manager, orchestrator):
        self.config_manager = config_manager
        self.orchestrator = orchestrator

        self.is_running: bool = False
        self._client: Optional[httpx.AsyncClient] = None
        self._runner: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._stop_event: asyncio.Event = asyncio.Event()

        self._access_token: Optional[str] = None
        self._token_expire_at: float = 0.0
        self._connected: bool = False
        # 记录用户最近一次会话 webhook，send_text 时作为兜底通道
        self._session_webhooks: Dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # 配置读取
    # ------------------------------------------------------------------ #
    def _credential(self) -> tuple[str, str]:
        client_id = str(self.config_manager.get("dingtalk_client_id", "") or "").strip()
        client_secret = str(self.config_manager.get("dingtalk_client_secret", "") or "").strip()
        return client_id, client_secret

    def _enabled(self) -> bool:
        return bool(self.config_manager.get("dingtalk_enabled", False))

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=15.0),
                headers={"Content-Type": "application/json"},
            )
        return self._client

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """非阻塞启动：内部拉起后台协程后立刻返回，方便 manager 直接 await。"""
        if self._runner is not None and not self._runner.done():
            logger.debug("钉钉适配器已在运行，忽略重复 start()")
            return

        client_id, client_secret = self._credential()
        if not self._enabled():
            logger.info("钉钉渠道未启用（dingtalk_enabled=false），跳过启动")
            self.is_running = False
            return
        if not client_id or not client_secret:
            logger.warning("钉钉 Client ID / Secret 未配置，无法启动 Stream 长连接")
            self.is_running = False
            return

        self._stop_event = asyncio.Event()
        self._runner = asyncio.create_task(self._run_forever(), name="dingtalk-stream-loop")
        self.is_running = True
        logger.info("钉钉 Stream 适配器已启动（client_id=%s）", client_id)

        # 让后台协程先跑起来；若外层立即被取消，也要顺手收尾
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            await self.stop()
            raise

    async def stop(self) -> None:
        """优雅停止：关 WS -> 取消协程 -> 关 HTTP 客户端。可重复调用。"""
        self._stop_event.set()

        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception as exc:  # noqa: BLE001 - 关闭失败无所谓
                logger.debug("关闭钉钉 WebSocket 时出现异常（可忽略）: %s", exc)

        runner = self._runner
        self._runner = None
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                logger.debug("钉钉长连接协程已取消")
            except Exception as exc:  # noqa: BLE001
                logger.debug("钉钉长连接协程退出时异常: %s", exc)

        self._client, client = None, self._client
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭钉钉 HTTP 客户端失败（已忽略）: %s", exc)

        self._connected = False
        self.is_running = False
        logger.info("钉钉适配器已停止")

    # ------------------------------------------------------------------ #
    # 长连接主循环
    # ------------------------------------------------------------------ #
    async def _run_forever(self) -> None:
        backoff = 3.0
        try:
            while not self._stop_event.is_set():
                delay = 2.0
                try:
                    await self._connect_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 任何异常都要能自动重连
                    logger.warning("钉钉 Stream 连接中断: %s（%.0fs 后重连）", exc, backoff)
                    delay = backoff
                    backoff = min(backoff * 2, 60.0)
                else:
                    backoff = 3.0
                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    logger.debug("钉钉 Stream 重连退避 %.0fs 结束，继续重连", delay)
        finally:
            self._connected = False
            self.is_running = False

    async def _connect_once(self) -> None:
        """建立一次 gateway 长连接并持续收消息，直到连接断开或 stop()。"""
        client = await self._ensure_client()
        endpoint, ticket = await self._open_gateway(client)
        url = _with_ticket(endpoint, ticket)

        import websockets  # 延迟导入，未安装时只影响本渠道

        logger.info("正在连接钉钉 Stream 网关…")
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_size=8 * 1024 * 1024,
            open_timeout=20,
        ) as ws:
            self._ws = ws
            self._connected = True
            logger.info("✅ 钉钉 Stream 长连接已建立")
            while not self._stop_event.is_set():
                raw = await ws.recv()
                await self._handle_frame(ws, raw)
        self._connected = False

    async def _open_gateway(self, client: httpx.AsyncClient) -> tuple[str, str]:
        """调用 gateway/connections/open 换取 ``endpoint`` 与 ``ticket``。"""
        client_id, client_secret = self._credential()
        if not client_id or not client_secret:
            raise RuntimeError("钉钉凭证缺失")

        headers: Dict[str, str] = {}
        token = await self._get_access_token(client)
        if token:
            headers["x-acs-dingtalk-access-token"] = token

        payload = {
            "clientId": client_id,
            "clientSecret": client_secret,
            "subscriptions": [
                {"type": "CALLBACK", "topic": BOT_CALLBACK_TOPIC},
            ],
            "ua": "chrono-secretary/1.0 (python)",
            "localIp": "",
        }
        resp = await client.post(GATEWAY_OPEN_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

        endpoint = data.get("endpoint") or ""
        ticket = data.get("ticket") or ""
        if not endpoint or not ticket:
            raise RuntimeError(f"gateway/connections/open 返回异常: {data!r}")
        return endpoint, ticket

    async def _get_access_token(self, client: httpx.AsyncClient, force: bool = False) -> Optional[str]:
        """获取/复用 accessToken（提前 60s 过期）。失败返回 None，不阻塞长连接建立。"""
        client_id, client_secret = self._credential()
        if not client_id or not client_secret:
            return None

        if not force and self._access_token and time.time() < self._token_expire_at - 60:
            return self._access_token

        try:
            resp = await client.post(
                TOKEN_URL,
                json={"appKey": client_id, "appSecret": client_secret},
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("accessToken") or data.get("access_token")
            expire_in = int(data.get("expireIn") or data.get("expiresIn") or 7200)
            if not token:
                logger.warning("钉钉 accessToken 响应缺少字段: %s", data)
                return None
            self._access_token = str(token)
            self._token_expire_at = time.time() + expire_in
            return self._access_token
        except Exception as exc:  # noqa: BLE001
            logger.warning("获取钉钉 accessToken 失败: %s", exc)
            return None

    # ------------------------------------------------------------------ #
    # 帧处理
    # ------------------------------------------------------------------ #
    async def _ack(self, ws, message_id: str) -> None:
        """回调 ACK。钉钉若在 3 秒内收不到 ACK 会重投同一条消息。"""
        if not message_id:
            return
        ack = {
            "code": 200,
            "headers": {"messageId": message_id, "contentType": "application/json"},
            "message": "OK",
            "data": json.dumps({"response": None}),
        }
        try:
            await ws.send(json.dumps(ack, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            logger.debug("发送钉钉 ACK 失败: %s", exc)

    async def _handle_frame(self, ws, raw: Any) -> None:
        try:
            frame = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        except (TypeError, ValueError):
            logger.warning("收到无法解析的钉钉帧: %r", raw)
            return
        if not isinstance(frame, dict):
            return

        headers = frame.get("headers") or {}
        message_id = str(headers.get("messageId") or "")
        frame_type = str(frame.get("type") or "").upper()

        if frame_type == "SYSTEM":
            # 心跳 / 连接建立通知
            await self._ack(ws, message_id)
            return

        if frame_type != "CALLBACK":
            await self._ack(ws, message_id)
            return

        data = frame.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                data = {}

        # 先 ACK 再执行业务：业务里可能要调 LLM（几秒），不能让平台等超时重投
        await self._ack(ws, message_id)

        if not isinstance(data, dict):
            return
        try:
            await self._dispatch(data)
        except Exception as exc:  # noqa: BLE001 - 单个消息处理失败不能拖垮长连接
            logger.exception("处理钉钉消息失败: %s", exc)

    async def _dispatch(self, data: Dict[str, Any]) -> None:
        """从钉钉回调 payload 提取统一消息体，交给 orchestrator。"""
        msg_type = str(data.get("msgtype") or data.get("msgType") or "text").lower()

        text_obj = data.get("text")
        content = ""
        if isinstance(text_obj, dict):
            content = str(text_obj.get("content") or "")
        elif isinstance(text_obj, str):
            content = text_obj
        elif msg_type == "text":
            content = str(data.get("content") or "")
        content = content.strip()

        if not content:
            logger.debug("钉钉消息无文本内容（msgtype=%s），忽略", msg_type)
            return

        sender_id = (
            data.get("senderStaffId")
            or data.get("senderId")
            or data.get("chatbotUserId")
            or "unknown"
        )
        sender_id = str(sender_id)

        platform_msg_id = (
            data.get("msgId")
            or data.get("messageId")
            or data.get("processQueryKey")
            or f"dingtalk-{uuid.uuid4().hex[:16]}"
        )
        platform_msg_id = str(platform_msg_id)

        # ---- 方法 A：引用消息 ID（两处结构都兼容）----
        replied: Optional[str] = None
        for candidate in (
            data.get("repliedMsgId"),
            (text_obj.get("repliedMsgId") if isinstance(text_obj, dict) else None),
            data.get("originalMsgId"),
            ((data.get("quoteMessage") or {}) if isinstance(data.get("quoteMessage"), dict) else {}).get("msgId"),
        ):
            if candidate:
                replied = str(candidate)
                break

        webhook = str(data.get("sessionWebhook") or "")
        if webhook:
            self._session_webhooks[sender_id] = webhook

        # context_token 复用 sessionWebhook：钉钉群/单聊会话都可以用它直接回消息
        ctx = webhook or str(data.get("conversationId") or "") or None

        message = UnifiedIncomingMessage(
            channel=ChannelType.DINGTALK,
            sender_id=sender_id,
            message_id=platform_msg_id,
            text=content,
            reply_to_msg_id=replied,
            context_token=ctx,
            raw_payload=data,
        )

        logger.info(
            "📥 钉钉消息 sender=%s msg_id=%s reply_to=%s text=%r",
            sender_id,
            platform_msg_id,
            replied,
            content[:80],
        )

        # orchestrator 内部负责：解析意图 -> 落库 -> 回复（经 channel_manager.send_text）-> 绑定消息
        reply = await self.orchestrator.handle_message(message)
        if reply:
            logger.debug("钉钉消息已处理，回复: %r", str(reply)[:120])

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    async def send_text(
        self,
        recipient_id: str,
        text: str,
        context_token: Optional[str] = None,
    ) -> Optional[str]:
        """发送文本并返回平台消息 ID（用于 MessageBinding 引用追踪）。

        优先走会话 ``sessionWebhook``（响应最快、无需 accessToken），
        拿不到 webhook 时退回机器人单聊 ``batchSend``（返回 ``processQueryKey``）。
        """
        text = (text or "").strip()
        if not text:
            return None
        if len(text) > MAX_TEXT_LENGTH:
            text = text[: MAX_TEXT_LENGTH - 3] + "..."
        recipient_id = str(recipient_id or "")

        try:
            client = await self._ensure_client()
        except Exception as exc:  # noqa: BLE001
            logger.error("钉钉发送失败（HTTP 客户端不可用）: %s", exc)
            return None

        webhook: Optional[str] = None
        if context_token and str(context_token).startswith("http"):
            webhook = str(context_token)
        if webhook is None:
            webhook = self._session_webhooks.get(recipient_id)
        if webhook is None:
            try:
                stored = await repo.get_context_token("dingtalk", recipient_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("读取钉钉 context_token 失败: %s", exc)
                stored = None
            if stored and str(stored).startswith("http"):
                webhook = str(stored)

        # 优先走机器人单聊接口 batchSend：既能拿到真实的 processQueryKey 供用户「引用回复」，又避免重复发送
        robot_msg_id = await self._send_via_robot(client, recipient_id, text)
        if robot_msg_id:
            return robot_msg_id

        # 若单聊接口不可用（例如群聊场景），回退使用 sessionWebhook 发送一次
        if webhook:
            return await self._send_via_webhook(client, webhook, text)

        return None

    async def _send_via_webhook(
        self,
        client: httpx.AsyncClient,
        webhook: str,
        text: str,
    ) -> Optional[str]:
        try:
            resp = await client.post(
                webhook,
                json={"msgtype": "text", "text": {"content": text}},
            )
            data = resp.json() if resp.content else {}
            errcode = data.get("errcode", 0)
            if errcode not in (0, None):
                logger.warning("钉钉 sessionWebhook 发送失败: %s", data)
                return None
            # sessionWebhook 成功时通常只返回 {"errcode": 0, "errmsg": "ok"}，必须返回非空标识防止上层误判失败而二次重发
            return str(data.get("msgId") or data.get("processQueryKey") or data.get("messageId") or f"dt-wh-{uuid.uuid4().hex[:12]}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("钉钉 sessionWebhook 发送异常: %s", exc)
            return None

    async def _send_via_robot(
        self,
        client: httpx.AsyncClient,
        recipient_id: str,
        text: str,
    ) -> Optional[str]:
        client_id, _ = self._credential()
        if not client_id or not recipient_id:
            logger.warning("钉钉单聊发送缺少 robotCode 或 recipient_id")
            return None

        token = await self._get_access_token(client)
        if not token:
            logger.warning("钉钉单聊发送失败：无法获取 accessToken")
            return None

        payload = {
            "robotCode": client_id,
            "userIds": [recipient_id],
            "msgKey": "sampleText",
            "msgParam": json.dumps({"content": text}, ensure_ascii=False),
        }
        try:
            resp = await client.post(
                ROBOT_OTO_URL,
                json=payload,
                headers={"x-acs-dingtalk-access-token": token},
            )
            resp.raise_for_status()
            data = resp.json()
            process_key = data.get("processQueryKey")
            if not process_key:
                logger.warning("钉钉单聊发送返回异常: %s", data)
            return process_key
        except Exception as exc:  # noqa: BLE001
            logger.warning("钉钉单聊发送异常: %s", exc)
            return None


__all__ = ["DingTalkChannelAdapter", "BOT_CALLBACK_TOPIC"]
