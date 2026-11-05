"""A tool call has to name what it acts on, and a mistyped argument has to be an error.

Measured on a real 5.1.4: `control_torrents(action="stop")` stopped every torrent,
and so did `control_torrents(action="stop", hash=[one])`, because the mistyped
argument was dropped and a missing `hashes` meant all. `hashes=[]` reported
`applied_to: all` while doing nothing, and deleting an unknown hash reported
`removed: 1`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from qbtkit import errors
from qbtkit.client import Client, encode
from qbtkit.mcp import server as S
from qbtkit.spec._types import Wire

HASH = "ab" * 20
OTHER = "cd" * 20


def raw(tool: str, args: dict) -> CallToolResult:
    res = asyncio.run(S.server.call_tool(tool, args))
    assert isinstance(res, CallToolResult), f"{tool} asked for input: {res!r}"
    return res


def text_of(res: CallToolResult) -> str:
    return "\n".join(c.text for c in res.content if isinstance(c, TextContent))


def call(tool: str, args: dict) -> dict:
    return json.loads(text_of(raw(tool, args)))


@pytest.fixture(autouse=True)
def _unreachable_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point at a dead port: every gate must fire before any request."""
    monkeypatch.setitem(S._OPTS, "url", "http://127.0.0.1:1")
    monkeypatch.setitem(S._OPTS, "username", "u")
    monkeypatch.setitem(S._OPTS, "password", "p")
    monkeypatch.setitem(S._OPTS, "api_key", None)
    monkeypatch.setitem(S._OPTS, "read_only", False)


def _agent() -> Client:
    return Client(
        "http://127.0.0.1:1", username="u", password="p", agent=True, use_keychain=False
    )


def _person() -> Client:
    return Client("http://127.0.0.1:1", username="u", password="p", use_keychain=False)


# ------------------------------------------------------------------ the gate


@pytest.mark.parametrize("value", ["all", ["all"]], ids=["string", "list"])
@pytest.mark.parametrize(
    "path", ["torrents/recheck", "torrents/stop", "torrents/topPrio", "torrents/reannounce"]
)
def test_every_torrent_needs_confirm_from_a_model(path: str, value: object) -> None:
    with pytest.raises(errors.ConfirmationRequired, match="every torrent"):
        _agent().call(path, hashes=value)


@pytest.mark.parametrize("value", ["all", ["all"]], ids=["string", "list"])
def test_confirm_gets_past_the_every_torrent_gate(value: object) -> None:
    with pytest.raises(errors.TransportError):
        _agent().call("torrents/recheck", hashes=value, confirm=True)


def test_the_gate_covers_the_other_spelling_of_hashes() -> None:
    """setSavePath calls its hashes `id`."""
    with pytest.raises(errors.ConfirmationRequired):
        _agent().call("torrents/setSavePath", id="all", path="/x", confirm=False)


@pytest.mark.parametrize("value", [[], "", "   "], ids=["empty list", "empty", "blank"])
@pytest.mark.parametrize("confirm", [False, True], ids=["unconfirmed", "confirmed"])
def test_naming_nothing_is_refused(value: object, confirm: bool) -> None:
    with pytest.raises(errors.MissingTargets):
        _agent().call("torrents/recheck", hashes=value, confirm=confirm)


@pytest.mark.parametrize("value", [["all", HASH], f"all|{HASH}"], ids=["list", "string"])
def test_all_mixed_with_a_hash_is_refused(value: object) -> None:
    """Sent as `all|<hash>`, and it isn't clear what was meant."""
    with pytest.raises(errors.MissingTargets, match="all"):
        _agent().call("torrents/recheck", hashes=value, confirm=True)


def test_leaving_out_a_required_hashes_is_the_same_refusal() -> None:
    with pytest.raises(errors.MissingTargets):
        _agent().call("torrents/recheck")


def test_a_read_doesnt_need_confirm() -> None:
    with pytest.raises(errors.TransportError):
        _agent().call("torrents/downloadLimit", hashes="all")
    with pytest.raises(errors.TransportError):
        _agent().call("torrents/info", hashes="all")


def test_read_only_still_answers_first() -> None:
    c = Client(
        "http://127.0.0.1:1",
        username="u",
        password="p",
        agent=True,
        read_only=True,
        use_keychain=False,
    )
    with pytest.raises(errors.ReadOnly):
        c.call("torrents/recheck", hashes=[])


def test_a_person_at_the_cli_keeps_all() -> None:
    with pytest.raises(errors.TransportError):
        _person().call("torrents/recheck", hashes="all")


def test_preflight_runs_the_gates_without_a_request() -> None:
    with pytest.raises(errors.ConfirmationRequired):
        _agent().preflight("torrents/recheck", hashes="all")
    _agent().preflight("torrents/recheck", hashes=[HASH])


def test_a_hash_holding_the_separator_is_refused() -> None:
    """Otherwise one value silently becomes two torrents."""
    with pytest.raises(errors.QbtError, match="separator"):
        encode(Wire.HASHES, [f"{HASH}|{OTHER}"])


def test_a_string_of_hashes_still_works() -> None:
    assert encode(Wire.HASHES, f"{HASH}|{OTHER}") == f"{HASH}|{OTHER}"


# ----------------------------------------------------------------- the tools


class _Server:
    """Answers torrents/info for the hashes it holds, as a real server does."""

    def __init__(self, *known: str) -> None:
        self.known = known
        self.calls: list[tuple[str, dict]] = []

    def preflight(self, path: str, /, *, confirm: bool = False, **params: object) -> None:
        _agent().preflight(path, confirm=confirm, **params)

    def call(self, path: str, /, *, confirm: bool = False, **kw: Any) -> object:
        self.calls.append((path, {"confirm": confirm, **kw}))
        if path == "torrents/info":
            wanted = {str(h).lower() for h in kw["hashes"]}
            return [{"hash": h} for h in self.known if h in wanted]
        return None

    def sent(self, path: str) -> list[dict]:
        return [kw for p, kw in self.calls if p == path]

    def __enter__(self) -> _Server:
        return self

    def __exit__(self, *_: object) -> None:
        return None


@pytest.mark.parametrize(
    "args",
    [{"action": "recheck"}, {"action": "recheck", "hashes": []}],
    ids=["omitted", "empty"],
)
def test_control_refuses_when_nothing_is_named(args: dict) -> None:
    assert call("control_torrents", args)["refused"] == "hashes_required"


def test_control_of_every_torrent_needs_confirm() -> None:
    out = call("control_torrents", {"action": "recheck", "hashes": ["all"]})
    assert out["refused"] == "confirmation_required"


def test_control_reports_all_when_all_was_confirmed(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("control_torrents", {"action": "recheck", "hashes": ["all"], "confirm": True})
    assert out["ok"] is True
    assert out["applied_to"] == "all"
    assert not fake.sent("torrents/info"), "no need to fetch the whole library"
    assert fake.sent("torrents/recheck")[0]["confirm"] is True


def test_control_reports_what_it_really_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("control_torrents", {"action": "stop", "hashes": [HASH, OTHER]})
    assert out["applied_to"] == 1
    assert out["not_found"] == [OTHER]
    assert fake.sent("torrents/stop")[0]["hashes"] == [HASH, OTHER]


def test_control_says_so_when_it_found_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server()
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("control_torrents", {"action": "stop", "hashes": [OTHER]})
    assert out["refused"] == "unknown_torrents"
    assert OTHER in out["detail"]
    assert not fake.sent("torrents/stop")


class _IgnoresTheFilter(_Server):
    """Measured on 4.4.5: torrents/info answers an unparsable hash with every torrent."""

    def call(self, path: str, /, *, confirm: bool = False, **kw: Any) -> object:
        if path == "torrents/info":
            return [{"hash": h} for h in self.known]
        return super().call(path, confirm=confirm, **kw)


def test_a_server_ignoring_the_filter_doesnt_fake_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _IgnoresTheFilter(HASH, OTHER)
    monkeypatch.setattr(S, "_client", lambda: fake)
    unknown = "0" * 40
    out = call("delete_torrents", {"hashes": [unknown], "confirm": True})
    assert out["refused"] == "unknown_torrents"
    assert not fake.sent("torrents/delete")


def test_a_server_ignoring_the_filter_doesnt_inflate_the_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _IgnoresTheFilter(HASH, OTHER)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("control_torrents", {"action": "stop", "hashes": [HASH]})
    assert out["applied_to"] == 1
    assert out["not_found"] == []


def test_a_capital_hash_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("control_torrents", {"action": "stop", "hashes": [HASH.upper()]})
    assert out["applied_to"] == 1
    assert out["not_found"] == []


def test_a_v2_torrent_is_found_by_its_full_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Its `hash` is the truncated id, so the hash the model holds isn't in that field."""
    full = "ef" * 32

    class V2(_Server):
        def call(self, path: str, /, *, confirm: bool = False, **kw: Any) -> object:
            if path == "torrents/info":
                return [{"hash": full[:40], "infohash_v2": full}]
            return super().call(path, confirm=confirm, **kw)

    monkeypatch.setattr(S, "_client", lambda: V2())
    out = call("control_torrents", {"action": "stop", "hashes": [full]})
    assert out["applied_to"] == 1
    assert out["not_found"] == []


def test_control_passes_its_extra_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    call("control_torrents", {"action": "force_start", "hashes": [HASH]})
    assert fake.sent("torrents/setForceStart")[0]["value"] is True


@pytest.mark.parametrize("action", ["add", "remove"])
def test_tagging_refuses_when_no_torrent_is_named(action: str) -> None:
    out = call("tags", {"action": action, "names": ["x"]})
    assert out["refused"] == "hashes_required"


@pytest.mark.parametrize("action", ["add", "remove"])
def test_tagging_every_torrent_needs_confirm(action: str) -> None:
    out = call("tags", {"action": action, "names": ["x"], "hashes": ["all"]})
    assert out["refused"] == "confirmation_required"


def test_tagging_every_torrent_with_confirm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("tags", {"action": "add", "names": ["x"], "hashes": ["all"], "confirm": True})
    assert out["applied_to"] == "all"
    assert fake.sent("torrents/addTags")[0]["confirm"] is True


@pytest.mark.parametrize(
    ("action", "path"), [("add", "torrents/addTags"), ("remove", "torrents/removeTags")]
)
def test_tagging_reports_what_it_found(
    monkeypatch: pytest.MonkeyPatch, action: str, path: str
) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("tags", {"action": action, "names": ["x"], "hashes": [HASH, OTHER]})
    assert out["applied_to"] == 1
    assert out["not_found"] == [OTHER]
    assert fake.sent(path)


def test_creating_a_tag_needs_no_torrent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server()
    monkeypatch.setattr(S, "_client", lambda: fake)
    assert call("tags", {"action": "create", "names": ["x"]})["ok"] is True


def test_deleting_an_unknown_hash_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server()
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("delete_torrents", {"hashes": [OTHER], "confirm": True})
    assert out["refused"] == "unknown_torrents"
    assert not fake.sent("torrents/delete")


def test_deleting_reports_what_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("delete_torrents", {"hashes": [HASH, OTHER], "confirm": True})
    assert out["removed"] == 1
    assert out["not_found"] == [OTHER]


def test_deleting_all_doesnt_claim_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("delete_torrents", {"hashes": ["all"], "confirm": True})
    assert out["removed"] == "all"


def test_deleting_nothing_is_refused() -> None:
    out = call("delete_torrents", {"hashes": [], "confirm": True})
    assert out["refused"] == "hashes_required"


def test_deleting_still_needs_confirm_first() -> None:
    """With the server down: the gate fires before the lookup."""
    out = call("delete_torrents", {"hashes": [HASH]})
    assert out["refused"] == "confirmation_required"


def test_qbt_call_is_gated_too() -> None:
    out = call("qbt_call", {"path": "torrents/recheck", "params": {"hashes": "all"}})
    assert out["refused"] == "confirmation_required"
    out = call("qbt_call", {"path": "torrents/recheck", "params": {"hashes": []}})
    assert out["refused"] == "hashes_required"
    out = call("qbt_call", {"path": "torrents/recheck"})
    assert out["refused"] == "hashes_required"


# ------------------------------------------------------- arguments and actions


@pytest.mark.parametrize(
    ("tool", "args", "typo"),
    [
        ("control_torrents", {"action": "stop", "hash": [HASH]}, "hash"),
        ("list_torrents", {"serach": "x"}, "serach"),
        (
            "delete_torrents",
            {"hashes": [HASH], "confirm": True, "delete_file": True},
            "delete_file",
        ),
        ("qbt_call", {"path": "torrents/info", "param": {}}, "param"),
    ],
    ids=["control", "list", "delete", "qbt_call"],
)
def test_an_unknown_argument_is_an_error(tool: str, args: dict, typo: str) -> None:
    """The typo must reach the model, not be dropped to change the call's meaning."""
    with pytest.raises(ToolError, match=typo):
        raw(tool, args)


def test_every_tool_is_strict() -> None:
    """A new tool gets this from registration, and advertises it to clients."""
    tools = asyncio.run(S.server.list_tools())
    assert tools
    for t in tools:
        assert t.input_schema.get("additionalProperties") is False, t.name


@pytest.mark.parametrize("action", ["writ", "wr", "", "READ"])
def test_preferences_refuses_an_unknown_action(action: str) -> None:
    """`writ` used to fall through and write."""
    out = call("preferences", {"action": action, "values": {"dl_limit": 1}})
    assert "unknown action" in out["error"]
    assert out["accepted"] == ["read", "write"]


def test_editing_a_category_needs_a_save_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leaving it out used to clear the category's save path."""
    fake = _Server()
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("categories", {"action": "edit", "name": "tv"})
    assert "save_path" in out["error"]
    assert not fake.sent("torrents/editCategory")


def test_editing_a_category_to_an_empty_path_stays_possible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Server()
    monkeypatch.setattr(S, "_client", lambda: fake)
    assert call("categories", {"action": "edit", "name": "tv", "save_path": ""})["ok"] is True
    assert fake.sent("torrents/editCategory")[0]["savePath"] == ""
