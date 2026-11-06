"""Transport: authentication, version gating, wire encoding, error mapping.

Everything here follows from what was measured in milestone 1 against real
4.6.7, 5.0.2, 5.1.2 and 5.2.3 servers. See `compat.py` for the measurements and
`docs/REVIEW-1.md` for what the first draft got wrong.
"""

from __future__ import annotations

import json as jsonlib
import logging
import os
import subprocess
import warnings
from typing import Any

import httpx

from . import compat, errors
from .spec import load_all
from .spec._types import Body, Endpoint, ReturnKind, Wire

log = logging.getLogger("qbtkit")


# httpx's DEBUG logging prints Authorization and Cookie headers, and an
# intention not to log secrets is not a control. Importing a library should not
# reconfigure the host application's logging, though, so rather than setting
# levels globally at import time each client attaches a filter to its own
# requests. `quiet_http=False` opts out.
class _NoSecrets(logging.Filter):
    """Drop any record whose text carries an Authorization or Cookie header."""

    _MARKERS = ("authorization", "cookie:", "set-cookie", "qbt_sid", "bearer ")

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage().lower()
        except Exception:  # pragma: no cover - a broken record is not ours to fix
            return True
        return not any(m in text for m in self._MARKERS)


_NO_SECRETS = _NoSecrets()

ALL = "all"  # the sentinel `hashes` accepts in place of a list


def _keychain(service: str, account: str) -> str | None:
    """Read a macOS Keychain secret, or None if absent."""
    try:
        # Absolute path and a fixed argv; `service` and `account` are
        # package-controlled identifiers, never shell-interpreted.
        out = subprocess.run(  # noqa: S603
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                account,
                "-w",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    return out.stdout.strip() or None


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off", ""}


def _parse_json_blob(value: Any) -> Any:
    """Turn JSON text into an object, so gates can see inside it."""
    if not isinstance(value, str):
        return value
    try:
        return jsonlib.loads(value)
    except jsonlib.JSONDecodeError as exc:
        raise errors.QbtError(f"not valid JSON: {exc}") from exc


def _as_bool(value: Any) -> bool:
    """Interpret a boolean that may have arrived as text.

    CLI parameters and MCP arguments cross a text boundary, so `False` reaches
    here as the string `"false"` — which is truthy. That turned
    `deleteFiles=false`, the spelling someone uses precisely to keep their
    files, into `deleteFiles=true`. Parse the known spellings and refuse the
    rest rather than falling back to truthiness, because the fallback is only
    ever wrong in the destructive direction.
    """
    if isinstance(value, bool) or value is None:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise errors.QbtError(
            f"{value!r} is not a boolean; use true/false (also 1/0, yes/no, on/off)"
        )
    return bool(value)


_NAMES_TAGS = frozenset({"torrents/addTags", "torrents/removeTags"})


def _check_targets(path: str, name: str, value: Any, confirm: bool) -> None:
    """Make a model say which torrents it means.

    On the wire, `all` is one small word for every torrent in the library. A
    person typing it at the CLI meant it; a model can reach it by leaving an
    argument out.
    """
    items = [] if value is None else value.split("|") if isinstance(value, str) else list(value)
    named = [s for s in (str(item).strip() for item in items) if s]
    if not named:
        raise errors.MissingTargets(f"{path}: {name} names no torrents")
    if ALL not in named:
        return
    if len(named) > 1:
        raise errors.MissingTargets(f'{path}: {name} mixes "all" with specific hashes')
    if not confirm:
        raise errors.EveryTorrentNeedsConfirm(
            f"{path} would act on every torrent; name the ones you mean, or "
            f"pass confirm=True if every torrent is what was asked for"
        )


def _check_tags(path: str, value: Any) -> None:
    """Make a model say which tags it means, for the same reason as `_check_targets`."""
    items = [] if value is None else value.split("|") if isinstance(value, str) else list(value)
    # qBittorrent splits on commas and drops the empty parts, so "," reaches it as no
    # tags at all.
    parts = [part for item in items for part in str(item).split(",")]
    if not parts or any(not part.strip() for part in parts):
        raise errors.MissingTags(f"{path}: tags can't be left out or blank; got {value!r}")


def encode(param_wire: Wire, value: Any) -> str | tuple[str, bytes]:
    """Render a Python value the way qBittorrent expects to read it."""
    if param_wire is Wire.BOOL_STR:
        return "true" if _as_bool(value) else "false"
    if param_wire is Wire.HASHES:
        if value == ALL:
            return ALL
        if isinstance(value, str):
            return value
        hashes = list(value)
        # A list says how many torrents were meant, and joining this would
        # silently turn one of them into two.
        if any("|" in h for h in hashes):
            raise errors.QbtError(f"a hash can't contain the | separator: {hashes!r}")
        return "|".join(hashes)
    if param_wire is Wire.PIPE_LIST:
        return value if isinstance(value, str) else "|".join(str(v) for v in value)
    if param_wire is Wire.JSON_BLOB:
        # A caller that already has JSON text — every `--json '{...}'` on the
        # CLI — must not have it encoded a second time. qBittorrent silently
        # ignores a double-encoded body, so this failed as a no-op that still
        # reported success.
        if isinstance(value, str):
            return jsonlib.dumps(_parse_json_blob(value))
        return jsonlib.dumps(value)
    if param_wire is Wire.INT:
        return str(int(value))
    if param_wire is Wire.FLOAT:
        return str(float(value))
    return str(value)


class Client:
    """A qBittorrent WebUI client.

    Authentication probes rather than predicts, because `app/webapiVersion`
    requires a session — measured 403 unauthenticated on every tested version —
    so the version cannot be consulted before deciding how to authenticate.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        username: str | None = None,
        password: str | None = None,
        api_key: str | None = None,
        read_only: bool = False,
        agent: bool = False,
        timeout: float = 30.0,
        keychain_service: str = "qbittorrent",
        use_keychain: bool = True,
    ) -> None:
        self.url = (url or os.environ.get("QBT_URL") or "http://127.0.0.1:8080").rstrip("/")
        self.base = f"{self.url}/api/v2"
        self.read_only = read_only
        # Set only by the MCP server. It belongs here rather than in the tool
        # layer because `qbt_call` reaches every endpoint in the spec: a gate
        # above the client is a gate the escape hatch walks around. Deliberately
        # not readable from the environment — a prompt that can reach os.environ
        # should not be able to turn this off.
        self.agent = agent
        self._username = username or os.environ.get("QBT_USERNAME")

        # `use_keychain=False` is how a caller says "these credentials and no
        # others". Without it, passing api_key=None still reached into the
        # Keychain, which sent a real key to throwaway test containers.
        def _vault(account: str) -> str | None:
            return _keychain(keychain_service, account) if use_keychain else None

        self._password = password or os.environ.get("QBT_PASSWORD") or _vault("webui")
        self._api_key = api_key or os.environ.get("QBT_API_KEY") or _vault("api-key")
        self._http = httpx.Client(timeout=timeout, follow_redirects=False)
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            if _NO_SECRETS not in logger.filters:
                logger.addFilter(_NO_SECRETS)
        self._version: tuple[int, int, int] | None = None
        self._auth_mode: str | None = None
        self._spec = {e.path: e for e in load_all()}

    # ------------------------------------------------------------ transport

    # httpx exceptions are an implementation detail; callers should only ever
    # have to catch QbtError.
    def _get(self, url: str, **kw: Any) -> httpx.Response:
        try:
            return self._http.get(url, **kw)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise errors.TransportError(f"GET {url}: {e}") from e

    def _post(self, url: str, **kw: Any) -> httpx.Response:
        try:
            return self._http.post(url, **kw)
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise errors.TransportError(f"POST {url}: {e}") from e

    # ----------------------------------------------------------------- auth

    def _try_bearer(self) -> bool:
        if not self._api_key:
            return False
        r = self._get(
            f"{self.base}/app/webapiVersion",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        if r.status_code == 200:
            self._http.headers["Authorization"] = f"Bearer {self._api_key}"
            self._auth_mode = "bearer"
            self._version = compat.parse_version(r.text)
            return True
        return False

    def _try_login(self) -> bool:
        if not (self._username and self._password):
            return False
        before = set(self._http.cookies.keys())
        r = self._post(
            f"{self.base}/auth/login",
            data={"username": self._username, "password": self._password},
        )
        gained = set(self._http.cookies.keys()) - before

        # Success cannot be read from the status code: 4.6/5.0/5.1 answer a
        # *failed* login with HTTP 200 and the body "Fails.". It cannot be read
        # from the body either, because 5.2 returns an empty 204. It can be read
        # from the cookie: measured across all four versions, a failed login sets
        # no cookie. That single rule replaces three dialects.
        if gained:
            self._auth_mode = "cookie"
            return True
        if r.status_code == 403:
            raise errors.BannedError(
                "login refused with 403 — the client IP is likely banned after "
                "repeated failures; wait or clear the ban in qBittorrent"
            )
        return False

    def authenticate(self) -> None:
        if self._auth_mode:
            return
        if self._try_bearer():
            return
        if self._try_login():
            return
        # Distinguish Host-header rejection from bad credentials, since both are
        # a bare 401 and only one is worth re-checking the password over.
        probe = self._get(f"{self.base}/app/version")
        if probe.status_code == 401 and not self._http.cookies:
            raise errors.HostHeaderRejected(
                "401 with no session established. If this server is reached "
                "through a mapped port, tunnel or proxy, the Host header's port "
                "must match the port qBittorrent listens on, or "
                "WebUI\\HostHeaderValidation must be disabled."
            )
        raise errors.AuthError(
            "could not authenticate: no usable API key, and username/password "
            "were rejected or absent"
        )

    @property
    def version(self) -> tuple[int, int, int]:
        """The server's WebAPI version. Requires a session, so authenticates."""
        if self._version is None:
            self.authenticate()
            if self._version is None:
                r = self._get(f"{self.base}/app/webapiVersion")
                errors.raise_for_status(r.status_code, "app/webapiVersion", r.text)
                self._version = compat.parse_version(r.text)
        return self._version

    @property
    def auth_mode(self) -> str:
        self.authenticate()
        return self._auth_mode or "unknown"

    # -------------------------------------------------------------- calling

    def call(self, path: str, /, *, confirm: bool = False, **params: Any) -> Any:
        """Invoke an endpoint by `scope/name`, validated against the spec.

        `path` is positional-only: several endpoints take a parameter of their
        own called `path` (rss/addFolder, torrents/setSavePath, …), and without
        the `/` those collide with this argument.
        """
        ep = self._spec.get(path)
        if ep is None:
            raise errors.QbtError(f"unknown endpoint {path!r}")
        return self.call_endpoint(ep, confirm=confirm, **params)

    def preflight(self, path: str, /, *, confirm: bool = False, **params: Any) -> None:
        """Run the gates for a call without making it.

        For a caller that has to read something first: a refusal shouldn't
        depend on that read succeeding.
        """
        ep = self._spec.get(path)
        if ep is None:
            raise errors.QbtError(f"unknown endpoint {path!r}")
        self._check_gates(ep, confirm, params)

    def call_endpoint(self, ep: Endpoint, *, confirm: bool = False, **params: Any) -> Any:
        self._check_gates(ep, confirm, params)
        return self._send(ep, params)

    def _check_gates(self, ep: Endpoint, confirm: bool, params: dict[str, Any]) -> None:
        """Gates, in order, all before any request so a refusal never depends on
        reaching the server.

        1. read-only      — anything mutating.
        2. agent-forbidden endpoint, then param.
        3. blocklist      — keys refused on every path, with no override.
        4. allowlist      — on the agent path only; a warning on the CLI.
        5. targets        — on the agent path only; every torrent needs `confirm`.
        6. confirm        — destructive endpoints.

        The no-override refusals come before `confirm` on purpose. Ordered the
        other way, a model is told "pass confirm=True", passes it, and only then
        learns there was never an override — two turns to reach the same wall,
        and a transcript that reads like the gate was negotiable.
        """
        if self.read_only and ep.mutating:
            raise errors.ReadOnly(f"{ep.path} mutates state; client is read-only")

        if self.agent and ep.agent_forbidden:
            raise errors.AgentForbidden(
                f"{ep.path} is not available to a language model. Ask the user to "
                f"do it in qBittorrent or with the `qbt` CLI."
            )

        for p in ep.params:
            if self.agent and p.agent_forbidden and p.name in params:
                raise errors.AgentForbidden(
                    f"{ep.path}: {p.name} is not available to a language model. "
                    f"Ask the user to do it in qBittorrent or with the `qbt` CLI."
                )

            if not (p.blocklist or p.allowlist) or p.name not in params:
                continue
            # Parse first: the CLI sends this as JSON text, and a gate that
            # only understands dicts matched nothing and let the value through.
            value = _parse_json_blob(params[p.name])
            if not isinstance(value, dict):
                raise errors.BlockedParameter(
                    f"{ep.path}: {p.name} must be an object so its keys can be "
                    f"checked against the blocklist; got {type(value).__name__}"
                )
            params[p.name] = value

            offending = sorted(set(value) & p.blocklist)
            if offending:
                raise errors.BlockedParameter(
                    f"{ep.path}: refusing to set {', '.join(offending)} — these "
                    f"would sever this client's own connection, hand over code "
                    f"execution, or weaken the server's security. Change them in "
                    f"qBittorrent directly."
                )

            if p.allowlist:
                unlisted = sorted(set(value) - p.allowlist)
                if unlisted and self.agent:
                    raise errors.AgentForbidden(
                        f"{ep.path}: {', '.join(unlisted)} is not on the agent "
                        f"allowlist. These keys can delete data, send it "
                        f"elsewhere, or name a program to run, so a model may not "
                        f"write them — there is no confirmation that permits it. "
                        f"Ask the user to change them in qBittorrent, or use the "
                        f"`qbt` CLI."
                    )
                if unlisted:
                    warnings.warn(
                        f"{ep.path}: {', '.join(unlisted)} is outside the reviewed-safe "
                        f"set; check what it does before relying on this.",
                        stacklevel=3,
                    )

        if self.agent and ep.mutating:
            for p in ep.params:
                if p.wire is Wire.HASHES and (p.required or p.name in params):
                    # A destructive call gets the destructive gate's stronger answer.
                    answered = confirm or ep.destructive
                    _check_targets(ep.path, p.name, params.get(p.name), answered)
            if ep.path in _NAMES_TAGS:
                _check_tags(ep.path, params.get("tags"))

        if ep.destructive and not confirm:
            raise errors.ConfirmationRequired(
                f"{ep.path} is destructive; pass confirm=True to proceed"
            )

    def _send(self, ep: Endpoint, params: dict[str, Any]) -> Any:
        version = self.version
        # A rename is not an absence. `since` on a renamed endpoint records when
        # the *name* changed, not when the capability appeared: stopping a
        # torrent has always existed, spelled `pause` before 5.0. So consult the
        # rename table first, and only refuse when there is no older spelling to
        # fall back to. Measured on 4.6.7: POST torrents/stop -> 404,
        # POST torrents/pause -> 200.
        endpoint_name = compat.wire_name(ep.scope, ep.name, version)
        has_legacy_name = endpoint_name != ep.name
        if ep.since and version < ep.since and not has_legacy_name:
            raise errors.UnsupportedEndpoint(ep.path, ep.since, version)
        if ep.until and version >= ep.until:
            raise errors.RemovedEndpoint(ep.path, ep.until, version)

        known = {p.name: p for p in ep.params}
        unknown = set(params) - set(known)
        if unknown:
            raise errors.QbtError(
                f"{ep.path}: unknown parameter(s) {sorted(unknown)}; accepted: {sorted(known)}"
            )
        for p in ep.params:
            if p.required and p.name not in params:
                raise errors.QbtError(f"{ep.path}: missing required parameter {p.name!r}")

        data: dict[str, Any] = {}
        files: dict[str, Any] = {}
        for name, value in params.items():
            if value is None:
                continue
            p = known[name]
            if not p.available_on(version):
                raise errors.QbtError(
                    f"{ep.path}: parameter {name!r} is not available on WebAPI "
                    f"{'.'.join(map(str, version))}"
                )
            wire_name = (
                compat.wire_param(ep.scope, ep.name, name, version) if p.renamed else name
            )
            if p.wire is Wire.FILE:
                files[wire_name] = value
            else:
                # `filter=stopped` means nothing before 5.0, and the server
                # answers an unknown filter with an empty list rather than an
                # error, so the value needs the same translation as the name.
                if name == "filter" and isinstance(value, str):
                    value = compat.wire_filter_value(value, version)
                data[wire_name] = encode(p.wire, value)

        target = f"{self.base}/{ep.scope}/{endpoint_name}"
        self.authenticate()

        if ep.method == "GET":
            r = self._get(target, params=data)
        elif ep.body is Body.MULTIPART:
            r = self._post(target, data=data, files=files or None)
        else:
            r = self._post(target, data=data)

        errors.raise_for_status(r.status_code, ep.path, r.text)

        kind = ep.returns.kind
        if kind is ReturnKind.EMPTY:
            return None
        if kind is ReturnKind.JSON:
            return r.json()
        if kind is ReturnKind.BINARY:
            return r.content
        if kind is ReturnKind.TEXT_SENTINEL:
            body = r.text.strip()
            if ep.returns.success_body and body != ep.returns.success_body:
                raise errors.ApiError(r.status_code, ep.path, body)
            return body
        return r.text

    # --------------------------------------------------------------- misc

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
