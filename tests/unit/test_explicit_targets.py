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
            if "hashes" not in kw:
                return [{"hash": h} for h in self.known]
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
    assert out["refused"] == "every_torrent_needs_confirm"


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
    assert out["refused"] == "every_torrent_needs_confirm"


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
    assert out["refused"] == "every_torrent_needs_confirm"
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


# --------------------------------------------------------------- long hash lists


@pytest.mark.parametrize("count", [501, 2000, 20000])
def test_a_long_list_is_looked_up_without_a_url_full_of_hashes(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """The lookup is a GET: about 2,000 hashes made a URL httpx refuses to build."""
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    many = [HASH] + [f"{i:040x}" for i in range(count - 1)]
    out = call("control_torrents", {"action": "stop", "hashes": many})
    assert out["applied_to"] == 1
    assert len(out["not_found"]) == count - 1
    assert "hashes" not in fake.sent("torrents/info")[0]
    assert fake.sent("torrents/stop")[0]["hashes"] == many


def test_a_short_list_is_still_filtered_by_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    many = [HASH] + [f"{i:040x}" for i in range(499)]
    call("control_torrents", {"action": "stop", "hashes": many})
    assert fake.sent("torrents/info")[0]["hashes"] == many


def test_the_limit_counts_what_goes_in_the_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeats don't shrink the URL, so they have to count."""
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("control_torrents", {"action": "stop", "hashes": [HASH] * 2000})
    assert out["applied_to"] == 1
    assert "hashes" not in fake.sent("torrents/info")[0]


@pytest.mark.parametrize("method", ["_get", "_post"])
def test_a_url_too_long_to_build_is_a_transport_error(method: str) -> None:
    """Not an httpx exception, so nothing above the client would catch it."""
    url = "http://127.0.0.1:1/api/v2/torrents/info?hashes=" + "a" * 100_000
    with pytest.raises(errors.TransportError):
        getattr(_agent(), method)(url)


def test_no_hashes_never_encodes_as_every_torrent() -> None:
    """`_send` skips None today, and this was the dangerous default if it ever didn't."""
    with pytest.raises(TypeError):
        encode(Wire.HASHES, None)


# ----------------------------------------------------------------- moving


def test_moving_an_unknown_hash_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server()
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("move_torrents", {"hashes": [OTHER], "location": "/x"})
    assert out["refused"] == "unknown_torrents"
    assert not fake.sent("torrents/setLocation")


def test_moving_reports_what_it_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("move_torrents", {"hashes": [HASH, OTHER], "location": "/x"})
    assert out["moved"] == 1
    assert out["not_found"] == [OTHER]
    assert fake.sent("torrents/setLocation") == [
        {"confirm": False, "hashes": [HASH, OTHER], "location": "/x"}
    ]


@pytest.mark.parametrize(
    "args", [{"location": "/x"}, {"hashes": [], "location": "/x"}], ids=["omitted", "empty"]
)
def test_moving_nothing_is_refused(args: dict) -> None:
    if "hashes" not in args:
        with pytest.raises(ToolError, match="hashes"):
            raw("move_torrents", args)
        return
    assert call("move_torrents", args)["refused"] == "hashes_required"


def test_moving_every_torrent_needs_a_confirm_the_tool_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It used to say to re-send with confirm, and then reject confirm."""
    out = call("move_torrents", {"hashes": ["all"], "location": "/x"})
    assert out["refused"] == "every_torrent_needs_confirm"
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("move_torrents", {"hashes": ["all"], "location": "/x", "confirm": True})
    assert out["moved"] == "all"
    assert fake.sent("torrents/setLocation") == [
        {"confirm": True, "hashes": ["all"], "location": "/x"}
    ]


def test_moving_and_rechecking_every_torrent_confirms_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    args = {"hashes": ["all"], "location": "/x", "confirm": True}
    call("move_torrents", {**args, "recheck": True})
    assert fake.sent("torrents/recheck") == [{"confirm": True, "hashes": ["all"]}]


def test_moving_doesnt_recheck_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recheck rehashes everything, so on large data it's slow."""
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("move_torrents", {"hashes": [HASH], "location": "/x"})
    assert out["rechecking"] is False
    assert not fake.sent("torrents/recheck")


# ------------------------------------------------------- what actually gets sent


@pytest.mark.parametrize("delete_files", [False, True], ids=["keep files", "delete files"])
def test_deleting_sends_exactly_what_was_asked(
    monkeypatch: pytest.MonkeyPatch, delete_files: bool
) -> None:
    """A wrong flag here is data loss, and the refusal tests can't see it."""
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    call("delete_torrents", {"hashes": [HASH], "delete_files": delete_files, "confirm": True})
    assert fake.sent("torrents/delete") == [
        {"confirm": True, "hashes": [HASH], "deleteFiles": delete_files}
    ]


def test_deleting_keeps_the_files_unless_told(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("delete_torrents", {"hashes": [HASH], "confirm": True})
    assert out["files_deleted"] is False
    assert fake.sent("torrents/delete")[0]["deleteFiles"] is False


@pytest.mark.parametrize(
    ("action", "path"), [("add", "torrents/addTags"), ("remove", "torrents/removeTags")]
)
def test_tagging_sends_exactly_what_was_asked(
    monkeypatch: pytest.MonkeyPatch, action: str, path: str
) -> None:
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    call("tags", {"action": action, "names": ["x", "y"], "hashes": [HASH]})
    assert fake.sent(path) == [{"confirm": False, "hashes": [HASH], "tags": ["x", "y"]}]


# ---------------------------------------------------------------- blank tag names


@pytest.mark.parametrize("action", ["add", "remove"])
@pytest.mark.parametrize(
    "names",
    [[""], ["  "], ["x", ""], [","], [",,"], ["x,"], [",x"], ["x, "]],
    ids=["empty", "blank", "mixed", "comma", "commas", "comma after", "comma before", "gap"],
)
def test_a_blank_tag_name_is_refused(
    monkeypatch: pytest.MonkeyPatch, action: str, names: list[str]
) -> None:
    """Measured: removing "" took every tag off the torrent and reported ok."""
    fake = _Server(HASH)
    monkeypatch.setattr(S, "_client", lambda: fake)
    out = call("tags", {"action": action, "names": names, "hashes": [HASH]})
    assert out["refused"] == "tags_required"
    assert not [p for p, _ in fake.calls if p != "torrents/info"]


@pytest.mark.parametrize("path", ["torrents/addTags", "torrents/removeTags"])
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"tags": ""},
        {"tags": []},
        {"tags": ["x", " "]},
        {"tags": "x| "},
        {"tags": ","},
        {"tags": [","]},
        {"tags": [",,"]},
    ],
    ids=[
        "omitted",
        "empty",
        "empty list",
        "blank in list",
        "blank in string",
        "comma",
        "comma in list",
        "commas in list",
    ],
)
def test_qbt_call_can_not_name_every_tag_by_omission(path: str, params: dict) -> None:
    out = call("qbt_call", {"path": path, "params": {"hashes": [HASH], **params}})
    assert out["refused"] == "tags_required"


def test_a_person_at_the_cli_can_still_remove_every_tag() -> None:
    with pytest.raises(errors.TransportError):
        _person().call("torrents/removeTags", hashes=[HASH])


# ------------------------------------------------------------ what a refusal says


def test_the_every_torrent_refusal_points_at_naming_them() -> None:
    """The shortest way past this gate shouldn't be the first thing it suggests."""
    out = call("control_torrents", {"action": "stop", "hashes": ["all"]})
    assert out["remedy"].startswith("name the torrents")
    assert "ask the user" in out["remedy"]
    assert "cannot be undone" not in out["remedy"]


def test_a_destructive_refusal_keeps_its_own_wording() -> None:
    out = call("delete_torrents", {"hashes": [HASH]})
    assert out["refused"] == "confirmation_required"


@pytest.mark.parametrize("delete_files", [False, True], ids=["keep files", "delete files"])
def test_deleting_every_torrent_gets_the_stronger_refusal(delete_files: bool) -> None:
    """The worst call in the API shouldn't get the wording for one that can be undone."""
    args = {"hashes": ["all"], "delete_files": delete_files}
    out = call("delete_torrents", args)
    assert out["refused"] == "confirmation_required"
    assert "cannot be undone" in out["remedy"]


def test_qbt_call_deleting_every_torrent_gets_the_stronger_refusal() -> None:
    out = call("qbt_call", {"path": "torrents/delete", "params": {"hashes": "all"}})
    assert out["refused"] == "confirmation_required"


def test_a_rejected_argument_doesnt_echo_its_value() -> None:
    """Every other reply is scrubbed, and this error isn't."""
    secret = "http://t.invalid/announce?passkey=FAKEFAKEFAKE"
    with pytest.raises(ToolError) as e:
        raw("control_torrents", {"action": "stop", "hash": secret})
    assert "hash" in str(e.value)
    assert "FAKEFAKEFAKE" not in str(e.value)
    assert "Strict" not in str(e.value)
    assert "control_torrents" in str(e.value)
