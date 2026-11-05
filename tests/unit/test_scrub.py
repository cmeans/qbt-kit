"""Credential scrubbing on the way to the model.

An external report against 0.1.2 found that the key-name redaction it replaces
protected qBittorrent's own credentials well and never reached a private-tracker
passkey, because a passkey lives inside a URL rather than in a field called
`password`. The reporter's own reproduction is `test_reporters_reproduction`.

The negative tests matter as much as the positive ones. Over-scrubbing destroys
the infohashes and tracker hostnames the user needs to tell one torrent from
another, and a tool that mangles ordinary output gets switched off.
"""

from __future__ import annotations

import json

from qbtkit.mcp.scrub import MARKER, scrub, scrub_magnet, scrub_text, scrub_url

PASSKEY = "FAKEPATHPASSKEY0000000000000000b"
INFOHASH = "ab" * 20


# --------------------------------------------------------------------------- #
# What must be removed


def test_announce_url_keeps_only_the_tracker_host() -> None:
    out = scrub_url(f"https://tracker.invalid:8443/{PASSKEY}/announce")
    assert PASSKEY not in out
    assert "tracker.invalid:8443" in out, "the host is how a user identifies the tracker"
    assert MARKER in out, "a reader should see that something was removed"


def test_udp_announce_is_treated_as_announce_without_the_word() -> None:
    out = scrub_url(f"udp://tracker.invalid:6969/{PASSKEY}")
    assert PASSKEY not in out
    assert "tracker.invalid:6969" in out


def test_scrape_urls_too() -> None:
    assert PASSKEY not in scrub_url(f"https://tracker.invalid/{PASSKEY}/scrape")


def test_credential_query_parameters() -> None:
    for key in ("passkey", "authkey", "torrent_pass", "apikey", "rsskey", "token"):
        out = scrub_url(f"https://tracker.invalid/rss.php?{key}={PASSKEY}&cat=tv")
        assert PASSKEY not in out, key
        assert "cat=tv" in out, "non-credential parameters are needed"


def test_credential_path_sibling() -> None:
    """How real trackers spell it: the segment after the word is the secret."""
    for word in ("passkey", "auth", "authkey", "torrent_pass", "secret"):
        out = scrub_url(f"https://tracker.invalid/{word}/{PASSKEY}/feed")
        assert PASSKEY not in out, word


def test_rss_is_not_treated_as_a_credential_path_marker() -> None:
    """`/rss/` is far too common on public sites to mean "a secret follows", so
    it stays out of the path words and keeps its place as a query name
    (`rsskey=`). A passkey in an RSS path is still caught in practice, by the
    learned-literal pass, because the same string appears in an announce URL."""
    url = "https://example.invalid/rss/feed.xml"
    assert scrub_url(url) == url


def test_userinfo_is_dropped() -> None:
    out = scrub_url("https://user:hunter2@tracker.invalid/feed.xml")
    assert "hunter2" not in out
    assert "tracker.invalid" in out


def test_magnet_scrubs_tracker_values_and_keeps_identity() -> None:
    tr = f"http://tracker.invalid/{PASSKEY}/announce"
    magnet = (
        f"magnet:?xt=urn:btih:{INFOHASH}&dn=Some.Release.2026&xl=1234"
        f"&tr={tr.replace(':', '%3A').replace('/', '%2F')}"
    )
    out = scrub_magnet(magnet)
    assert PASSKEY not in out
    assert INFOHASH in out, "the infohash is the torrent's identity, not a secret"
    assert "Some.Release.2026" in out
    assert "1234" in out


def test_passkey_inside_free_text() -> None:
    out = scrub_text(f"tracker said: passkey={PASSKEY} is invalid")
    assert PASSKEY not in out


def test_url_inside_free_text() -> None:
    """`log/main` messages and tracker `msg` fields carry announce URLs."""
    out = scrub_text(f"announce to http://tracker.invalid/{PASSKEY}/announce failed")
    assert PASSKEY not in out


def test_secret_key_names_are_still_redacted_by_substring() -> None:
    """The behaviour the old _redact had, kept: an exact-name list would miss
    every prefixed spelling qBittorrent actually returns."""
    out = scrub({"web_ui_password": "s", "api_key": "s", "rss_token": "s", "dl_limit": 10})
    assert out["web_ui_password"] == MARKER
    assert out["api_key"] == MARKER
    assert out["rss_token"] == MARKER
    assert out["dl_limit"] == 10


def test_empty_secrets_stay_empty_so_a_caller_can_tell_it_is_unset() -> None:
    assert scrub({"api_key": ""})["api_key"] == ""


def test_dict_keys_are_scrubbed_not_just_values() -> None:
    """sync/maindata keys its trackers map by announce URL, which no regex over
    the serialised text would reach."""
    data = {"trackers": {f"http://tracker.invalid/{PASSKEY}/announce": [INFOHASH]}}
    out = scrub(data)
    assert PASSKEY not in json.dumps(out)
    assert list(out["trackers"].values()) == [[INFOHASH]]


def test_colliding_keys_merge_rather_than_overwrite() -> None:
    """Two announce URLs differing only by passkey reduce to the same host.
    Dropping one would hide a tracker the user has."""
    data = {
        f"http://tracker.invalid/{PASSKEY}/announce": ["a"],
        "http://tracker.invalid/OTHERKEY111111111111111111111111/announce": ["b"],
    }
    out = scrub(data)
    assert len(out) == 1
    assert sorted(next(iter(out.values()))) == ["a", "b"]


# --------------------------------------------------------------------------- #
# What must survive


def test_public_download_urls_are_untouched() -> None:
    """The shape an entropy heuristic would have destroyed. It is public, it is
    indistinguishable from a passkey by randomness alone, and
    search/downloadTorrent needs it."""
    url = f"https://eztvx.to/download/{INFOHASH}/Some.Release.2026.torrent"
    assert scrub_url(url) == url


def test_infohashes_and_release_names_in_text_survive() -> None:
    msg = f"{INFOHASH} Some.Release.2026.2160p-GROUP [CRC32: 1a2b3c4d]"
    assert scrub_text(msg) == msg


def test_ordinary_http_urls_survive() -> None:
    for url in (
        "https://github.com/qbittorrent/qBittorrent/issues/1234",
        "https://tracker.invalid/rss.php?cat=tv&page=2",
    ):
        assert scrub_url(url) == url


def test_non_string_leaves_are_left_alone() -> None:
    data = {"size": 123, "progress": 0.5, "done": True, "missing": None}
    assert scrub(data) == data


# --------------------------------------------------------------------------- #
# The reporter's own case


def test_reporters_reproduction() -> None:
    """Report step 3: "The output contains FAKEPATHPASSKEY0000000000000000b."

    The magnet from the report, the reply torrent_detail would build from it, and
    the assertion that the passkey is gone while the torrent stays identifiable.
    """
    magnet = (
        f"magnet:?xt=urn:btih:{INFOHASH}&dn=fake"
        f"&tr=http%3A%2F%2Ftracker.invalid%2F{PASSKEY}%2Fannounce"
    )
    reply = {
        "name": "fake",
        "hash": INFOHASH,
        "magnet_uri": magnet,
        "tracker": f"http://tracker.invalid/{PASSKEY}/announce",
        "trackers": [{"url": f"http://tracker.invalid/{PASSKEY}/announce", "status": 2}],
        "properties": {"comment": f"get it at https://tracker.invalid/{PASSKEY}/details"},
    }
    out = json.dumps(scrub(reply))
    assert PASSKEY not in out
    assert INFOHASH in out
    assert "tracker.invalid" in out


# --------------------------------------------------------------------------- #
# The attach point


def test_every_registered_tool_scrubs_its_output() -> None:
    """The report found two call sites of the old redaction and thirteen tools
    that did not call it. Registration and scrubbing are one decorator now, so
    this asserts the property rather than trusting the convention.
    """
    from qbtkit.mcp import server as mcp

    registered = mcp.server._tool_manager._tools
    unscrubbed = [
        name
        for name, tool in registered.items()
        if not getattr(tool.fn, "__qbt_scrubbed__", False)
    ]
    assert not unscrubbed, (
        f"these tools were registered without the scrub, which means @server.tool "
        f"was used directly instead of @_tool: {unscrubbed}"
    )
    assert len(registered) >= 15, "tools vanished; the assertion above would pass vacuously"


def test_a_tool_returning_a_dict_fails_loudly() -> None:
    """MCP would serialise a non-string return downstream of the wrapper, where
    nothing scrubs it. Better a visible error than a quiet leak."""
    import pytest

    from qbtkit.mcp.server import _scrub_payload

    with pytest.raises(TypeError, match="JSON string"):
        _scrub_payload({"tracker": "http://t.invalid/SECRETPASSKEY00000/announce"})


def test_refusal_bodies_are_scrubbed_too() -> None:
    """ApiError carries the first 200 characters of the server's reply, which on
    a 409 can be an announce URL."""
    from qbtkit import errors
    from qbtkit.mcp.server import _safe

    @_safe
    def boom() -> str:
        raise errors.ApiError(
            409, "torrents/add", f"bad tracker http://t.invalid/{PASSKEY}/announce"
        )

    assert PASSKEY not in boom()
