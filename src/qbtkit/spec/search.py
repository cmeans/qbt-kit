"""The `search` scope — 11 endpoints driving qBittorrent's plugin search.

Searching is a job: `start` returns an id, `status` and `results` are polled,
`stop` and `delete` wind it down. That lifecycle is stateful and belongs in the
library rather than the spec.

Most of this scope cannot be contract-tested on a private network: it needs an
installed plugin and outbound internet. Those endpoints declare why.
"""

from __future__ import annotations

from ._types import Body, Endpoint, Json, NoContent, Param, Wire, flag, integer, register, text

NEEDS_NET = "needs an installed search plugin and outbound internet"

register(
    Endpoint(
        scope="search",
        name="start",
        method="POST",
        since=(2, 1, 1),
        summary="Begin a search. Returns an id to poll. Fails with 409 once five "
        "searches are already running.",
        returns=Json("SearchJob"),
        body=Body.URLENCODED,
        params=(
            text("pattern", required=True),
            Param(
                "plugins",
                Wire.PIPE_LIST,
                required=True,
                summary='plugin names, or "all" / "enabled"',
            ),
            text("category", required=True, summary='or "all"'),
        ),
        mutating=True,
        untestable=NEEDS_NET,
    ),
    Endpoint(
        scope="search",
        name="stop",
        method="POST",
        since=(2, 1, 1),
        summary="Stop a running search.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(integer("id", required=True),),
        mutating=True,
        untestable=NEEDS_NET,
    ),
    Endpoint(
        scope="search",
        name="status",
        method="GET",
        since=(2, 1, 1),
        summary="Status of one search, or of all of them.",
        returns=Json("SearchStatus"),
        params=(integer("id"),),
    ),
    Endpoint(
        scope="search",
        name="results",
        method="GET",
        since=(2, 1, 1),
        summary="Results of a search, paginated. An out-of-range offset is a 409.",
        returns=Json("SearchResults"),
        params=(integer("id", required=True), integer("limit"), integer("offset")),
        untestable=NEEDS_NET,
    ),
    Endpoint(
        scope="search",
        name="delete",
        method="POST",
        since=(2, 1, 1),
        summary="Discard a search and its results.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(integer("id", required=True),),
        mutating=True,
        untestable=NEEDS_NET,
    ),
    Endpoint(
        scope="search",
        name="downloadTorrent",
        method="POST",
        since=(2, 11, 3),
        summary="Add a torrent straight from a search result.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(text("torrentUrl", required=True), text("plugin")),
        mutating=True,
        untestable=NEEDS_NET,
    ),
    # ----- plugin management
    Endpoint(
        scope="search",
        name="plugins",
        method="GET",
        since=(2, 1, 1),
        summary="Installed search plugins, with their enabled state and categories.",
        returns=Json("SearchPlugin"),
    ),
    Endpoint(
        scope="search",
        name="installPlugin",
        method="POST",
        since=(2, 1, 1),
        summary="Install plugins from URLs or local paths. These are third-party "
        "Python modules that run inside qBittorrent's process, so install "
        "only from sources you have reviewed.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(Param("sources", Wire.PIPE_LIST, required=True),),
        mutating=True,
        # Reported externally against 0.1.2: a plugin was fetched from a URL and
        # its Python ran inside qBittorrent's process. The prose warning above is
        # advice to a human reading the summary; it was never a gate.
        agent_forbidden=True,
        untestable="would fetch and execute third-party code",
    ),
    Endpoint(
        scope="search",
        name="uninstallPlugin",
        method="POST",
        since=(2, 1, 1),
        summary="Remove search plugins.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(Param("names", Wire.PIPE_LIST, required=True),),
        mutating=True,
        destructive=True,
    ),
    Endpoint(
        scope="search",
        name="enablePlugin",
        method="POST",
        since=(2, 1, 1),
        summary="Enable or disable plugins.",
        returns=NoContent,
        body=Body.URLENCODED,
        params=(Param("names", Wire.PIPE_LIST, required=True), flag("enable", required=True)),
        mutating=True,
    ),
    Endpoint(
        scope="search",
        name="updatePlugins",
        method="POST",
        since=(2, 1, 1),
        summary="Update all installed plugins from their source URLs.",
        returns=NoContent,
        mutating=True,
        # Same outcome as installPlugin without naming a source: it re-fetches
        # and re-executes from whatever URLs the installed plugins carry.
        agent_forbidden=True,
        untestable="would fetch third-party code",
    ),
)
