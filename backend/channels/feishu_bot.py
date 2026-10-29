"""
backend/channels/feishu_bot.py
==============================
飞书（Feishu / Lark）机器人适配器（同时支持「WebSocket 长连接模式」与「HTTP 事件回调模式」）。

核心特性：
1. 免公网 IP 长连接模式（推荐）：
   - 通过 `https://open.feishu.cn/callback/ws/endpoint` 获取 WSS 接入点；
   - 内置轻量级纯 Python Protobuf (`pbbp2.Frame`) 编解码器，无需编译安装重量级 C 扩展；
   - 自动发送心跳帧 (`ping`) 并在 3 秒内回执 `{"code": 200}` 确认帧。
2. 消息引用检测（方法 A · Quote Reply · 0 模型成本）：
   - 自动提取飞书 `im.message.receive_v1` 事件中的 `message.parent_id` / `message.root_id` 作为 `reply_to_msg_id`；
   - 主动发送消息时返回飞书 `message_id` (`om_xxx`)，供 `orchestrator` 写入 `message_bindings` 形成闭环。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx
import websockets

from backend.core.schemas import BaseChannelAdapter, ChannelType, UnifiedIncomingMessage

logger = logging.getLogger(__name__)

FEISHU_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
FEISHU_WS_ENDPOINT_URL = "https://open.feishu.cn/callback/ws/endpoint"
FEISHU_SEND_MSG_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

_MENTION_RE = re.compile(r"@_user_\d+\s*")


# --------------------------------------------------------------------------- #
# 轻量级飞书长连接 Protobuf (pbbp2.Frame) 编解码器（0 外部 C 依赖）
# --------------------------------------------------------------------------- #
def _encode_varint(val: int) -> bytes:
    out = bytearray()
    val &= (1 << 64) - 1
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _decode_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
    raise ValueError("Truncated protobuf varint")


def _encode_len_delimited(field_num: int, data: bytes) -> bytes:
    tag = (field_num << 3) | 2
    return _encode_varint(tag) + _encode_varint(len(data)) + data


def _encode_header(key: str, value: str) -> bytes:
    kb = key.encode("utf-8")
    vb = value.encode("utf-8")
    return _encode_len_delimited(1, kb) + _encode_len_delimited(2, vb)


def encode_feishu_frame(
    seq_id: int,
    log_id: int,
    service: int,
    method: int,
    headers: Dict[str, str],
    payload: bytes = b"",
) -> bytes:
    """将飞书 WebSocket 帧序列化为 pbbp2.Frame 二进制格式。"""
    out = bytearray()
    out += _encode_varint((1 << 3) | 0) + _encode_varint(seq_id)
    out += _encode_varint((2 << 3) | 0) + _encode_varint(log_id)
    out += _encode_varint((3 << 3) | 0) + _encode_varint(service)
    out += _encode_varint((4 << 3) | 0) + _encode_varint(method)
    for k, v in headers.items():
        out += _encode_len_delimited(5, _encode_header(str(k), str(v)))
    if payload:
        out += _encode_len_delimited(8, payload)
    return bytes(out)


def _decode_header(buf: bytes) -> Tuple[str, str]:
    pos = 0
    k = ""
    v = ""
    while pos < len(buf):
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 2:
            length, pos = _decode_varint(buf, pos)
            val_bytes = buf[pos : pos + length]
            pos += length
            if field_num == 1:
                k = val_bytes.decode("utf-8", errors="replace")
            elif field_num == 2:
                v = val_bytes.decode("utf-8", errors="replace")
        elif wire_type == 0:
            _, pos = _decode_varint(buf, pos)
        else:
            break
    return k, v


def decode_feishu_frame(buf: bytes) -> Dict[str, Any]:
    """解析飞书 WebSocket 下发的 pbbp2.Frame 二进制帧。"""
    pos = 0
    frame: Dict[str, Any] = {
        "seq_id": 0,
        "log_id": 0,
        "service": 0,
        "method": 0,
        "headers": {},
        "payload": b"",
    }
    while pos < len(buf):
        tag, pos = _decode_varint(buf, pos)
        field_num = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            val, pos = _decode_varint(buf, pos)
            if field_num == 1:
                frame["seq_id"] = val
            elif field_num == 2:
                frame["log_id"] = val
            elif field_num == 3:
                frame["service"] = val
            elif field_num == 4:
                frame["method"] = val
        elif wire_type == 2:
            length, pos = _decode_varint(buf, pos)
            chunk = buf[pos : pos + length]
            pos += length
            if field_num == 5:
                hk, hv = _decode_header(chunk)
                if hk:
                    frame["headers"][hk] = hv
            elif field_num == 8:
                frame["payload"] = chunk
        elif wire_type == 1:
            pos += 8
        elif wire_type == 5:
            pos += 4
        else:
            break
    return frame


class FeishuChannelAdapter(BaseChannelAdapter):
    """飞书机器人渠道适配器（支持长连接 WebSocket 与 HTTP Webhook 双模式）。"""

    def __init__(self, config_manager, orchestrator):
        self.config_manager = config_manager
        self.orchestrator = orchestrator

        self.is_running: bool = False
        self._client: Optional[httpx.AsyncClient] = None
        self._runner: Optional[asyncio.Task] = None
        self._ws: Any = None
        self._stop_event: asyncio.Event = asyncio.Event()

        self._tenant_token: str = ""
        self._token_expire_at: float = 0.0
        self._seen_msg_ids: OrderedDict[str, float] = OrderedDict()

    def _credentials(self) -> Tuple[str, str]:
        app_id = str(self.config_manager.get("feishu_app_id", "") or "").strip()
        app_secret = str(self.config_manager.get("feishu_app_secret", "") or "").strip()
        return app_id, app_secret

    def _enabled(self) -> bool:
        return bool(self.config_manager.get("feishu_enabled", False))

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(25.0, connect=10.0),
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        return self._client

    async def _get_tenant_access_token(self) -> str:
        now = time.time()
        if self._tenant_token and now < self._token_expire_at - 120:
            return self._tenant_token

        app_id, app_secret = self._credentials()
        if not app_id or not app_secret:
            raise RuntimeError("飞书 App ID 或 App Secret 未配置")

        client = await self._ensure_client()
        resp = await client.post(
            FEISHU_TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0 or not data.get("tenant_access_token"):
            raise RuntimeError(f"获取飞书 tenant_access_token 失败: {data.get('msg') or data}")

        self._tenant_token = str(data["tenant_access_token"])
        expire = int(data.get("expire") or 7200)
        self._token_expire_at = now + expire
        return self._tenant_token

    async def start(self) -> None:
        if self._runner is not None and not self._runner.done():
            return

        app_id, app_secret = self._credentials()
        if not self._enabled():
            logger.info("飞书渠道未启用（feishu_enabled=false），跳过启动")
            self.is_running = False
            return
        if not app_id or not app_secret:
            logger.warning("飞书 App ID / App Secret 未配置，无法启动")
            self.is_running = False
            return

        # 先验证凭证有效性
        try:
            await self._get_tenant_access_token()
        except Exception as exc:
            logger.error("飞书凭证校验失败，请检查 App ID 与 App Secret: %s", exc)
            self.is_running = False
            return

        self._stop_event = asyncio.Event()
        self._runner = asyncio.create_task(self._ws_loop(), name="feishu-ws-loop")
        self.is_running = True
        logger.info("飞书机器人适配器已启动 (app_id=%s)", app_id)

    async def stop(self) -> None:
        self._stop_event.set()
        self.is_running = False

        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

        runner = self._runner
        self._runner = None
        if runner is not None and not runner.done():
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):
                pass

        client = self._client
        self._client = None
        if client is not None and not client.is_closed:
            try:
                await client.aclose()
            except Exception:
                pass

    async def _ws_loop(self) -> None:
        """飞书长连接主循环（断线自动重连；若用户仅配了 Webhook 模式则静默待命）。"""
        backoff = 3.0
        while not self._stop_event.is_set():
            app_id, app_secret = self._credentials()
            if not self._enabled() or not app_id or not app_secret:
                break
            try:
                client = await self._ensure_client()
                resp = await client.post(
                    FEISHU_WS_ENDPOINT_URL,
                    headers={"locale": "zh"},
                    json={"AppID": app_id, "AppSecret": app_secret},
                )
                body = resp.json()
                data = body.get("data") or {}
                ws_url = str(data.get("URL") or "").strip()
                if not ws_url:
                    logger.info(
                        "飞书长连接未开启或仅使用 Webhook 回调模式 (msg=%s)，可通过 /api/feishu/webhook 接收消息",
                        body.get("msg") or body.get("code"),
                    )
                    await asyncio.wait_for(self._stop_event.wait(), timeout=60.0)
                    continue

                client_cfg = data.get("ClientConfig") or {}
                ping_interval = float(client_cfg.get("PingInterval") or 90.0)
                parsed = urlparse(ws_url)
                qs = parse_qs(parsed.query)
                service_id = int((qs.get("service_id") or ["0"])[0] or 0)

                async with websockets.connect(ws_url, ping_interval=None, close_timeout=5) as ws:
                    self._ws = ws
                    backoff = 3.0
                    logger.info("✅ 飞书 WebSocket 长连接已建立")
                    ping_task = asyncio.create_task(self._ping_worker(ws, service_id, ping_interval))
                    try:
                        while not self._stop_event.is_set():
                            raw_msg = await ws.recv()
                            if isinstance(raw_msg, bytes):
                                await self._handle_ws_frame(ws, raw_msg)
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except (asyncio.CancelledError, Exception):
                            pass
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.warning("飞书长连接断开，%.0f 秒后重连: %s", backoff, exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 1.8, 60.0)

    async def _ping_worker(self, ws: Any, service_id: int, interval: float) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(max(15.0, interval))
            frame = encode_feishu_frame(
                seq_id=int(time.time() * 1000),
                log_id=0,
                service=service_id,
                method=0,
                headers={"type": "ping"},
            )
            await ws.send(frame)

    async def _handle_ws_frame(self, ws: Any, raw: bytes) -> None:
        try:
            frame = decode_feishu_frame(raw)
        except Exception as exc:
            logger.debug("解析飞书 WS 帧失败: %s", exc)
            return

        # method == 1 为数据帧 (Event)
        if frame.get("method") != 1:
            return

        headers: Dict[str, str] = frame.get("headers") or {}
        msg_type = headers.get("type", "")
        payload_bytes: bytes = frame.get("payload") or b""

        # 先在 3 秒内回执 200 OK，防止飞书服务端判定超时重推
        ack_payload = json.dumps({"code": 200}).encode("utf-8")
        ack_frame = encode_feishu_frame(
            seq_id=frame.get("seq_id", 0),
            log_id=frame.get("log_id", 0),
            service=frame.get("service", 0),
            method=1,
            headers=headers,
            payload=ack_payload,
        )
        try:
            await ws.send(ack_frame)
        except Exception:
            pass

        if msg_type != "event" or not payload_bytes:
            return

        try:
            event_json = json.loads(payload_bytes.decode("utf-8", errors="replace"))
        except Exception:
            return

        asyncio.create_task(self.handle_webhook_event(event_json))

    def _is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen_msg_ids:
            return True
        self._seen_msg_ids[msg_id] = now
        while len(self._seen_msg_ids) > 500:
            self._seen_msg_ids.popitem(last=False)
        return False

    @staticmethod
    def parse_event_message(payload: Dict[str, Any]) -> Optional[UnifiedIncomingMessage]:
        """从飞书 v2.0 事件 (`im.message.receive_v1`) 提取统一消息结构与引用 ID。"""
        if not isinstance(payload, dict):
            return None
        header = payload.get("header") or {}
        event_type = header.get("event_type") or payload.get("type") or ""
        if event_type and event_type != "im.message.receive_v1":
            return None

        event = payload.get("event") or {}
        message = event.get("message") or {}
        sender = event.get("sender") or {}
        if sender.get("sender_type") == "app":
            return None  # 忽略机器人自身发出的消息

        msg_id = str(message.get("message_id") or "").strip()
        if not msg_id:
            return None

        # 解析文本内容
        raw_content = message.get("content") or "{}"
        text = ""
        if isinstance(raw_content, str):
            try:
                content_obj = json.loads(raw_content)
                if isinstance(content_obj, dict):
                    text = str(content_obj.get("text") or "").strip()
            except Exception:
                text = raw_content.strip()
        elif isinstance(raw_content, dict):
            text = str(raw_content.get("text") or "").strip()

        text = _MENTION_RE.sub("", text).strip()
        if not text:
            return None

        # 确定回复目标：群聊走 chat_id (oc_xxx)，单聊优先走 open_id (ou_xxx) 或 chat_id
        chat_type = str(message.get("chat_type") or "p2p").lower()
        chat_id = str(message.get("chat_id") or "").strip()
        sender_id_obj = sender.get("sender_id") or {}
        open_id = str(sender_id_obj.get("open_id") or sender_id_obj.get("user_id") or "").strip()
        recipient_id = chat_id if (chat_type == "group" and chat_id) else (open_id or chat_id)
        if not recipient_id:
            return None

        # 方法 A：提取被引用消息的 parent_id / root_id
        reply_to_msg_id = str(message.get("parent_id") or message.get("root_id") or "").strip() or None

        return UnifiedIncomingMessage(
            channel=ChannelType.FEISHU,
            sender_id=recipient_id,
            message_id=msg_id,
            text=text,
            reply_to_msg_id=reply_to_msg_id,
            context_token=chat_id or None,
            raw_payload=payload,
        )

    async def handle_webhook_event(self, payload: Dict[str, Any]) -> Optional[str]:
        """处理飞书事件并交由 orchestrator 编排。"""
        incoming = self.parse_event_message(payload)
        if incoming is None:
            return None
        if self._is_duplicate(incoming.message_id):
            return None

        handler = self.orchestrator
        if handler is None:
            from backend.core.orchestrator import orchestrator as default_orch
            handler = default_orch

        try:
            return await handler.handle_message(incoming)
        except Exception as exc:
            logger.exception("飞书消息处理异常: %s", exc)
            return None

    async def send_text(
        self,
        recipient_id: str,
        text: str,
        context_token: Optional[str] = None,
    ) -> Optional[str]:
        """调用飞书发送消息接口，并返回飞书生成的 message_id (`om_xxx`) 以便用户引用回复。"""
        target = (recipient_id or context_token or "").strip()
        if not target or not text:
            return None

        if target.startswith("oc_"):
            receive_id_type = "chat_id"
        elif target.startswith("ou_"):
            receive_id_type = "open_id"
        elif target.startswith("on_"):
            receive_id_type = "union_id"
        else:
            receive_id_type = "open_id"

        token = await self._get_tenant_access_token()
        client = await self._ensure_client()
        resp = await client.post(
            f"{FEISHU_SEND_MSG_URL}?receive_id_type={receive_id_type}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": target,
                "msg_type": "text",
                "content": json.dumps({"text": str(text)}, ensure_ascii=False),
            },
        )
        data = resp.json()
        if data.get("code") != 0:
            logger.error("飞书发送消息失败: %s", data)
            return None

        msg_data = data.get("data") or {}
        return str(msg_data.get("message_id") or "").strip() or None
