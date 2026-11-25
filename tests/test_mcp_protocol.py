"""The stdio JSON-RPC layer of the MCP server, against a stand-in tool."""

from __future__ import annotations

import io
import json
import os
import threading
from collections.abc import Mapping
from typing import Any

from upgrade_impact.mcp_server.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    InvalidParamsError,
    Progress,
    StdioServer,
    Tool,
    ToolResult,
)


def _req(id_: int, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = dict(params)
    return msg


def _call(id_: int, name: str, args: Mapping[str, Any], **params: Any) -> dict[str, Any]:
    return _req(id_, "tools/call", {"name": name, "arguments": dict(args), **params})


def _session(server: StdioServer, *messages: Any) -> list[dict[str, Any]]:
    """Run the server over in-memory stdio; returns every message it wrote."""
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    stdout = io.StringIO()
    server.serve(io.StringIO("\n".join(lines) + "\n"), stdout)
    return [json.loads(line) for line in stdout.getvalue().splitlines()]


def _by_id(replies: list[dict[str, Any]], id_: int) -> dict[str, Any]:
    return next(r for r in replies if r.get("id") == id_)


def _echo(args: Mapping[str, Any], progress: Progress) -> ToolResult:
    if args.get("explode"):
        raise RuntimeError("kaboom")
    if args.get("bad"):
        raise InvalidParamsError("bad argument")
    progress(1, 2, "half way")
    return ToolResult(text=str(args.get("say", "")), structured={"said": args.get("say")})


ECHO = Tool(
    name="echo",
    title="Echo",
    description="echo",
    input_schema={"type": "object", "properties": {"say": {"type": "string"}}},
    output_schema={"type": "object", "properties": {"said": {"type": "string"}}},
    handler=_echo,
    annotations={"readOnlyHint": True},
)


def _echo_server(**kwargs: Any) -> StdioServer:
    return StdioServer([ECHO], name="t", title="T", version="9", instructions="hi", **kwargs)


def _init(version: str = "2025-06-18") -> dict[str, Any]:
    return _req(0, "initialize", {"protocolVersion": version, "capabilities": {}})


def test_handshake_negotiates_versions() -> None:
    for asked, got in [
        ("2025-06-18", "2025-06-18"),
        ("2024-11-05", "2024-11-05"),
        ("2025-11-25", "2025-11-25"),
        ("2099-01-01", "2025-11-25"),
    ]:
        reply = _session(_echo_server(), _init(asked))[0]["result"]
        assert reply["protocolVersion"] == got
        assert reply["capabilities"] == {"tools": {"listChanged": False}}
        assert reply["instructions"] == "hi"


def test_tool_listing_follows_the_negotiated_version() -> None:
    new = _by_id(_session(_echo_server(), _init("2025-06-18"), _req(1, "tools/list")), 1)
    tool = new["result"]["tools"][0]
    assert tool["title"] == "Echo"
    assert "outputSchema" in tool
    assert tool["annotations"]["readOnlyHint"] is True
    old = _by_id(_session(_echo_server(), _init("2024-11-05"), _req(1, "tools/list")), 1)
    tool = old["result"]["tools"][0]
    assert "title" not in tool
    assert "outputSchema" not in tool
    assert "annotations" not in tool


def test_structured_content_only_from_2025_06_18() -> None:
    call = _call(1, "echo", {"say": "x"})
    new = _by_id(_session(_echo_server(), _init("2025-06-18"), call), 1)["result"]
    assert new["structuredContent"] == {"said": "x"}
    assert new["isError"] is False
    old = _by_id(_session(_echo_server(), _init("2025-03-26"), call), 1)["result"]
    assert "structuredContent" not in old
    assert old["content"] == [{"type": "text", "text": "x"}]


def test_errors_and_edge_cases() -> None:
    replies = _session(
        _echo_server(),
        "{not json",
        json.dumps({"id": 1, "method": "ping"}),
        _req(2, "ping"),
        _req(3, "server/discover"),
        _req(4, "tools/call", {"name": "nope"}),
        _call(5, "echo", {"bad": True}),
        _call(6, "echo", {"explode": True}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 99, "result": {}},
        {"jsonrpc": "2.0", "id": 7, "params": {}},
        _req(8, "tools/call", {"name": "echo", "arguments": [1]}),
        {"jsonrpc": "2.0", "id": 9, "method": "ping", "params": [1]},
    )
    assert replies[0]["error"]["code"] == PARSE_ERROR
    assert replies[1]["error"]["code"] == INVALID_REQUEST
    assert _by_id(replies, 2)["result"] == {}
    assert _by_id(replies, 3)["error"]["code"] == METHOD_NOT_FOUND
    assert _by_id(replies, 4)["error"]["code"] == INVALID_PARAMS
    assert _by_id(replies, 5)["error"]["code"] == INVALID_PARAMS
    failed = _by_id(replies, 6)["result"]
    assert failed["isError"] is True
    assert "kaboom" in failed["content"][0]["text"]
    assert _by_id(replies, 7)["error"]["code"] == INVALID_REQUEST
    assert _by_id(replies, 8)["error"]["code"] == INVALID_PARAMS
    assert _by_id(replies, 9)["error"]["code"] == INVALID_PARAMS
    assert not any(r.get("id") == 99 for r in replies)


def test_batch() -> None:
    stdout = io.StringIO()
    _echo_server().serve(io.StringIO(json.dumps([_req(1, "ping"), _req(2, "ping")])), stdout)
    batch = json.loads(stdout.getvalue())
    assert [r["id"] for r in batch] == [1, 2]


def test_progress_notifications_need_a_token() -> None:
    with_token = _session(
        _echo_server(), _init(), _call(1, "echo", {"say": "x"}, _meta={"progressToken": "t1"})
    )
    progress = [r for r in with_token if r.get("method") == "notifications/progress"]
    assert progress == [
        {
            "jsonrpc": "2.0",
            "method": "notifications/progress",
            "params": {"progressToken": "t1", "progress": 1, "total": 2, "message": "half way"},
        }
    ]
    without = _session(_echo_server(), _init(), _call(1, "echo", {"say": "x"}))
    assert not [r for r in without if r.get("method")]


def test_slow_calls_do_not_block_and_cancelled_calls_are_not_answered() -> None:
    release = threading.Event()
    started = threading.Event()

    def slow(args: Mapping[str, Any], progress: Progress) -> ToolResult:
        started.set()
        release.wait(5)
        return ToolResult(text="late")

    server = StdioServer(
        [Tool("slow", "Slow", "slow", {"type": "object"}, slow)], name="t", title="T", version="1"
    )
    stdin_r, stdin_w = os.pipe()
    stdout = io.StringIO()
    reader = os.fdopen(stdin_r, "r")
    writer = os.fdopen(stdin_w, "w")
    worker = threading.Thread(target=server.serve, args=(reader, stdout))
    worker.start()
    writer.write(json.dumps(_call(1, "slow", {})) + "\n")
    writer.flush()
    assert started.wait(5)
    writer.write(json.dumps(_req(2, "ping")) + "\n")
    cancel = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
    writer.write(json.dumps(cancel) + "\n")
    writer.flush()
    # The ping is answered while the slow call is still running.
    for _ in range(100):
        if '"id":2' in stdout.getvalue():
            break
        threading.Event().wait(0.02)
    assert '"id":2' in stdout.getvalue()
    release.set()
    writer.close()
    worker.join(10)
    reader.close()
    ids = [json.loads(line).get("id") for line in stdout.getvalue().splitlines()]
    assert ids == [2]
