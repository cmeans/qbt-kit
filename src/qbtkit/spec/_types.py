"""Spec primitives.

One declarative description per endpoint, from which the client method, the CLI
command, the MCP schema and the docs are all generated. Writing 123 endpoints
four times over is four chances to drift.

The review of the first plan made the sharp point that *generation* is easy and
the *primitives* are hard, because this API is irregular in specific ways:

  - bodies are urlencoded, multipart, or absent
  - `hashes` is pipe-joined with an `all` sentinel
  - booleans go on the wire as the strings "true"/"false"
  - some params carry JSON inside a form field (`setPreferences json=`)
  - returns are JSON, empty, plain text, *or binary* (`torrents/export`)
  - a successful `auth/login` is 204 on 5.2 but 200 `Ok.` before that
  - endpoints and parameters are added, renamed and removed across versions

So every one of those is expressible here rather than special-cased later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

Version = tuple[int, int, int]


# --------------------------------------------------------------------------- #
# How the request body is encoded


class Body(Enum):
    NONE = "none"
    URLENCODED = "urlencoded"
    MULTIPART = "multipart"  # torrents/add, torrents/parseMetadata, search/installPlugin


# --------------------------------------------------------------------------- #
# What comes back


class ReturnKind(Enum):
    EMPTY = "empty"
    JSON = "json"
    TEXT = "text"
    BINARY = "binary"  # torrents/export, torrentcreator/torrentFile
    TEXT_SENTINEL = "text_sentinel"  # 200 body decides success; see auth/login


def camel_to_kebab(name: str) -> str:
    """camelCase to kebab-case, treating an acronym as one word.

    A naive split on every capital produces `--auto-t-m-m` and
    `set-s-s-l-parameters`, which no one would guess and which made the
    documented examples wrong. Breaking only at a lower-to-upper boundary, or
    before the last capital of an acronym run, gives `--auto-tmm` and
    `set-ssl-parameters`.
    """
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i:
            prev_is_lower = name[i - 1].islower() or name[i - 1].isdigit()
            # The tail of an acronym run that starts a new word: the "P" in
            # "SSLParameters".
            ends_acronym = name[i - 1].isupper() and i + 1 < len(name) and name[i + 1].islower()
            if prev_is_lower or ends_acronym:
                out.append("-")
        out.append(ch.lower())
    return "".join(out).replace("_", "-")


@dataclass(frozen=True)
class Returns:
    kind: ReturnKind
    model: str | None = None  # reserved: response model name, once responses are typed
    success_body: str | None = None  # for TEXT_SENTINEL, e.g. "Ok."
    media_type: str | None = None  # for BINARY

    def __post_init__(self) -> None:
        if self.kind is ReturnKind.TEXT_SENTINEL and not self.success_body:
            raise ValueError("TEXT_SENTINEL needs success_body")
        if self.kind is ReturnKind.JSON and not self.model:
            raise ValueError("JSON returns need a model name")


NoContent = Returns(ReturnKind.EMPTY)
RawText = Returns(ReturnKind.TEXT)


def Json(model: str) -> Returns:
    return Returns(ReturnKind.JSON, model=model)


def Binary(media_type: str) -> Returns:
    return Returns(ReturnKind.BINARY, media_type=media_type)


# --------------------------------------------------------------------------- #
# How a parameter is written onto the wire


class Wire(Enum):
    PLAIN = "plain"  # str(value)
    BOOL_STR = "bool_str"  # True -> "true"
    PIPE_LIST = "pipe_list"  # ["a","b"] -> "a|b"
    HASHES = "hashes"  # pipe list, or the literal "all"
    JSON_BLOB = "json_blob"  # json.dumps(value) inside one form field
    FILE = "file"  # multipart file part
    INT = "int"
    FLOAT = "float"


@dataclass(frozen=True)
class Param:
    name: str
    wire: Wire = Wire.PLAIN
    required: bool = False
    default: Any = None
    summary: str = ""
    since: Version | None = None
    until: Version | None = None
    # Some params were renamed; compat.py holds the mapping and this flags it so
    # the generator knows to consult it.
    renamed: bool = False
    # Refuse these values outright, whatever the caller asks. Used for the
    # setPreferences keys that would sever the connection.
    blocklist: frozenset[str] = field(default_factory=frozenset)
    # Keys an agent may write. Anything outside is refused on the agent path and
    # passes with a warning on the CLI. An allowlist rather than a longer
    # blocklist because a blocklist has to anticipate every dangerous key, and
    # the two reports that prompted this found three it had not.
    allowlist: frozenset[str] = field(default_factory=frozenset)
    # Refused whenever this param is present on the agent path, at any value.
    agent_forbidden: bool = False

    def available_on(self, version: Version) -> bool:
        if self.since and version < self.since:
            return False
        return not (self.until and version >= self.until)


# --------------------------------------------------------------------------- #
# The endpoint itself


@dataclass(frozen=True)
class Endpoint:
    scope: str
    name: str
    method: str
    summary: str
    returns: Returns
    body: Body = Body.NONE
    params: tuple[Param, ...] = ()
    since: Version | None = None
    until: Version | None = None
    # Safety, enforced by the MCP layer and `qbt call` alike. The review found
    # that gating only the curated tools left the raw escape hatch wide open.
    mutating: bool = False
    destructive: bool = False
    # Never callable by a model, with no confirmation that permits it. For
    # endpoints whose whole output is a credential, or whose effect is code
    # execution on the qBittorrent host. See errors.AgentForbidden for why
    # `confirm` is not the right gate for these.
    agent_forbidden: bool = False
    # Endpoints that cannot be contract-tested, with the reason, so the coverage
    # ledger can distinguish "untested" from "deliberately untestable".
    untestable: str | None = None

    def __post_init__(self) -> None:
        if self.destructive and not self.mutating:
            raise ValueError(f"{self.path}: destructive implies mutating")
        if self.body is Body.NONE and self.method == "POST" and self.params:
            raise ValueError(f"{self.path}: POST with params needs a body encoding")
        seen: set[str] = set()
        for p in self.params:
            if p.name in seen:
                raise ValueError(f"{self.path}: duplicate param {p.name!r}")
            seen.add(p.name)

    @property
    def path(self) -> str:
        return f"{self.scope}/{self.name}"

    @property
    def cli_command(self) -> str:
        """infoAction -> info; setLocation -> set-location."""
        return camel_to_kebab(self.name)

    def available_on(self, version: Version) -> bool:
        if self.since and version < self.since:
            return False
        return not (self.until and version >= self.until)


# --------------------------------------------------------------------------- #
# Registry


REGISTRY: dict[str, Endpoint] = {}


def register(*endpoints: Endpoint) -> None:
    for e in endpoints:
        if e.path in REGISTRY:
            raise ValueError(f"duplicate endpoint {e.path}")
        REGISTRY[e.path] = e


def all_endpoints() -> list[Endpoint]:
    return sorted(REGISTRY.values(), key=lambda e: (e.scope, e.name))


# Shorthands used heavily by the scope modules.
Hashes = Param("hashes", Wire.HASHES, required=True, summary='torrent hashes, or "all"')


def flag(name: str, **kw: Any) -> Param:
    return Param(name, Wire.BOOL_STR, **kw)


def integer(name: str, **kw: Any) -> Param:
    return Param(name, Wire.INT, **kw)


def text(name: str, **kw: Any) -> Param:
    return Param(name, Wire.PLAIN, **kw)
