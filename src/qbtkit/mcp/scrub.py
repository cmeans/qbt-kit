"""Strip credentials out of everything on its way to a model.

A private-tracker passkey is an account credential: whoever holds it can act as
that account. It does not live in a field called `password`, which is what the
old key-name redaction looked for. It lives *inside a URL* — in a path segment
of an announce URL, in a `passkey=` query parameter, in a `tr=` value inside a
magnet — so a gate that matches key names walks straight past it.

Once a secret is in tool output it is in the model's context, and from there in
transcripts, client logs and whatever the provider retains. No later instruction
recalls it. Worse, this codebase already assumes the model can be steered by
text it reads — a torrent name, an RSS title — so an injected instruction could
read a passkey out of one tool's reply and post it through another.

Two deliberate limits, because both cost something real:

Redaction here is one-way. A redacted announce URL cannot be handed back to
`torrents/editTracker`, and a redacted `fileUrl` cannot be handed back to
`search/downloadTorrent`. A reversible token would restore that, but a token
standing for part of a URL lets the model build a new URL around it and
exfiltrate the secret through `torrents/addTrackers`, which is worse than the
leak it fixes. So: tracker edits on a private tracker are a CLI job, and the
marker left behind says what was removed rather than pretending nothing was.

There is no entropy heuristic. "This segment looks random" cannot tell a passkey
from a 40-hex infohash, and public download URLs are routinely
`/download/<infohash>/name.torrent` — needed, public, and indistinguishable. A
path segment is only removed when a *sibling* segment names a credential, which
is how real trackers spell it: `/passkey/<x>/announce`, `/rss/<x>`, `/auth/<x>`.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

MARKER = "<redacted-by-qbt-kit>"

# Query parameters and path-sibling words that name a credential. Kept narrow on
# purpose: every entry here can erase data the user needs to see.
CREDENTIAL_WORDS = frozenset(
    {
        "passkey",
        "passphrase",
        "torrent_pass",
        "authkey",
        "auth",
        "rsskey",
        "apikey",
        "api_key",
        "secret",
        "token",
        "access_token",
        "sid",
        "pid",
        "key",
        "cookie",
        "session",
        "password",
        "pwd",
        "credential",
    }
)

# Substrings that mark a key's value as a credential whatever shape it takes.
# Matched as substrings, not exact names, because qBittorrent hands back
# `web_ui_password`, `web_ui_api_key` and friends from app/preferences in
# plaintext and an exact-name list would miss every prefixed spelling.
SECRET_KEY_HINTS = (
    "password",
    "api_key",
    "apikey",
    "secret",
    "token",
    "passkey",
    "ssl_private_key",
    "ssl_dh_params",
)

_URL_IN_TEXT = re.compile(r"\b(?:magnet:\?|(?:https?|udp|wss?)://)[^\s\"'<>,)\]]+", re.I)
# `passkey=abc`, `passkey: abc`, `authkey abc` inside free prose.
_PAIR_IN_TEXT = re.compile(
    r"\b(" + "|".join(sorted(CREDENTIAL_WORDS)) + r")\b\s*[=:]\s*([^\s&\"';,)\]]{6,})",
    re.I,
)
_MAGNET_URL_KEYS = ("tr", "xs", "as", "ws")


def _is_announce(parts: urllib.parse.SplitResult) -> bool:
    """Announce and scrape URLs exist only to carry the passkey, so nothing in
    the path is worth keeping. UDP trackers are always announce endpoints."""
    if parts.scheme.lower() == "udp":
        return True
    low = parts.path.lower()
    return "announce" in low or "scrape" in low


def _strip_userinfo(netloc: str) -> str:
    """Drop `user:pass@`. Keep the host, which is the part a user reads to tell
    which tracker a torrent belongs to."""
    return netloc.rsplit("@", 1)[-1] if "@" in netloc else netloc


def _clean_query(query: str) -> str:
    if not query:
        return query
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return query
    out = [(k, MARKER if k.lower() in CREDENTIAL_WORDS and v else v) for k, v in pairs]
    return urllib.parse.urlencode(out)


def _clean_path(path: str) -> str:
    """Remove a segment whose neighbour names a credential.

    `/passkey/<x>/announce` and `/rss/<x>` are how trackers actually spell it.
    Judging a segment on its own content is what mangles public download URLs.
    """
    segs = path.split("/")
    out = list(segs)
    for i, seg in enumerate(segs):
        if seg.lower().strip("._-") not in CREDENTIAL_WORDS:
            continue
        if i + 1 < len(segs) and segs[i + 1]:
            out[i + 1] = MARKER
    return "/".join(out)


def scrub_url(url: str) -> str:
    if url.lower().startswith("magnet:"):
        return scrub_magnet(url)
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme or not parts.netloc:
        return url
    netloc = _strip_userinfo(parts.netloc)
    if _is_announce(parts):
        # Host and port kept so the tracker is still identifiable; everything
        # that could carry the credential goes.
        return f"{parts.scheme}://{netloc}/{MARKER}"
    return urllib.parse.urlunsplit(
        (parts.scheme, netloc, _clean_path(parts.path), _clean_query(parts.query), "")
    )


def scrub_magnet(magnet: str) -> str:
    """Scrub only the URL-bearing keys. `xt` is the infohash, `dn` the display
    name and `xl` the length: all needed, none secret."""
    head, _, query = magnet.partition("?")
    if not query:
        return magnet
    pairs = urllib.parse.parse_qsl(query, keep_blank_values=True)
    if not pairs:
        return magnet
    out = [(k, scrub_url(v) if k.lower() in _MAGNET_URL_KEYS and v else v) for k, v in pairs]
    return f"{head}?{urllib.parse.urlencode(out)}"


def scrub_text(text: str) -> str:
    """Scrub URLs and `passkey=`-style pairs embedded in prose.

    Deliberately does not touch anything else. Torrent names, `log/main`
    messages and tracker `msg` fields carry infohashes and release CRCs that the
    user needs in order to correlate what they are looking at.
    """
    out = _URL_IN_TEXT.sub(lambda m: scrub_url(m.group(0)), text)
    return _PAIR_IN_TEXT.sub(lambda m: f"{m.group(1)}={MARKER}", out)


# A secret short enough to collide with a word is not worth propagating.
MIN_SECRET_LEN = 8
_NOT_SECRETS = frozenset({"announce", "scrape", "announce.php", "scrape.php"})


def _harvest_url(url: str, found: set[str]) -> None:
    if url.lower().startswith("magnet:"):
        _, _, query = url.partition("?")
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
            if k.lower() in _MAGNET_URL_KEYS and v:
                _harvest_url(v, found)
        return
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return
    if not parts.scheme or not parts.netloc:
        return

    def keep(s: str) -> None:
        if len(s) >= MIN_SECRET_LEN and s.lower() not in _NOT_SECRETS:
            found.add(s)

    if "@" in parts.netloc:
        userinfo = parts.netloc.rsplit("@", 1)[0]
        for piece in userinfo.split(":"):
            keep(piece)
    segs = parts.path.split("/")
    if _is_announce(parts):
        for seg in segs:
            keep(seg)
    for i, seg in enumerate(segs):
        if seg.lower().strip("._-") in CREDENTIAL_WORDS and i + 1 < len(segs):
            keep(segs[i + 1])
    for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        if k.lower() in CREDENTIAL_WORDS and v:
            keep(v)


def collect_secrets(value: Any) -> set[str]:
    """Learn the literal secrets in a reply from the places that prove it.

    A passkey in an announce URL is certainly a passkey. The same string in a
    `comment` field, or in a `details` URL with nothing naming it, is
    indistinguishable from an infohash by shape alone — which is why there is no
    entropy rule here. But once the announce URL has identified the literal,
    every other copy of it in the same reply can go too. That is how the
    reporter's own `comment` case is covered without guessing.
    """
    found: set[str] = set()

    def visit(v: Any) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if isinstance(k, str):
                    for url in _URL_IN_TEXT.findall(k):
                        _harvest_url(url, found)
                visit(x)
        elif isinstance(v, list):
            for x in v:
                visit(x)
        elif isinstance(v, str):
            for url in _URL_IN_TEXT.findall(v):
                _harvest_url(url, found)

    visit(value)
    return found


def scrub(value: Any) -> Any:
    """Scrub a decoded reply: structural rules first, then learned literals."""
    return _walk(value, collect_secrets(value))


def _redact_known(text: str, secrets: set[str]) -> str:
    for secret in secrets:
        if secret in text:
            text = text.replace(secret, MARKER)
    return text


def _walk(value: Any, secrets: set[str]) -> Any:
    """Walk a decoded reply, scrubbing keys and string leaves.

    Done on the object rather than the serialised JSON for two reasons: dict
    keys are not reachable with a regex over the text, and `json.dumps` escapes
    non-ASCII to `\\uXXXX`, so one such character in a URL defeats a URL pattern.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for k, v in value.items():
            is_url_key = isinstance(k, str) and bool(_URL_IN_TEXT.match(k))
            # The name test runs only on keys that are names. A tracker URL used
            # as a key contains the word "passkey" often enough that testing it
            # by substring redacted the value and left the URL — the opposite of
            # what was wanted, and how this was caught.
            if (
                not is_url_key
                and isinstance(k, str)
                and v
                # Empty values are left alone so a caller can still tell whether
                # a key is set at all, which is usually the real question.
                and any(h in k.lower() for h in SECRET_KEY_HINTS)
            ):
                out[k] = MARKER
                continue
            new_key = scrub_url(k) if is_url_key else k
            if isinstance(new_key, str):
                new_key = _redact_known(new_key, secrets)
            new_val = _walk(v, secrets)
            if new_key in out:
                # Two URLs differing only by passkey collapse to the same host.
                # Merging beats overwriting: dropping one would hide a tracker
                # the user has, and the lists here are per-tracker torrent ids.
                existing = out[new_key]
                if isinstance(existing, list) and isinstance(new_val, list):
                    out[new_key] = existing + [x for x in new_val if x not in existing]
                    continue
                if isinstance(existing, dict) and isinstance(new_val, dict):
                    out[new_key] = {**existing, **new_val}
                    continue
            out[new_key] = new_val
        return out
    if isinstance(value, list):
        return [_walk(v, secrets) for v in value]
    if isinstance(value, str):
        return _redact_known(scrub_text(value), secrets)
    return value
