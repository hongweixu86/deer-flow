"""Tests for the chat-level fallback path when OutboundMessage has no thread_id.

When the executor (Task 7) tries to push a result back to Feishu for a scheduled
run that failed before creating a langgraph thread, the OutboundMessage has no
``thread_id``. The Feishu channel must still push an alert by sending a
chat-level message via ``CreateMessageRequest`` (not ``ReplyMessageRequest``).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from app.channels.feishu import FeishuChannel
from app.channels.message_bus import MessageBus, OutboundMessage


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_channel_with_request_mocks() -> tuple[FeishuChannel, MagicMock, MagicMock]:
    """Build a FeishuChannel with mocked CreateMessageRequest/ReplyMessageRequest.

    Returns:
        A tuple of (channel, create_request_mock, reply_request_mock). The
        request mocks are also attached to the channel as ``_CreateMessageRequest``
        and ``_ReplyMessageRequest`` to mirror what ``start()`` would do.
    """
    bus = MessageBus()
    channel = FeishuChannel(bus=bus, config={"app_id": "x", "app_secret": "y"})

    create_request = MagicMock(name="CreateMessageRequest")
    reply_request = MagicMock(name="ReplyMessageRequest")

    channel._api_client = MagicMock()
    channel._lark = MagicMock()
    channel._main_loop = MagicMock()
    channel._CreateMessageRequest = create_request
    channel._CreateMessageRequestBody = MagicMock(name="CreateMessageRequestBody")
    channel._ReplyMessageRequest = reply_request
    channel._ReplyMessageRequestBody = MagicMock(name="ReplyMessageRequestBody")

    return channel, create_request, reply_request


def test_outbound_message_thread_id_is_optional():
    """OutboundMessage must accept thread_id=None without TypeError."""
    msg = OutboundMessage(
        channel_name="feishu",
        chat_id="oc_1",
        thread_id=None,
        text="alert",
        connection_id=None,
        owner_user_id="u1",
    )
    assert msg.thread_id is None


def test_thread_id_none_uses_chat_message():
    """When thread_id is None, _on_outbound should call CreateMessageRequest.builder."""
    channel, create_request, reply_request = _make_channel_with_request_mocks()

    msg = OutboundMessage(
        channel_name="feishu",
        chat_id="oc_1",
        thread_id=None,
        text="hello",
        connection_id=None,
        owner_user_id="u1",
    )

    async def go():
        await channel._on_outbound(msg)

    _run(go())

    # Chat-level path was used
    create_request.builder.assert_called()
    # Reply path was NOT used
    reply_request.builder.assert_not_called()
    # The chat-level create API was invoked
    channel._api_client.im.v1.message.create.assert_called()


def test_thread_id_none_dispatches_to_chat_message_helper():
    """When thread_id is None, _on_outbound must delegate to _send_chat_message.

    This pins down the explicit dispatch in the channel's _on_outbound override.
    """
    channel, create_request, reply_request = _make_channel_with_request_mocks()

    # Stub the helpers so we can observe dispatch without performing real I/O.
    send_chat = MagicMock()
    existing_send = MagicMock()
    channel._send_chat_message = send_chat
    channel.send = existing_send

    msg = OutboundMessage(
        channel_name="feishu",
        chat_id="oc_1",
        thread_id=None,
        text="hello",
        connection_id=None,
        owner_user_id="u1",
    )

    async def go():
        await channel._on_outbound(msg)

    _run(go())

    send_chat.assert_called_once()
    existing_send.assert_not_called()


def test_thread_id_present_uses_reply_path():
    """When thread_id is set, _on_outbound should go through the existing reply path."""
    channel, create_request, reply_request = _make_channel_with_request_mocks()

    msg = OutboundMessage(
        channel_name="feishu",
        chat_id="oc_1",
        thread_id="deer-thread-1",
        text="hello",
        thread_ts="om_source_1",
        connection_id=None,
        owner_user_id="u1",
    )

    async def go():
        await channel._on_outbound(msg)

    _run(go())

    # Reply path was used
    reply_request.builder.assert_called()
    # Chat-level create was NOT used
    channel._api_client.im.v1.message.create.assert_not_called()
