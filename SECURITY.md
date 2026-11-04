# Security

## Reporting a vulnerability

Please report privately through
[GitHub's security advisories](https://github.com/manueltarouca/qbt-kit/security/advisories/new)
rather than opening a public issue. Include what an attacker gains and the
smallest reproduction you have; a proof-of-concept is welcome but not required.

Expect an acknowledgement within a few days. This is a personal project, not a
funded one, so please size your expectations accordingly — but anything that
lets a caller delete data or escape a gate will be treated as urgent.

## What this tool holds

qbt-kit authenticates to a qBittorrent WebUI with either an API key
(qBittorrent 5.2+) or a username and password. **qBittorrent has no OAuth**, so
there is no short-lived token to delegate: the secret is long-lived and grants
full control of the torrent client, including where it writes files.

Credentials resolve from flags, then the environment, then the macOS Keychain.
Prefer the Keychain. An MCP client's `env` block is plaintext and frequently
committed, so if you use it, reference a variable rather than pasting a secret.

Two deliberate behaviours:

- **Credentials are never printed.** Startup reports the *source* of the
  credential, never its value. `httpx` DEBUG logging would print `Authorization`
  and `Cookie` headers, so each client attaches a filter that drops any record
  carrying them.
- **`app/preferences` is redacted.** qBittorrent returns its own API key and
  several passwords in that response. Both the `preferences` tool and the raw
  `qbt_call` strip them, because returning a secret into a model's context puts
  it in transcripts and logs that no later instruction can recall.

## Threat model for the MCP server

The MCP server is driven by a language model, which may be acting on text it
read somewhere — a torrent name, an RSS title, a web page. Treat tool arguments
as untrusted and assume the model can be talked into asking for anything the
API allows. The gates are therefore code, not prompt text:

- Destructive endpoints require explicit `confirm`; `delete_files` is never
  implied.
- `--read-only` / `QBT_READ_ONLY=1` refuses every mutating endpoint, **including
  through `qbt_call`**, so the escape hatch cannot be used to escape the gate.
- `app/setPreferences` takes an **allowlist**, `SAFE_PREF_KEYS`. Keys outside it
  are refused on the MCP path and warn on the CLI. It is an allowlist because
  the blocklist it replaced was careful and still missed three routes to code
  execution and mass deletion; an allowlist fails closed on keys qBittorrent has
  not shipped yet. `LOCKOUT_KEYS` stays as a set refused on every path with no
  override — WebUI credentials and reachability, `autorun_program`, and
  `python_executable_path`. A contract test asserts every key in both sets
  exists in the server's own preferences, because a misspelled entry silently
  guards nothing.
- Some calls are refused to a model with **no override at all**
  (`AgentForbidden`), and stay available on the CLI: `search/installPlugin` and
  `updatePlugins` (they execute third-party Python inside qBittorrent),
  `app/cookies`, `app/setCookies`, `torrents/SSLParameters`, and the
  `torrentFilePath` and `startSeeding` parameters of `torrentcreator/addTask`.

`confirm` is the right gate for irreversible loss of **your own** data, which
you can be asked about. It is the wrong gate for handing a third party code
execution, a credential, or a redirect, because the model sets `confirm` itself
— it is evidence of intent, not of consent. That distinction is why the list
above exists rather than everything being merely destructive.

### Credentials in tool output

A private-tracker passkey is an account credential and it lives inside URLs, not
in a field named `password`. Every MCP reply is therefore scrubbed on the way
out, in one place that tool registration cannot skip: announce and scrape URLs
keep only scheme, host and port; credential-named query parameters and path
siblings go; magnet `tr`/`xs`/`as`/`ws` values get the same treatment; and any
literal learned from one of those places is removed everywhere else in the same
reply, which is how a passkey in a `comment` field is caught without guessing at
randomness. Tracker hostnames and infohashes are kept deliberately — they are
how you tell one torrent from another.

**This is one-way.** A scrubbed announce URL cannot be handed back to
`torrents/editTracker`, and a scrubbed `fileUrl` cannot be handed back to
`search/downloadTorrent`. On a private tracker those are CLI jobs. A reversible
token would restore them, but a token standing for part of a URL would let the
model assemble a new URL around it and send the secret through
`torrents/addTrackers`, which is worse than the leak it fixes.

What the gates do **not** protect against:

- A caller with legitimate confirmation deleting data they meant to keep.
- Anything qBittorrent itself permits once a preference has been set outside
  this tool.
- **Writing a file anywhere qBittorrent can write**, via `savepath` on
  `torrents/add`, `setLocation`, `setSavePath`, `setDownloadPath`,
  `createCategory` or an `rss` rule. Attacker-chosen content plus an
  attacker-chosen directory is a file write, and libtorrent overwrites data that
  does not match. These are reachable through the ordinary `add_torrent` and
  `move_torrents` tools, so they cannot simply be refused. Run qBittorrent as a
  user that cannot write outside its own data, which is worth doing regardless
  of this tool.

## Transports

`qbt-mcp` speaks stdio by default, which is the safe choice: the server is a
child process of the client and nothing listens on a socket. The `sse` and
`streamable-http` transports expose an **unauthenticated** control surface
carrying your qBittorrent credentials — the MCP SDK binds them to `127.0.0.1`,
so reaching them means reaching your machine, but do not forward those ports or
bind them to an external interface.

## Supply chain

Runtime dependencies are deliberately few — `httpx` and `typer`, plus `mcp` and
`pydantic` for the optional server — and each is justified in
[`docs/dependencies.md`](docs/dependencies.md). Releases are built and published
by GitHub Actions through PyPI trusted publishing, so no long-lived PyPI token
exists to steal.
