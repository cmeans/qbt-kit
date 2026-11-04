# qbt-kit (fork)

This is `cmeans/qbt-kit`, a public fork of `manueltarouca/qbt-kit`. Read
`CONTRIBUTING.md` for how the project works: the endpoint surface is generated
from a spec under `src/qbtkit/spec/`, and that spec is checked against
qBittorrent's own source.

## Running the code

The code came from another author and isn't fully audited. Run it only in a
throwaway container, never on the host, which holds real credentials:

- Mount the repo read-only. Use named volumes for the virtualenv and uv's cache.
- Never mount `$HOME`, the keyring, credential directories or the container socket.
- Set `UV_PROJECT_ENVIRONMENT` to a path in a volume, and `UV_FROZEN=1`.
- Cap anything that could run away: a container memory limit, `ulimit -v` and a
  timeout. `/tmp` on the host is tmpfs and counts against memory.

```sh
podman run --rm --security-opt label=disable --memory=4g \
  -v "$PWD":/work:ro -v qbtk-venv:/venv -v qbtk-uvcache:/uvcache -w /work \
  -e UV_PROJECT_ENVIRONMENT=/venv -e UV_CACHE_DIR=/uvcache -e UV_FROZEN=1 \
  -e PYTHONDONTWRITEBYTECODE=1 <image with python and uv> sh -c '
    uv sync --all-extras -q
    uv run --no-sync pytest -q -p no:cacheprovider tests/unit
    uv run --no-sync ruff check --no-cache src tools tests
    uv run --no-sync ruff format --no-cache --check src tools tests
    uv run --no-sync ty check src tools tests
    uv run --no-sync python tools/build_coverage.py --target release-5.2.3 --strict'
```

`tests/contract` shells out to a container runtime, so it can't run in that
sandbox; CI runs it. To check behavior against a real server locally, start a
`qbittorrentofficial/qbittorrent-nox` container on a `podman network create
--internal` network, and run the sandbox on the same network. Seed its login with
`tests/contract/seed_config.py`.

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
- Keep PRs small and suited to upstream. This file and the label workflows
  (`qa-gate.yml`, `pr-labels.yml`, `pr-labels-ci.yml`) are specific to this fork,
  so leave them out of anything offered upstream.

## Changes that affect what a model can do

The gates live in `Client`, so `qbt_call` can't bypass them. Treat the model as
something that can be manipulated by text it reads, and remember that `confirm` is
set by the model. A new endpoint's safety flags are what make the gates correct,
so set them deliberately.
