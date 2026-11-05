# qbt-kit (fork)

This is `cmeans/qbt-kit`, a public fork of `manueltarouca/qbt-kit`. Read
`CONTRIBUTING.md` for how the project works: the endpoint surface is generated
from a spec under `src/qbtkit/spec/`, and that spec is checked against
qBittorrent's own source.

This file takes precedence over those documents where they differ:

- `CONTRIBUTING.md` and `docs/TESTING.md` give host commands such as `uv sync` and
  `uv run pytest`. Here, run their equivalents in the sandbox below.
- `SECURITY.md` sends reports to upstream's advisories. Here, reports go through
  the maintainer. See "Security reports and upstream".
- `.mcp.json` starts `uv run` on the host if an MCP client loads it. Decline it in
  this checkout.

## Running the code

The code came from another author and isn't fully audited. Run it only in a
throwaway container, never on the host, which holds real credentials:

- Mount the repo read-only.
- Never mount `$HOME`, the keyring, credential directories or the container socket.
- Give the code no network. Only the dependency install gets one, and that step
  doesn't run tests.
- Use a new virtualenv volume for each run, so code from one branch can't change
  what a later run executes. Only the install step mounts uv's shared cache.
- Cap anything that could run away: the command below has a memory limit,
  `ulimit -v` and a timeout. `/tmp` on the host is tmpfs and counts against memory.

Build the image once. The official uv images can be older than the `>=0.12` that
`pyproject.toml` requires.

```sh
podman build -t qbtk-dev - <<'EOF'
FROM docker.io/library/python:3.12-slim
RUN pip install --no-cache-dir "uv>=0.12"
EOF
```

Then run the checks. The exit code is meaningful: any failure except `ty`, which is
advisory as it is in CI, makes it nonzero. Outside a git repo it stops rather than
mounting the wrong directory.

```sh
sh -eu <<'SH'
root=$(git rev-parse --show-toplevel)
venv=qbtk-venv-$$
trap 'podman volume rm -f "$venv" >/dev/null' EXIT
box() {
  podman run --rm --security-opt label=disable --memory=4g \
    -v "$root":/work:ro -v "$venv":/venv -w /work \
    -e UV_PROJECT_ENVIRONMENT=/venv -e UV_FROZEN=1 -e PYTHONDONTWRITEBYTECODE=1 "$@"
}
box -v qbtk-uvcache:/uvcache -e UV_CACHE_DIR=/uvcache qbtk-dev uv sync --all-extras -q
box --network none qbtk-dev sh -ec '
  ulimit -v 6000000
  timeout 300 uv run --no-sync pytest -q -p no:cacheprovider tests/unit
  uv run --no-sync ruff check --no-cache src tools tests
  uv run --no-sync ruff format --no-cache --check src tools tests
  uv run --no-sync ty check src tools tests || true
  uv run --no-sync python tools/build_coverage.py --target release-5.2.3 --strict'
SH
```

`tests/contract` shells out to a container runtime, so it can't run in that
sandbox; CI runs it. To check behavior against a real server locally, start one on
a network with no way out. The login is `testuser` / `testpass123`, at
`http://qbtk-srv:8080` from a sandbox started with `--network qbtk-net`:

```sh
podman network create --internal qbtk-net
podman volume create qbtk-config
podman run --rm --security-opt label=disable --network none \
  -v "$(git rev-parse --show-toplevel)":/work:ro -v qbtk-config:/cfg -w /work \
  qbtk-dev sh -ec 'mkdir -p /cfg/qBittorrent/config
    python -I tests/contract/seed_config.py --password testpass123 \
      --out /cfg/qBittorrent/config/qBittorrent.conf'
podman run -d --name qbtk-srv --network qbtk-net -e QBT_LEGAL_NOTICE=confirm \
  -e QBT_WEBUI_PORT=8080 -v qbtk-config:/config \
  docker.io/qbittorrentofficial/qbittorrent-nox:5.2.3-1
# When finished:
podman rm -f qbtk-srv; podman network rm qbtk-net; podman volume rm qbtk-config
```

## Testing against a server

- **Only ever a throwaway container.** Never a real qBittorrent, even for a read:
  the maintainer's own instance is off limits, because it's in production.
- Use fake secrets only. Never put a real passkey or credential in a test, an
  issue, a PR or any output.
- Measure before trusting a claim about qBittorrent, including ones in this repo's
  docs and spec. The API's behavior differs by version and often from its wiki.
  CI covers 4.6, 5.0, 5.1 and 5.2.

## Security reports and upstream

- Security fixes don't get pushed to this public fork until upstream has had
  notice and the agreed date has passed. Check with the maintainer first. A
  commit message, PR or issue here is public, so a fix's description discloses
  the problem as surely as its code does.
- Upstream contact goes through the maintainer. Don't open issues or PRs upstream
  without their go-ahead.
- `gh` can resolve to the parent repo once an `upstream` remote exists, so aim it
  on purpose. Run `gh repo set-default cmeans/qbt-kit` in a new checkout, and pass
  `--repo cmeans/qbt-kit` on every `gh` write.

## Keeping fork-only files out of upstream

This file and the label workflows (`qa-gate.yml`, `pr-labels.yml`,
`pr-labels-ci.yml`) are specific to this fork, and this fork's `main` carries them.

- **Code PRs:** keep them small and suited to upstream, with a `CHANGELOG.md` entry.
  Cut the branch from the latest upstream commit, not from this fork's `main`, and
  never merge this fork's `main` into it, including with GitHub's "Update branch".
  The PR still targets this fork's `main`.
- **Fork-only PRs:** these change only the files above, and get no `CHANGELOG.md`
  entry, because it would conflict with each upstream sync.
- Before a branch is offered upstream, check that this prints nothing, where
  `<upstream commit>` is the commit it was cut from:

  ```sh
  git diff --name-only <upstream commit>...HEAD -- CLAUDE.md .github/workflows/qa-gate.yml .github/workflows/pr-labels.yml .github/workflows/pr-labels-ci.yml
  ```

## Changes that affect what a model can do

The gates live in `Client`, so `qbt_call` can't bypass them. Treat the model as
something that can be manipulated by text it reads, and remember that `confirm` is
set by the model. A new endpoint's safety flags are what make the gates correct,
so set them deliberately.
