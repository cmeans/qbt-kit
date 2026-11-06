"""Exceptions, mapped from qBittorrent's fixed error translation.

The server maps its internal error types to status codes in one place
(`webapplication.cpp`), so the mapping is derived here once rather than
hand-written per endpoint, which would drift.
"""

from __future__ import annotations


class QbtError(Exception):
    """Base for everything this package raises."""


class TransportError(QbtError):
    """The server could not be reached at all."""


class AuthError(QbtError):
    """Authentication failed, or the session is not valid."""


class HostHeaderRejected(AuthError):
    """A 401 caused by Host-header validation rather than bad credentials.

    `validateHostHeader` is enabled by default and rejects any request whose
    Host header carries a port different from the server's listening port. The
    401 it returns is indistinguishable from bad credentials unless you notice
    that no session was ever established — which is what this distinguishes.
    Common behind port-mapped containers, SSH tunnels and reverse proxies.
    """


class BannedError(AuthError):
    """The client IP is temporarily banned after repeated failed logins."""


class ApiError(QbtError):
    """The server rejected the request."""

    def __init__(self, status: int, path: str, body: str = "") -> None:
        self.status = status
        self.path = path
        self.body = body.strip()
        detail = f": {self.body[:200]}" if self.body else ""
        super().__init__(f"{path} -> HTTP {status}{detail}")


class BadParams(ApiError):
    """400 — a parameter was missing or malformed."""


class Forbidden(ApiError):
    """403 — authenticated but not permitted."""


class NotFound(ApiError):
    """404 — unknown endpoint, or the referenced torrent does not exist."""


class Conflict(ApiError):
    """409 — the request conflicts with current state."""


class BadData(ApiError):
    """415 — the payload could not be parsed.

    Unusual choice of status by qBittorrent, hence its own type: a malformed
    .torrent upload surfaces here rather than as a 400.
    """


class UnsupportedEndpoint(QbtError):
    """The endpoint exists in the spec but not on this server version."""

    def __init__(
        self, path: str, required: tuple[int, int, int], actual: tuple[int, int, int]
    ) -> None:
        self.path = path
        self.required = required
        self.actual = actual
        req = ".".join(map(str, required))
        act = ".".join(map(str, actual))
        super().__init__(f"{path} needs WebAPI >= {req}; this server is {act}")


class RemovedEndpoint(QbtError):
    """The endpoint was removed in this server version."""

    def __init__(
        self,
        path: str,
        removed_in: tuple[int, int, int],
        actual: tuple[int, int, int],
        replacement: str | None = None,
    ) -> None:
        self.path = path
        hint = f"; use {replacement}" if replacement else ""
        super().__init__(
            f"{path} was removed in WebAPI {'.'.join(map(str, removed_in))}; "
            f"this server is {'.'.join(map(str, actual))}{hint}"
        )


class BlockedParameter(QbtError):
    """A parameter value is refused regardless of permissions.

    Used for the `app/setPreferences` keys that would sever the caller's own
    connection or weaken the server's security — changing the WebUI port,
    password, or host-header validation. Unlike the destructive gate, there is
    no confirmation that permits these: the tool simply does not do them.
    """


class AgentForbidden(QbtError):
    """A call no language model may make, whatever it passes.

    `confirm` is set by the model, not by the person reading the transcript, so
    it is evidence of intent and not of consent. It is the right gate for
    irreversible loss of the user's *own* data, which they can be asked about.
    It is the wrong gate for handing a third party code execution, a credential
    or a redirect, because the model can simply pass it. Those calls are refused
    on the agent path with no override, and stay available on the CLI where a
    person is typing.
    """


class ReadOnly(QbtError):
    """A mutating call was attempted while the client is in read-only mode."""


class ConfirmationRequired(QbtError):
    """A destructive call was attempted without explicit confirmation."""


class EveryTorrentNeedsConfirm(ConfirmationRequired):
    """A call names every torrent, and hasn't confirmed that.

    Its own type because the answer differs from a destructive call's, which keeps
    its stronger wording: these can mostly be undone, and the better next step is to
    name the torrents.
    """


class MissingTargets(QbtError):
    """A call acting on torrents doesn't clearly say which ones.

    Omitted or empty hashes used to mean every torrent, or nothing while
    reporting every torrent, so the mistake was invisible to whoever made it.
    """


class MissingTags(QbtError):
    """A tag call leaves out which tags, or names a blank one.

    On removal qBittorrent reads that as every tag: measured on 4.4.5 through 5.2.3,
    removing "" or "," took all of a torrent's tags off and reported success. Adding
    one does nothing, and is refused too because it can't be what was meant.
    """


class UnknownTorrents(QbtError):
    """None of the named torrents exist.

    The server answers 200 for a hash it doesn't have, so this has to be looked
    up: otherwise a mistyped hash reads as a success.
    """


STATUS_MAP: dict[int, type[ApiError]] = {
    400: BadParams,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    415: BadData,
}


def raise_for_status(status: int, path: str, body: str) -> None:
    """Translate a non-2xx response into the right exception."""
    if status < 400:
        return
    cls = STATUS_MAP.get(status)
    if cls is not None:
        raise cls(status, path, body)
    if status == 401:
        raise AuthError(f"{path} -> HTTP 401 {body.strip()[:120]}")
    raise ApiError(status, path, body)
