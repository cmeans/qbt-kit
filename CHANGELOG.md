# Changelog

Notable changes to qbt-kit. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning is
[semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **MCP tools now have to name the torrents they act on.** `control_torrents`
  and `tags` add/remove used to treat omitted hashes as every torrent, with no
  confirmation. They now refuse with `hashes_required`. Every torrent takes
  `hashes=["all"]` and `confirm=true`, also for `move_torrents`, which gains
  `confirm`, and for hash lists through `qbt_call`. The refusal is
  `every_torrent_needs_confirm`. The CLI still accepts `all`.
- `tags` add/remove refuses a blank name, including `","`, and `qbt_call` refuses
  leaving `tags` out of those calls, with `tags_required`. On removal, qBittorrent
  reads any of these as every tag on the torrent.
- `mcp` is capped at `<3`, because strict tool arguments rely on private SDK
  attributes.
- **Unknown MCP tool arguments are errors.** A mistyped `hash` used to be dropped,
  which turned a call for one torrent into a call for all of them, and
  `delete_file=true` kept the files. Tool schemas now set
  `additionalProperties: false`.
- `preferences` refuses an unknown action, where `writ` used to write.
  `categories` edit requires `save_path`, because leaving it out cleared the
  path; pass `""` to clear it on purpose.

### Fixed

- `control_torrents`, `tags`, `move_torrents` and `delete_torrents` report what
  the server has rather than repeating the request. `applied_to`, `moved` and
  `removed` are counts of torrents found, or `"all"`, with the rest in
  `not_found`. If none exist, the call is refused with `unknown_torrents` and
  nothing is sent. Deleting an unknown hash
  used to report `removed: 1`, and `hashes=[]` reported `applied_to: all` while
  changing nothing.
- A list element containing `|` is refused, rather than becoming two hashes.
- A URL too long to build is a transport error, not a bare tool error.

## [0.1.3] — 2026-11-04

Two vulnerabilities reported privately by Chris Means, both affecting
`<= 0.1.2` through the MCP server. The CLI was never affected. Upgrade if you
run `qbt-mcp`, particularly against a private tracker.

### Security

- **Private-tracker passkeys reached the language model.** Redaction matched
  dict key names, and a passkey lives inside a URL, so it passed through
  `list_torrents(full=true)`, `torrent_detail`, `torrents/trackers`,
  `sync/maindata`, `app/cookies` and others. `QBT_READ_ONLY` did not help,
  because these are reads. Every MCP reply is now scrubbed in one place that
  tool registration cannot skip. Announce and scrape URLs keep only scheme,
  host and port; credential-named query parameters and path siblings go; magnet
  `tr`/`xs`/`as`/`ws` values get the same treatment; and a literal learned from
  any of those is removed everywhere else in the same reply. Tracker hostnames
  and infohashes are deliberately kept.
- **The preference gate missed several routes to deletion and code execution.**
  `max_ratio_act` with a zero limit deleted every finished torrent's data with
  no confirmation; `search/installPlugin` executed third-party Python inside
  qBittorrent; `python_executable_path` ran a file already on the host;
  `proxy_*`, `export_dir`, `scan_dirs` and `add_trackers` sent data elsewhere;
  `torrentcreator/addTask` overwrote a chosen file and announced qBittorrent's
  own configuration directory. `app/setPreferences` now takes an allowlist
  rather than a blocklist, so a key nobody anticipated fails closed.
- **Some calls are refused to a model outright**, with no `confirm` override,
  and stay available on the CLI: `search/installPlugin`, `search/updatePlugins`,
  `app/cookies`, `app/setCookies`, `torrents/SSLParameters`, and `addTask`'s
  `torrentFilePath` and `startSeeding`. `confirm` is set by the model, so it is
  evidence of intent rather than consent: the right gate for losing your own
  data, the wrong one for handing a third party code execution or a credential.
- `torrents/setShareLimits` is now destructive. Reaching a limit deletes data
  when `max_ratio_act` is 1 or 3, so it had `torrents/delete`'s outcome without
  its gate.

### Changed

- Scrubbing announce URLs is one-way. A redacted tracker URL cannot be passed
  back to `torrents/editTracker`, and a redacted `fileUrl` cannot be passed back
  to `search/downloadTorrent`, so on a private tracker those are CLI jobs now. A
  reversible token would restore them and would also let the model build a new
  URL around the token and leak through `torrents/addTrackers`.
- SECURITY.md documents a residual neither report covered: `savepath` on
  `torrents/add` and its relatives is an arbitrary file write, reachable through
  the ordinary tools and so not refusable. Run qBittorrent as a user that cannot
  write outside its own data.

## [0.1.2] — 2026-10-19

### Fixed

- `qbt-mcp` exited when qBittorrent was unreachable at startup. An MCP client
  launches its servers when the session starts, which for a desktop qBittorrent
  is routinely before the application is running, so the server vanished and
  the client reported only "connection closed" — never the one fact worth
  knowing. A stopped server and an expiring ban are now reported on stderr and
  the tools serve regardless, each reporting the failure per call. Rejected
  credentials and a refused host header stay fatal: they do not fix themselves,
  and since every tool call builds a fresh client that re-POSTs `auth/login`,
  staying up would trip qBittorrent's ban on repeated failed logins and lock
  the user out of the WebUI in their browser too.

## [0.1.1] — 2026-10-18

### Fixed

- The licence badge linked to a relative path, which resolves to nothing on
  PyPI. Badges are now Markdown rather than raw HTML, so no HTML attribute is
  left for a relative target to hide in.
- `uvx qbt-mcp` appeared twice in the README. It resolves to nothing:
  `qbt-mcp` is a console script inside `qbt-kit`, not a package. Both now read
  `uvx --from "qbt-kit[mcp]" qbt-mcp`.

### Added

- The repository is also a plugin marketplace:
  `/plugin install qbt-kit --marketplace manueltarouca/qbt-kit`.

## [0.1.0] — 2026-10-18

First public release.

### Added

- Typed client for the qBittorrent WebUI API covering **all 123 endpoints** of
  qBittorrent 5.2.3, generated from a declarative spec.
- `qbt` CLI with one command per endpoint, plus `doctor`, `health`, `ready`,
  `call` and `coverage`.
- `qbt-mcp`, an MCP server exposing 15 tools — 13 task-shaped plus `qbt_call`
  escape hatch, so the small tool surface stays complete.
- Coverage traceability: endpoint inventories extracted from the qBittorrent
  C++ source per release tag (4.1.9, 4.4.5, 5.2.3, master) with a CI gate that
  fails on both missing and invented endpoints.
- Version compatibility down to 4.4, with a rename table so `torrents/stop`
  resolves to `pause` on servers that predate the rename. 4.1 inventories are
  committed, but 4.4 is the supported floor.
- Safety gates: destructive endpoints require explicit confirmation, a
  read-only mode that holds even through `qbt_call`, and a `setPreferences`
  blocklist covering the WebUI and auth keys that could lock you out.
- Contract test suite driving real qBittorrent containers across 4.6, 5.0, 5.1
  and 5.2, including scenarios reproduced from real incidents.

### Notes

Several behaviours documented here are not in the official wiki, including that
a failed login returns HTTP 200 on qBittorrent ≤5.1, that `app/webapiVersion`
requires authentication, and that a torrent reports `progress: 1.0` while files
set to priority 0 were never downloaded. See the README and `docs/TESTING.md`.
