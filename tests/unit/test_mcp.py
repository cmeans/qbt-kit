"""The MCP surface, and that its refusals are usable.

A refused tool call must tell the model what happened and what to do instead. If
a QbtError escapes, the model sees only `UnexpectedToolError: Error executing
tool X`, cannot distinguish a missing confirmation from a dead server, and
cannot self-correct — so every refusal is asserted to be structured.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.types import CallToolResult, TextContent

from qbtkit import errors
from qbtkit.mcp import server as S
from qbtkit.spec import load_all

CURATED_MAX = 20  # a long tool list costs context and degrades tool choice


def call(tool: str, args: dict | None = None) -> dict:
    res = asyncio.run(S.server.call_tool(tool, args or {}))
    # call_tool is typed CallToolResult | InputRequiredResult; only the former
    # carries content, and none of these tools ever asks for input.
    assert isinstance(res, CallToolResult), f"{tool} asked for input: {res!r}"
    parts = [c.text for c in res.content if isinstance(c, TextContent)]
    return json.loads("\n".join(parts))


@pytest.fixture(autouse=True)
def _unreachable_server() -> None:
    """Point at a dead port: every gate must fire before any request."""
    S._OPTS.update(
        url="http://127.0.0.1:1", username="u", password="p", api_key=None, read_only=False
    )


def test_tool_surface_stays_small() -> None:
    tools = asyncio.run(S.server.list_tools())
    assert len(tools) <= CURATED_MAX, (
        f"{len(tools)} tools is too many; the point of the curated surface is "
        f"that it is not one tool per endpoint ({len(load_all())} of those)"
    )


def test_every_tool_has_a_description() -> None:
    for t in asyncio.run(S.server.list_tools()):
        assert t.description and len(t.description) > 20, f"{t.name} needs a description"


def test_qbt_call_can_reach_every_endpoint() -> None:
    """The escape hatch is what makes the small surface complete."""
    names = {t.name for t in asyncio.run(S.server.list_tools())}
    assert "qbt_call" in names
    assert "list_endpoints" in names, "a model needs to discover what qbt_call reaches"


def test_destructive_refusal_is_structured() -> None:
    out = call(
        "qbt_call",
        {"path": "torrents/delete", "params": {"hashes": "ab" * 20, "deleteFiles": True}},
    )
    assert out["refused"] == "confirmation_required"
    assert "confirm=true" in out["remedy"]


def test_lockout_keys_refused_through_qbt_call() -> None:
    """The hole the plan review found: gates must not be bypassable."""
    out = call(
        "qbt_call", {"path": "app/setPreferences", "params": {"json": {"web_ui_password": "x"}}}
    )
    assert out["refused"] == "blocked_parameter"
    assert "no override" in out["remedy"]


def test_lockout_keys_refused_through_the_curated_tool() -> None:
    out = call("preferences", {"action": "write", "values": {"web_ui_port": 1234}})
    assert out["refused"] == "blocked_parameter"


def test_read_only_refuses_mutations_both_ways() -> None:
    S._OPTS["read_only"] = True
    for tool, args in (
        ("control_torrents", {"action": "stop"}),
        ("qbt_call", {"path": "torrents/stop", "params": {"hashes": "all"}}),
    ):
        out = call(tool, args)
        assert out["refused"] == "read_only", f"{tool} was not refused"


def test_transport_failure_is_reported_not_raised() -> None:
    out = call("list_torrents")
    assert out.get("error") == "TransportError", out
    assert "127.0.0.1:1" in out["detail"]


def test_unknown_action_is_explained() -> None:
    out = call("control_torrents", {"action": "obliterate"})
    assert "error" in out
    assert "accepted" in out, "an unknown action should list the valid ones"


def test_swarm_health_excludes_unconnected_states() -> None:
    """The source of the real over-deletion bug, encoded in the tool itself."""
    import inspect

    src = inspect.getsource(S.swarm_health)
    for state in ("queuedDL", "metaDL", "checkingResumeData"):
        assert state in src, (
            f"swarm_health must exclude {state}: such torrents report "
            f"availability 0 for not being connected, not for being dead"
        )


class _FakeClient:
    """Stands in for a Client so verdict logic can be tested without a server."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def call(self, path: str, /, **_: object) -> list[dict]:
        assert path == "torrents/info", path
        return self._rows

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _row(**over: object) -> dict:
    row = {
        "name": "pack",
        "hash": "a" * 40,
        "state": "stoppedUP",
        "progress": 1.0,
        "size": 1000,
        "total_size": 1000,
        "completed": 1000,
        "availability": -1.0,
        "num_seeds": 0,
        "num_complete": 3,
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    ("size", "partial"),
    [(1000, False), (570, True)],
    ids=["all files selected", "some files deselected"],
)
def test_swarm_health_flags_a_partial_selection(
    monkeypatch: pytest.MonkeyPatch, size: int, partial: bool
) -> None:
    """progress 1.0 must not read as "every file is on disk".

    A live season pack reported progress 1.0 while holding 57% of its pieces,
    because four episodes were set to priority 0. Anything deciding "the content
    is all here, the torrent is safe to delete" from progress alone deletes data
    it never had, so the verdict says which of the two it is.
    """
    monkeypatch.setattr(S, "_client", lambda: _FakeClient([_row(size=size, total_size=1000)]))
    out = call("swarm_health", {"include_complete": True})
    (t,) = out["torrents"]
    assert t["partial_selection"] is partial
    assert ("deselected" in t["verdict"]) is partial, t["verdict"]


class _CapturingClient:
    """Records the kwargs a tool sends, so defaults can be asserted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def call(self, path: str, /, **kw: object) -> dict:
        self.calls.append((path, kw))
        return {"ok": True}

    def __enter__(self) -> _CapturingClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.parametrize(
    ("args", "expected_tmm"),
    [
        ({"category": "tv"}, True),
        ({"category": "tv", "save_path": "/elsewhere"}, False),
        ({}, False),
        ({"category": "tv", "auto_tmm": False}, False),
    ],
    ids=["category alone", "category plus explicit path", "neither", "explicit override wins"],
)
def test_add_torrent_honours_the_category_save_path(
    monkeypatch: pytest.MonkeyPatch, args: dict, expected_tmm: bool
) -> None:
    """A category implies its save path, because that is why categories exist.

    Without automatic management qBittorrent files the torrent under the global
    save path and ignores the category's, so `category="tv"` alone silently
    wrote a 55 GB download to the global save path instead of the category's.
    Asking for a category and getting the wrong directory is never what anyone
    meant, so a category with no explicit path turns management on.
    """
    cap = _CapturingClient()
    monkeypatch.setattr(S, "_client", lambda: cap)
    call("add_torrent", {"urls": ["magnet:?xt=urn:btih:" + "a" * 40], **args})
    (path, kw) = cap.calls[0]
    assert path == "torrents/add"
    assert kw["autoTMM"] is expected_tmm


_SECRET = "s3cr3t-api-key-value"


@pytest.mark.parametrize(
    "tool,args",
    [
        ("preferences", {"action": "read"}),
        ("qbt_call", {"path": "app/preferences"}),
    ],
    ids=["preferences tool", "raw qbt_call"],
)
def test_reading_preferences_never_returns_credentials(
    monkeypatch: pytest.MonkeyPatch, tool: str, args: dict
) -> None:
    """qBittorrent returns its own API key and passwords in app/preferences.

    `web_ui_api_key` is stored in plaintext and handed back by that call, along
    with proxy, dyndns and mail passwords. Returning the object verbatim writes
    the server's own credential into the model's context and into every
    transcript and log that context reaches — the opposite of the promise that
    credentials are never echoed. Both the curated tool and the raw escape
    hatch have to redact, or the gate is one `qbt_call` away from pointless.
    """

    class _Prefs:
        def call(self, path: str, /, **_: object) -> dict:
            return {
                "web_ui_api_key": _SECRET,
                "proxy_password": _SECRET,
                "dyndns_password": _SECRET,
                "web_ui_username": "admin",
                "dl_limit": 0,
            }

        def __enter__(self) -> _Prefs:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(S, "_client", lambda: _Prefs())
    raw = asyncio.run(S.server.call_tool(tool, args))
    # call_tool is typed CallToolResult | InputRequiredResult; only the former
    # carries content, and none of these tools ever asks for input.
    assert isinstance(raw, CallToolResult), f"{tool} asked for input: {raw!r}"
    text = "\n".join(c.text for c in raw.content if isinstance(c, TextContent))
    assert _SECRET not in text, f"{tool} leaked a credential: {text[:300]}"
    # Non-secret values must survive, or redaction has eaten the answer.
    assert "admin" in text and "dl_limit" in text


def _raising(exc: Exception):
    def _refuse() -> object:
        raise exc

    return _refuse


def test_main_starts_when_qbittorrent_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead qBittorrent must not stop the server from starting.

    An MCP client launches its servers when the session starts, which for a
    desktop qBittorrent is routinely before the application is running. Exiting
    here strips every tool from the model's list and the client reports only
    "connection closed", so the one fact worth knowing — that qBittorrent is
    down — never reaches the user.
    """
    monkeypatch.setattr("sys.argv", ["qbt-mcp"])

    monkeypatch.setattr(S, "_client", _raising(errors.TransportError("connection refused")))
    ran: list[str] = []
    monkeypatch.setattr(S.server, "run", lambda transport: ran.append(transport))

    S.main()

    out, err = capsys.readouterr()
    assert ran == ["stdio"], "the server must still serve its tools"
    assert "connection refused" in err
    # stdout carries the JSON-RPC framing; anything else there corrupts it.
    assert out == ""


def test_main_keeps_serving_while_banned(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ban expires on its own, so it is transient like a stopped server."""
    monkeypatch.setattr("sys.argv", ["qbt-mcp"])
    monkeypatch.setattr(S, "_client", _raising(errors.BannedError("banned for 3600s")))
    ran: list[str] = []
    monkeypatch.setattr(S.server, "run", lambda transport: ran.append(transport))

    S.main()

    assert ran == ["stdio"]


def test_main_exits_on_rejected_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad credentials must stay fatal.

    They never fix themselves while the process runs, and every tool call
    builds a fresh client that re-POSTs auth/login. qBittorrent bans an IP for
    repeated failed logins, so staying up would lock the user out of their own
    WebUI — a worse outcome than exiting.
    """
    monkeypatch.setattr("sys.argv", ["qbt-mcp"])
    monkeypatch.setattr(S, "_client", _raising(errors.AuthError("rejected")))
    monkeypatch.setattr(S.server, "run", lambda transport: pytest.fail("must not serve"))

    with pytest.raises(SystemExit, match="rejected"):
        S.main()
