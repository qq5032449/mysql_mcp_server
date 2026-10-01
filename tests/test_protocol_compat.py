"""协议兼容回归：未初始化会话的请求放行（TRAE 重连场景）。"""
from unittest.mock import AsyncMock

import pytest
from mcp.server.session import InitializationState, ServerSession
from mcp.types import (
    ClientCapabilities, Implementation, InitializeRequest,
    InitializeRequestParams, ListToolsRequest,
)

# 导入即应用补丁
import mysql_mcp_server.server  # noqa: F401


def _fake_responder(request_root):
    responder = AsyncMock()
    responder.request = AsyncMock()
    responder.request.root = request_root
    return responder


def _bare_session(state):
    s = object.__new__(ServerSession)
    s._initialization_state = state
    return s


def test_patch_applied():
    assert getattr(ServerSession, "_auto_init_patched", False) is True


@pytest.mark.asyncio
async def test_uninitialized_request_auto_initializes():
    """未初始化会话收到 tools/list → 自动置为已初始化并放行（不再抛 RuntimeError）。"""
    s = _bare_session(InitializationState.NotInitialized)
    await ServerSession._received_request(s, _fake_responder(ListToolsRequest()))
    assert s._initialization_state == InitializationState.Initialized


@pytest.mark.asyncio
async def test_uninitialized_notification_auto_initializes():
    """未初始化会话收到通知（如客户端私有通知）→ 自动置为已初始化。"""
    s = _bare_session(InitializationState.NotInitialized)
    await ServerSession._received_notification(s, AsyncMock())
    assert s._initialization_state == InitializationState.Initialized


@pytest.mark.asyncio
async def test_initialize_request_not_preinitialized(monkeypatch):
    """initialize 请求不由补丁提前放行，交给原始逻辑处理。"""
    mock_orig = AsyncMock(return_value=None)
    monkeypatch.setattr(ServerSession, "_received_request_original", mock_orig)
    s = _bare_session(InitializationState.NotInitialized)
    req = InitializeRequest(params=InitializeRequestParams(
        protocolVersion="2025-06-18",
        capabilities=ClientCapabilities(),
        clientInfo=Implementation(name="c", version="0"),
    ))
    await ServerSession._received_request(s, _fake_responder(req))
    mock_orig.assert_awaited_once()
    assert s._initialization_state == InitializationState.NotInitialized


@pytest.mark.asyncio
async def test_original_logic_still_rejects():
    """对照：SDK 原始实现对未初始化的非 initialize 请求仍抛错（证明补丁有实际差异）。"""
    s = _bare_session(InitializationState.NotInitialized)
    with pytest.raises(RuntimeError, match="before initialization"):
        await ServerSession._received_request_original(
            s, _fake_responder(ListToolsRequest()))
