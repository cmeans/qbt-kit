"""MCP server: task-shaped tools over the qBittorrent WebUI API.

Why not one tool per endpoint: there are 123 of them, and a tool list that long
costs context on every turn and measurably degrades which tool a model picks. So
the surface is 13 tools written for what someone actually wants to do, plus
`qbt_call` as the escape hatch that keeps every endpoint reachable.

The safety gates live in `Client`, not here, so `qbt_call` cannot bypass them —
destructive endpoints need `confirm`, `--read-only` refuses anything mutating,
and the `app/setPreferences` lockout keys are refused outright.
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .. import errors
from ..client import Client
from ..spec import load_all
from .scrub import scrub, scrub_text, scrub_url

server = MCPServer(
    name="qbt-kit",
    instructions=(
        "Manage a qBittorrent instance. Prefer the task-shaped tools; use "
        "qbt_call only for endpoints they do not cover. Before concluding that "
        "a download is merely slow, use swarm_health: a torrent whose "
        "availability does not exceed its progress cannot finish at all."
    ),
)

_OPTS: dict[str, Any] = {
    "url": None,
    "username": None,
    "password": None,
    "api_key": None,
    "read_only": False,
}

# Fields worth returning by default. The full torrent object has ~60 keys, most
# of which waste the model's context.
BRIEF = (
    "name",
    "hash",
    "state",
    "progress",
    "size",
    "total_size",
    "completed",
    "category",
    "save_path",
    "num_seeds",
    "num_complete",
    "availability",
    "dlspeed",
    "upspeed",
    "eta",
    "force_start",
)


def _safe(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn a refusal into an answer the model can act on.

    Letting a QbtError escape gives the model `UnexpectedToolError: Error
    executing tool X` and nothing else — it cannot tell a missing confirmation
    from a dead server, so it cannot self-correct. Each refusal instead returns
    structured JSON naming the cause and the remedy.
    """

    def _dispatch(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except errors.ConfirmationRequired as e:
            return json.dumps(
                {
                    "refused": "confirmation_required",
                    "detail": str(e),
                    "remedy": "re-send the same call with confirm=true if this is "
                    "really intended; it cannot be undone",
                },
                indent=2,
            )
        except errors.ReadOnly as e:
            return json.dumps(
                {
                    "refused": "read_only",
                    "detail": str(e),
                    "remedy": "this server was started with --read-only; nothing "
                    "that changes state can be done through it",
                },
                indent=2,
            )
        except errors.BlockedParameter as e:
            return json.dumps(
                {
                    "refused": "blocked_parameter",
                    "detail": str(e),
                    "remedy": "there is no override. Change the setting in "
                    "qBittorrent directly if it is genuinely wanted",
                },
                indent=2,
            )
        except errors.AgentForbidden as e:
            # Worth a distinct refusal rather than folding into the one above:
            # told "blocked", a model reasonably tries confirm=true next. This
            # says the override does not exist and names who can do it instead.
            return json.dumps(
                {
                    "refused": "agent_forbidden",
                    "detail": str(e),
                    "remedy": "no confirmation permits this through MCP, because "
                    "a model sets confirm itself. Ask the user to do it in "
                    "qBittorrent or with the `qbt` CLI.",
                },
                indent=2,
            )
        except errors.UnsupportedEndpoint as e:
            return json.dumps(
                {
                    "refused": "unsupported_on_this_version",
                    "detail": str(e),
                    "remedy": "use server_info to see what this version supports",
                },
                indent=2,
            )
        except errors.HostHeaderRejected as e:
            return json.dumps(
                {
                    "refused": "host_header_validation",
                    "detail": str(e),
                    "remedy": "the credentials are probably fine; the Host header's "
                    "port must match the port qBittorrent listens on",
                },
                indent=2,
            )
        except (TypeError, ValueError, KeyError) as e:
            # Bad argument shapes reach here as plain Python errors — an extra
            # key in `params`, a non-numeric int, a missing field. Unwrapped
            # they surface as `UnexpectedToolError` with no cause, which a
            # model cannot correct from.
            return json.dumps(
                {
                    "refused": "bad_arguments",
                    "detail": f"{type(e).__name__}: {e}",
                    "remedy": "check the argument names and types against "
                    "list_endpoints; parameters go inside `params`",
                },
                indent=2,
            )
        except errors.QbtError as e:
            return json.dumps({"error": type(e).__name__, "detail": str(e)}, indent=2)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        # Scrub on the way out, including the refusals above: ApiError carries
        # the first 200 characters of the server's body, which on a 409 can be a
        # tracker URL, and TransportError carries the configured server URL.
        return _scrub_payload(_dispatch(*args, **kwargs))

    # A marker, not decoration: a unit test reads it off every tool the MCP
    # server has registered, so adding a tool with bare `@server.tool` fails
    # the suite instead of silently returning unscrubbed output.
    setattr(wrapper, "__qbt_scrubbed__", True)  # noqa: B010
    return wrapper


def _scrub_payload(text: str) -> str:
    """Decode, walk, re-encode once.

    Walking the object is what reaches dict keys — `sync/maindata` keys its
    trackers map by announce URL — and avoids `json.dumps` escaping non-ASCII to
    `\\uXXXX`, which would hide a URL from any pattern applied to the text.
    """
    if not isinstance(text, str):
        # A tool returning a dict would have MCP serialise it downstream of this
        # wrapper, where nothing scrubs it. Fail loudly rather than leak quietly.
        raise TypeError(
            f"MCP tools must return a JSON string so it can be scrubbed; "
            f"got {type(text).__name__}"
        )
    try:
        data = json.loads(text)
    except ValueError:
        return scrub_text(text)
    return json.dumps(scrub(data), indent=2, default=str)


def _tool(**kw: Any) -> Callable[[Callable[..., str]], Callable[..., str]]:
    """Register a tool and scrub its output in one act.

    Two call sites of the old key-name redaction, and thirteen tools that did
    not call it, is what the external report found. Registration and scrubbing
    being separate decorators is what made forgetting possible, so they are one
    decorator now and there is no path to `server.tool` that skips the scrub.
    """

    def decorate(fn: Callable[..., str]) -> Callable[..., str]:
        return server.tool(**kw)(_safe(fn))  # type: ignore[no-any-return]

    return decorate


def _client() -> Client:
    return Client(
        _OPTS["url"],
        username=_OPTS["username"],
        password=_OPTS["password"],
        api_key=_OPTS["api_key"],
        read_only=_OPTS["read_only"],
        # Every client this process builds is driven by a model, so the stricter
        # gates apply to all of them — including the ones `qbt_call` uses.
        agent=True,
    )


def _brief(t: dict, fields: tuple[str, ...] = BRIEF) -> dict:
    return {k: t[k] for k in fields if k in t}


def _hashes(value: str | list[str] | None) -> str | list[str]:
    if value is None:
        return "all"
    return value


# --------------------------------------------------------------------------- #
# reading


@_tool(description="List torrents, with the fields that usually matter.")
def list_torrents(
    status: Annotated[
        str | None,
        Field(description="all, downloading, seeding, completed, stopped, stalled, errored"),
    ] = None,
    category: Annotated[str | None, Field(description="category name")] = None,
    tag: Annotated[str | None, Field(description="tag name")] = None,
    sort: Annotated[str | None, Field(description="field to sort by, e.g. progress")] = None,
    limit: Annotated[int | None, Field(description="maximum rows")] = None,
    full: Annotated[
        bool, Field(description="return every field rather than the useful subset")
    ] = False,
) -> str:
    with _client() as c:
        rows = c.call(
            "torrents/info", filter=status, category=category, tag=tag, sort=sort, limit=limit
        )
    out = rows if full else [_brief(t) for t in rows]
    return json.dumps({"count": len(rows), "torrents": out}, indent=2)


@_tool(description="Properties, trackers and files for one torrent.")
def torrent_detail(
    hash: Annotated[str, Field(description="torrent hash")],
) -> str:
    with _client() as c:
        return json.dumps(
            {
                "properties": c.call("torrents/properties", hash=hash),
                "trackers": c.call("torrents/trackers", hash=hash),
                "files": c.call("torrents/files", hash=hash),
            },
            indent=2,
        )


@_tool(
    description=(
        "Judge whether each torrent can actually finish. `availability` is how many "
        "complete copies exist across reachable peers; when it does not exceed "
        "`progress`, no peer holds the missing pieces and the download can never "
        "complete however long it waits. Queued torrents are excluded because they "
        "report availability 0 simply for not being connected yet. `progress` is "
        "relative to the selected files, so `partial_selection` marks torrents "
        "that are 100% yet never fetched their deselected files."
    )
)
def swarm_health(
    include_complete: Annotated[
        bool, Field(description="also report torrents already at 100%")
    ] = False,
) -> str:
    with _client() as c:
        rows = c.call("torrents/info")
    verdicts = []
    for t in rows:
        progress, avail = t["progress"], t["availability"]
        # `size` counts only the files selected for download, so a torrent with
        # deselected files reaches progress 1.0 having never fetched them. The
        # verdict has to say so: a caller deciding "the content is all on disk,
        # the torrent is safe to delete" is wrong on exactly these.
        # `total_size` is present on every version measured (4.6 through 5.2),
        # but a field that vanishes should degrade to "unknown" rather than
        # take the whole health report down with a KeyError.
        total_size = t.get("total_size")
        partial = total_size is not None and t["size"] < total_size
        if progress >= 1.0:
            if not include_complete:
                continue
            verdict = (
                "complete for the selected files only — some files were "
                "deselected and were never downloaded"
                if partial
                else "complete"
            )
        elif t["state"] in (
            "queuedDL",
            "queuedUP",
            "checkingDL",
            "checkingUP",
            "metaDL",
            "forcedMetaDL",
            "allocating",
            "checkingResumeData",
            "moving",
        ):
            verdict = "not yet connected — cannot be judged"
        elif avail - progress > 0.05:
            verdict = "healthy — the swarm holds what it lacks"
        else:
            verdict = "stuck — no reachable peer has the missing pieces"
        verdicts.append(
            {
                "name": t["name"],
                "hash": t["hash"],
                "state": t["state"],
                "progress": round(progress, 4),
                "partial_selection": partial,
                "availability": round(avail, 3),
                "gap": round(avail - progress, 3),
                "missing_bytes": t["size"] - t["completed"],
                "seeds_connected": t["num_seeds"],
                "seeds_known": t["num_complete"],
                "verdict": verdict,
            }
        )
    stuck = [v for v in verdicts if str(v["verdict"]).startswith("stuck")]
    return json.dumps(
        {
            "summary": {
                "total": len(verdicts),
                "stuck": len(stuck),
                "stuck_bytes_held": sum(
                    r["completed"] for r in rows if any(s["hash"] == r["hash"] for s in stuck)
                ),
            },
            "torrents": verdicts,
        },
        indent=2,
    )


@_tool(description="Global transfer rates, limits and connection status.")
def transfer_status() -> str:
    with _client() as c:
        return json.dumps(
            {
                "info": c.call("transfer/info"),
                "download_limit": c.call("transfer/downloadLimit"),
                "upload_limit": c.call("transfer/uploadLimit"),
                "alt_limits_active": c.call("transfer/speedLimitsMode"),
            },
            indent=2,
        )


@_tool(description="Server version, auth mode and endpoint availability.")
def server_info() -> str:
    with _client() as c:
        version = c.version
        eps = load_all()
        return json.dumps(
            {
                "url": scrub_url(c.url),
                "qbittorrent": c.call("app/version"),
                "webapi": ".".join(map(str, version)),
                "auth_mode": c.auth_mode,
                "read_only": _OPTS["read_only"],
                "endpoints_available": sum(1 for e in eps if e.available_on(version)),
                "endpoints_known": len(eps),
                "unavailable": [e.path for e in eps if not e.available_on(version)],
            },
            indent=2,
        )


# --------------------------------------------------------------------------- #
# writing


@_tool(description="Add a torrent by magnet or URL, or several at once.")
def add_torrent(
    urls: Annotated[list[str], Field(description="magnet or http(s) URLs")],
    category: Annotated[str | None, Field(description="category to file it under")] = None,
    save_path: Annotated[
        str | None,
        Field(description="explicit save path; overrides the category's"),
    ] = None,
    stopped: Annotated[bool, Field(description="add without starting")] = False,
    auto_tmm: Annotated[
        bool | None,
        Field(
            description=(
                "automatic torrent management. Left unset it is on when a category "
                "is given without an explicit save path, which is what makes the "
                "category's save path apply."
            )
        ),
    ] = None,
) -> str:
    # qBittorrent only honours a category's save path under automatic torrent
    # management; without it the torrent lands in the global save path and the
    # category becomes a label only. Naming a category and getting a different
    # directory is never the intent, so infer it rather than making every caller
    # remember — this silently put a 55 GB download in the wrong place once.
    if auto_tmm is None:
        auto_tmm = category is not None and save_path is None
    with _client() as c:
        result = c.call(
            "torrents/add",
            urls=urls,
            category=category,
            savepath=save_path,
            stopped=stopped,
            autoTMM=auto_tmm,
        )
    return json.dumps({"result": result}, indent=2)


@_tool(
    description=(
        "Start, stop, recheck or reannounce torrents. Note that force_start makes a "
        "torrent ignore share limits, so a stop-on-complete rule stops applying to it."
    )
)
def control_torrents(
    action: Annotated[
        str, Field(description="start, stop, recheck, reannounce, force_start, unforce_start")
    ],
    hashes: Annotated[
        list[str] | None, Field(description="torrent hashes; omit for all")
    ] = None,
) -> str:
    paths = {
        "start": ("torrents/start", {}),
        "stop": ("torrents/stop", {}),
        "recheck": ("torrents/recheck", {}),
        "reannounce": ("torrents/reannounce", {}),
        "force_start": ("torrents/setForceStart", {"value": True}),
        "unforce_start": ("torrents/setForceStart", {"value": False}),
    }
    if action not in paths:
        return json.dumps({"error": f"unknown action {action!r}", "accepted": sorted(paths)})
    path, extra = paths[action]
    with _client() as c:
        c.call(path, hashes=_hashes(hashes), **extra)
    return json.dumps({"ok": True, "action": action, "applied_to": hashes or "all"}, indent=2)


@_tool(
    description=(
        "Remove torrents. `delete_files` is never implied: without it the data stays "
        "on disk. Requires confirm=True because it cannot be undone."
    )
)
def delete_torrents(
    hashes: Annotated[
        list[str],
        Field(
            description=(
                'torrent hashes to remove. The single value "all" targets EVERY '
                "torrent in the queue — pass explicit hashes unless wiping the "
                "whole queue is genuinely what was asked."
            )
        ),
    ],
    delete_files: Annotated[bool, Field(description="also delete the downloaded data")] = False,
    confirm: Annotated[bool, Field(description="must be true to proceed")] = False,
) -> str:
    with _client() as c:
        c.call("torrents/delete", hashes=hashes, deleteFiles=delete_files, confirm=confirm)
    return json.dumps(
        {"ok": True, "removed": len(hashes), "files_deleted": delete_files}, indent=2
    )


@_tool(
    description=(
        "Move a torrent's data to a new location. qBittorrent performs the move and "
        "the torrent stays valid; the call returns before the move finishes. A "
        "recheck afterwards is optional because it rehashes everything."
    )
)
def move_torrents(
    hashes: Annotated[list[str], Field(description="torrent hashes")],
    location: Annotated[str, Field(description="destination directory")],
    recheck: Annotated[
        bool, Field(description="verify against piece hashes afterwards; slow on large data")
    ] = False,
) -> str:
    with _client() as c:
        c.call("torrents/setLocation", hashes=hashes, location=location)
        if recheck:
            c.call("torrents/recheck", hashes=hashes)
    return json.dumps(
        {"ok": True, "moved": len(hashes), "location": location, "rechecking": recheck},
        indent=2,
    )


@_tool(description="List, create, edit or remove categories.")
def categories(
    action: Annotated[str, Field(description="list, create, edit or remove")] = "list",
    name: Annotated[str | None, Field(description="category name")] = None,
    save_path: Annotated[str | None, Field(description="save path for it")] = None,
    confirm: Annotated[bool, Field(description="required for remove")] = False,
) -> str:
    with _client() as c:
        if action == "list":
            return json.dumps(c.call("torrents/categories"), indent=2)
        if name is None:
            return json.dumps({"error": "name is required for this action"})
        if action == "create":
            c.call("torrents/createCategory", category=name, savePath=save_path)
        elif action == "edit":
            c.call("torrents/editCategory", category=name, savePath=save_path or "")
        elif action == "remove":
            c.call("torrents/removeCategories", categories=[name], confirm=confirm)
        else:
            return json.dumps({"error": f"unknown action {action!r}"})
    return json.dumps({"ok": True, "action": action, "category": name}, indent=2)


@_tool(description="List, create, delete, add or remove tags.")
def tags(
    action: Annotated[str, Field(description="list, create, delete, add or remove")] = "list",
    names: Annotated[list[str] | None, Field(description="tag names")] = None,
    hashes: Annotated[
        list[str] | None, Field(description="torrents, for add and remove")
    ] = None,
    confirm: Annotated[bool, Field(description="required for delete")] = False,
) -> str:
    with _client() as c:
        if action == "list":
            return json.dumps(c.call("torrents/tags"), indent=2)
        if not names:
            return json.dumps({"error": "names is required for this action"})
        if action == "create":
            c.call("torrents/createTags", tags=names)
        elif action == "delete":
            c.call("torrents/deleteTags", tags=names, confirm=confirm)
        elif action == "add":
            c.call("torrents/addTags", hashes=_hashes(hashes), tags=names)
        elif action == "remove":
            c.call("torrents/removeTags", hashes=_hashes(hashes), tags=names)
        else:
            return json.dumps({"error": f"unknown action {action!r}"})
    return json.dumps({"ok": True, "action": action, "tags": names}, indent=2)


@_tool(
    description=(
        "Read preferences, or write a subset. Keys that would sever this connection "
        "or weaken the server's security (WebUI port, password, host-header "
        "validation and similar) are refused outright, with no override."
    )
)
def preferences(
    action: Annotated[str, Field(description="read or write")] = "read",
    values: Annotated[
        dict[str, Any] | None, Field(description="preference keys to set, for write")
    ] = None,
    keys: Annotated[list[str] | None, Field(description="limit a read to these keys")] = None,
) -> str:
    with _client() as c:
        if action == "read":
            prefs = c.call("app/preferences")
            if keys:
                prefs = {k: prefs.get(k) for k in keys}
            return json.dumps(prefs, indent=2)
        if not values:
            return json.dumps({"error": "values is required for write"})
        c.call("app/setPreferences", json=values)
    return json.dumps({"ok": True, "set": sorted(values)}, indent=2)


@_tool(
    description=(
        "Run a plugin search: start it, poll until it settles, return the results. "
        "Requires qBittorrent to have search plugins installed."
    )
)
def search(
    pattern: Annotated[str, Field(description="what to search for")],
    plugins: Annotated[str, Field(description='plugin names, "all" or "enabled"')] = "enabled",
    category: Annotated[str, Field(description='category or "all"')] = "all",
    limit: Annotated[int, Field(description="maximum results")] = 25,
    wait_seconds: Annotated[int, Field(description="how long to poll")] = 20,
) -> str:
    import time

    with _client() as c:
        job = c.call("search/start", pattern=pattern, plugins=plugins, category=category)
        sid = job["id"]
        try:
            deadline = time.time() + wait_seconds
            while time.time() < deadline:
                status = c.call("search/status", id=sid)
                if status and status[0].get("status") != "Running":
                    break
                time.sleep(1)
            results = c.call("search/results", id=sid, limit=limit)
        finally:
            c.call("search/delete", id=sid)
    return json.dumps(results, indent=2)


# --------------------------------------------------------------------------- #
# the escape hatch


@_tool(
    description=(
        "Call any qBittorrent endpoint directly as scope/name, for anything the "
        "tools above do not cover. Parameters are validated against the endpoint "
        "spec, and the same safety gates apply: destructive endpoints need confirm, "
        "and read-only mode refuses anything that mutates state."
    )
)
def qbt_call(
    path: Annotated[str, Field(description="scope/name, e.g. torrents/pieceStates")],
    params: Annotated[dict[str, Any] | None, Field(description="endpoint parameters")] = None,
    confirm: Annotated[bool, Field(description="required for destructive endpoints")] = False,
) -> str:
    with _client() as c:
        result = c.call(path, confirm=confirm, **(params or {}))
    if isinstance(result, bytes):
        return json.dumps(
            {"bytes": len(result), "note": "binary response, not returned inline"}
        )
    return json.dumps({"result": result}, indent=2, default=str)


@_tool(description="List every endpoint qbt_call can reach, with its shape.")
def list_endpoints(
    scope: Annotated[str | None, Field(description="filter to one scope")] = None,
    mutating_only: Annotated[
        bool, Field(description="only endpoints that change state")
    ] = False,
) -> str:
    out = []
    for e in load_all():
        if scope and e.scope != scope:
            continue
        if mutating_only and not e.mutating:
            continue
        out.append(
            {
                "path": e.path,
                "method": e.method,
                "summary": e.summary,
                "mutating": e.mutating,
                "destructive": e.destructive,
                # Advertised so a model can see a wall before walking into it,
                # rather than discovering it as a refusal.
                "agent_forbidden": e.agent_forbidden,
                "params": [p.name for p in e.params],
                "agent_forbidden_params": [p.name for p in e.params if p.agent_forbidden],
                "since": ".".join(map(str, e.since)) if e.since else None,
            }
        )
    return json.dumps({"count": len(out), "endpoints": out}, indent=2)


# --------------------------------------------------------------------------- #


ENV_VARS = """Configuration is read from flags first, then the environment, then
the macOS Keychain:

  QBT_URL         base URL, default http://127.0.0.1:8080
  QBT_API_KEY     API key (qBittorrent 5.2+); preferred when present
  QBT_USERNAME    WebUI username, for cookie login on any version
  QBT_PASSWORD    WebUI password
  QBT_READ_ONLY   1/true/yes to refuse everything that changes state

Keychain fallback, service `qbittorrent`: account `api-key` for a key, account
`webui` for the password.

qBittorrent has no OAuth, so there is no token flow to delegate and the secret
is long-lived. Prefer the Keychain: an MCP client's config file is plaintext and
frequently committed. If you do use the `env` block, reference a variable rather
than pasting the secret:

  "env": { "QBT_API_KEY": "${QBT_API_KEY}" }
"""


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n")[0],
        epilog=ENV_VARS,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url")
    ap.add_argument("--username")
    ap.add_argument("--password")
    ap.add_argument("--api-key")
    ap.add_argument(
        "--read-only",
        action="store_true",
        default=_env_flag("QBT_READ_ONLY"),
        help="refuse every endpoint that changes state (or set QBT_READ_ONLY=1)",
    )
    ap.add_argument("--transport", default="stdio", choices=("stdio", "sse", "streamable-http"))
    args = ap.parse_args()
    _OPTS.update(
        url=args.url,
        username=args.username,
        password=args.password,
        api_key=args.api_key,
        read_only=args.read_only,
    )

    # Fail loudly at startup rather than on the model's first tool call, and say
    # where the credentials came from — never what they are. An unset `${VAR}`
    # in an MCP config is passed through literally rather than as empty, so
    # catching that here saves a confusing 401 later.
    for var in ("QBT_API_KEY", "QBT_PASSWORD", "QBT_USERNAME", "QBT_URL"):
        value = os.environ.get(var, "")
        if value.startswith("${") and value.endswith("}"):
            raise SystemExit(
                f"qbt-mcp: {var} is the literal text {value!r}, which means the "
                f"variable was not set when your MCP client expanded its config. "
                f"Export it, give it a default like ${{{var}:-...}}, or drop it "
                f"and rely on the Keychain."
            )
    try:
        with _client() as c:
            c.authenticate()
            source = (
                "--api-key flag"
                if args.api_key
                else "QBT_API_KEY"
                if os.environ.get("QBT_API_KEY")
                else "--password flag"
                if args.password
                else "QBT_PASSWORD"
                if os.environ.get("QBT_PASSWORD")
                else "macOS Keychain"
            )
            print(
                f"qbt-mcp: {c.url} · WebAPI {'.'.join(map(str, c.version))} · "
                f"auth={c.auth_mode} via {source}"
                f"{' · read-only' if _OPTS['read_only'] else ''}",
                file=sys.stderr,
            )
    except (errors.TransportError, errors.BannedError) as e:
        # Report, but keep serving. An MCP client launches its servers when the
        # session starts, which for a desktop qBittorrent is routinely before
        # the application is running. Exiting strips every tool from the
        # model's list and the client reports only "connection closed", so the
        # one fact worth knowing — that qBittorrent is down — never reaches the
        # user. Each tool reports the failure per call instead, where it can be
        # acted on. Both of these clear on their own: the server comes up, the
        # ban expires.
        print(f"qbt-mcp: {e}", file=sys.stderr)
    except errors.QbtError as e:
        # Everything else here is a configuration error that will not fix
        # itself while the process runs — rejected credentials, a refused host
        # header. Staying up would be worse than exiting: every tool call
        # builds a fresh client and re-POSTs auth/login, and repeated failed
        # logins are exactly what qBittorrent bans an IP for, which would lock
        # the user out of the WebUI in their browser too. Exit, as the ${VAR}
        # check above does.
        raise SystemExit(f"qbt-mcp: {e}") from e

    server.run(transport=args.transport)


if __name__ == "__main__":
    main()
