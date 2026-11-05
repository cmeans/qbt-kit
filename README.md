# qbt-kit

[![CI status](https://github.com/manueltarouca/qbt-kit/actions/workflows/ci.yaml/badge.svg)](https://github.com/manueltarouca/qbt-kit/actions/workflows/ci.yaml)
[![Latest PyPI version](https://img.shields.io/pypi/v/qbt-kit.svg)](https://pypi.org/project/qbt-kit/)
[![Supported Python versions](https://img.shields.io/pypi/pyversions/qbt-kit.svg)](https://pypi.org/project/qbt-kit/)
[![MIT licence](https://img.shields.io/badge/licence-MIT-blue.svg)](https://github.com/manueltarouca/qbt-kit/blob/main/LICENSE)

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/manueltarouca/qbt-kit/main/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/manueltarouca/qbt-kit/main/assets/logo.png">
    <img alt="qbt-kit" src="https://raw.githubusercontent.com/manueltarouca/qbt-kit/main/assets/logo.png" width="180">
  </picture>
</p>

A complete, typed Python client for the qBittorrent WebUI API — with a CLI that
covers every endpoint one-to-one, and an MCP server so coding agents can drive
it without deleting your data.

```
$ qbt coverage

qbt-kit coverage — target release-5.2.3

  scope               n   spec  progress
  torrents           61     61  ████████████████████  61/61
  app                16     16  ████████████████████  16/16
  rss                14     14  ████████████████████  14/14
  search             11     11  ████████████████████  11/11
  transfer            9      9  ████████████████████  9/9
  torrentcreator      4      4  ████████████████████  4/4
  auth                2      2  ████████████████████  2/2
  clientdata          2      2  ████████████████████  2/2
  log                 2      2  ████████████████████  2/2
  sync                2      2  ████████████████████  2/2
                   ----
  TOTAL             123    123  ████████████████████  100.0%
```

<p align="center">
  <i>The ledger is generated from qBittorrent's own source, and CI fails if it slips.</i>
</p>

## Highlights

- **All 123 endpoints** of qBittorrent 5.2.3, each one a spec entry that
  generates the client method, the CLI command and the docs together.
- **A verifiable coverage claim.** The endpoint list is extracted from the
  qBittorrent C++ source per release tag and committed, so CI fails both when
  the spec misses an endpoint and when it invents one.
- **An MCP server** with 15 tools — 13 task-shaped, plus `qbt_call` and
  `list_endpoints` — and safety gates that hold even through the escape hatch.
- **Compatibility measured, not assumed** — tested against real 4.6, 5.0, 5.1
  and 5.2 containers, with a rename table so `torrents/stop` resolves to
  `pause` on servers that predate the rename.
- **Typed throughout**, no runtime dependency beyond `httpx` and `typer`.

That coverage claim is worth a sentence of context. The wiki page most clients
are written against documents **78** endpoints. qBittorrent 5.2.3 ships
**123**, and two whole scopes — `torrentcreator` and `clientdata` — appear in
no wiki page at all.

## Installation

PyPI's `qbt` name belongs to an unrelated package, so the CLI is run via
`--from`:

```sh
uvx --from qbt-kit qbt doctor
uvx --from "qbt-kit[mcp]" qbt-mcp --help   # the server lives in the `mcp` extra
```

Both run via `--from` because the `qbt` name on PyPI belongs to an unrelated
package, and `qbt-mcp` is a script inside this one rather than a package of its
own — `uvx qbt-mcp` resolves to nothing.

Or add it to a project:

```sh
uv add qbt-kit              # library and CLI
uv add "qbt-kit[mcp]"       # with the MCP server
```

### As a Claude Code plugin

This repository is also a plugin marketplace, so one line installs the MCP
server and the skill together:

```
/plugin install qbt-kit --marketplace manueltarouca/qbt-kit
```

The bundled MCP server runs from the plugin's own checkout, so it needs `uv`
but not a published package, and it always matches the version you installed.
Point it at your server with `QBT_URL`, and see [Configuration](#configuration)
for credentials.

## Quickstart

```sh
qbt doctor                          # version, auth mode, endpoint availability
qbt torrents info --category tv     # the API calls this `info`, not `list`
qbt torrents add --urls <magnet> --category tv --auto-tmm
qbt health                          # swarm health across the queue
qbt coverage                        # the endpoint coverage ledger
qbt call torrents/pieceStates -p hash=<h>    # raw, reaches all 123
```

Scope commands mirror the API exactly — `torrents info`, not a friendlier
`list` — so anything you read in qBittorrent's own docs translates directly.
`--json` works everywhere, so every command composes with `jq`.

`qbt ready` is also there, but it inspects the local filesystem to decide
whether a download has really settled, so it only means anything when the CLI
runs on the same machine as qBittorrent.

## MCP server

```jsonc
{
  "mcpServers": {
    "qbittorrent": {
      "command": "uvx",
      "args": ["--from", "qbt-kit[mcp]", "qbt-mcp", "--url", "http://127.0.0.1:8080"]
    }
  }
}
```

Fifteen tools rather than 123 — a tool list that long costs context
on every turn and measurably worsens which tool a model picks. `qbt_call` keeps
every endpoint reachable and `list_endpoints` lets a model discover them, so the
small surface stays complete.

### Configuration

Flags first, then the environment, then the macOS Keychain:

| variable | meaning |
|---|---|
| `QBT_URL` | base URL, default `http://127.0.0.1:8080` |
| `QBT_API_KEY` | API key (qBittorrent 5.2+); preferred when present |
| `QBT_USERNAME` | WebUI username, for cookie login on any version |
| `QBT_PASSWORD` | WebUI password |
| `QBT_READ_ONLY` | `1`/`true`/`yes` to refuse everything that changes state |

Keychain fallback uses service `qbittorrent`, account `api-key` for a key and
`webui` for the password:

```sh
security add-generic-password -s qbittorrent -a api-key -w '<key>'
```

**qBittorrent has no OAuth**, so there is no token flow to delegate and the
secret is long-lived. The reference MCP servers put credentials straight into
the client's `env` block — `GITHUB_PERSONAL_ACCESS_TOKEN` is the canonical
example — but that file is plaintext and frequently committed. Prefer the
Keychain, and if you do use `env`, reference a variable instead of pasting the
secret:

```jsonc
"env": {
  "QBT_URL": "${QBT_URL:-http://127.0.0.1:8080}",
  "QBT_API_KEY": "${QBT_API_KEY:-}"      // empty falls through to the Keychain
}
```

The `:-` default matters. Claude Code passes an unset `${VAR}` through as the
*literal text* `${VAR}` rather than as empty, which otherwise surfaces as a
confusing 401 much later — so `qbt-mcp` detects that case at startup and says
so plainly instead of starting.

On startup it reports, on stderr, where the credentials came from and never
what they are:

```
qbt-mcp: http://127.0.0.1:8080 · WebAPI 2.15.1 · auth=bearer via macOS Keychain
```

### Safety

An agent driving a torrent client can destroy data that took weeks to collect,
so the gates are code rather than guidance:

- Destructive endpoints require `confirm`; `delete_files` is never implied.
- MCP tools act only on the torrents they name. `all` needs `confirm`, and a
  mistyped argument is an error.
- `--read-only` (or `QBT_READ_ONLY=1`) blocks mutations, **including via
  `qbt_call`**.
- `app/setPreferences` refuses the WebUI, auth and network keys that would
  sever the connection or weaken security. There is no override.
- Refusals return a structured cause and remedy, so an agent can correct itself
  instead of seeing an opaque tool error.

`qbt-mcp` speaks stdio by default, which keeps it a child process with nothing
listening. The `sse` and `streamable-http` transports expose an
*unauthenticated* control surface holding your qBittorrent credentials — bound
to loopback, but do not forward those ports. See [SECURITY.md](https://github.com/manueltarouca/qbt-kit/blob/main/SECURITY.md).

## Documentation

| | |
|---|---|
| [`docs/TRACEABILITY.md`](https://github.com/manueltarouca/qbt-kit/blob/main/docs/TRACEABILITY.md) | how the coverage claim is built and gated |
| [`docs/TESTING.md`](https://github.com/manueltarouca/qbt-kit/blob/main/docs/TESTING.md) | the container harness, and behaviours that cost real time to learn |
| [`docs/PLAN.md`](https://github.com/manueltarouca/qbt-kit/blob/main/docs/PLAN.md) | the design and the reasoning behind it |
| [`docs/ROADMAP.md`](https://github.com/manueltarouca/qbt-kit/blob/main/docs/ROADMAP.md) | what is done and what is not |
| [`docs/REVIEW-1.md`](https://github.com/manueltarouca/qbt-kit/blob/main/docs/REVIEW-1.md) | an independent review of the plan, with every finding dispositioned |
| [`docs/dependencies.md`](https://github.com/manueltarouca/qbt-kit/blob/main/docs/dependencies.md) | every dependency, and why it is justified |
| [`SECURITY.md`](https://github.com/manueltarouca/qbt-kit/blob/main/SECURITY.md) | threat model, credential handling, how to report a vulnerability |

A Claude skill ships in [`skills/qbt-kit/`](https://github.com/manueltarouca/qbt-kit/tree/main/skills/qbt-kit/) covering CLI usage
and the swarm-health rule.

## Field notes

Things that cost us time, documented so they cost you none.

- **Login success cannot be read from the status code.** qBittorrent ≤5.1
  answers a *failed* login with HTTP 200 and the body `Fails.`; 5.2 returns an
  empty 204. The only rule that holds across versions is whether a session
  cookie was issued.
- **`app/webapiVersion` requires authentication.** Only `auth/login` is public,
  so you cannot read the version to decide how to authenticate. Probe instead.
- **The session cookie is not always `SID`.** On 5.2 it is `QBT_SID_<port>`;
  on 5.0/5.1 it is user-configurable. Never match on the name.
- **A bare 401 is often Host-header validation, not bad credentials.** It is on
  by default and rejects any request whose `Host` port differs from the port
  qBittorrent listens on — which is what happens behind mapped container ports,
  SSH tunnels and most reverse proxies.
- **API keys exist only from qBittorrent 5.2.0.** Sending `Authorization:
  Bearer` to 5.0/5.1 gets a 403 that looks like a credentials problem.
- **`pause`/`resume` were removed in 5.0**, not aliased; they 404 there.
- **`progress: 1.0` does not mean every file is on disk.** Progress is computed
  over the *selected* files, so a torrent with files at priority 0 reports 100%
  having never fetched them. `size < total_size` is the cheap flag, from the
  same listing call.
- **A category alone does not place a torrent.** qBittorrent only applies a
  category's save path under automatic torrent management, so `torrents/add`
  with a category but without `autoTMM` files it in the global save path.
- **A stalled torrent is usually not slow but stuck.** `availability` is how
  many complete copies exist across reachable peers; when it does not exceed
  `progress`, no peer has the missing pieces. But a *queued* torrent reports
  availability 0 simply for not being connected — judging those as dead is how
  you delete healthy downloads.

## Contributing

See [CONTRIBUTING.md](https://github.com/manueltarouca/qbt-kit/blob/main/CONTRIBUTING.md). The short version: endpoints are added
to the spec under `src/qbtkit/spec/`, never by hand to the CLI or MCP, and
`tools/build_coverage.py --strict` has to stay green.

## Acknowledgements

- [qBittorrent](https://github.com/qbittorrent/qBittorrent) — and specifically
  its `src/webui/api/*controller.cpp`, which is the only complete and accurate
  description of this API that exists.
- [`qbittorrent-api`](https://pypi.org/project/qbittorrent-api/) is the mature,
  well-maintained Python library for this API, and if a library is all you
  need, use it. qbt-kit exists for the CLI, the MCP server and the verifiable
  coverage claim, not because anything is wrong with it.

## Licence

MIT — see [LICENSE](https://github.com/manueltarouca/qbt-kit/blob/main/LICENSE).
