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
- `README.md` and `skills/qbt-kit/SKILL.md` give `uvx` commands, which install the
  published package on the host. They're for people using a release. Don't use them
  to run or test anything here.

## Running the code

The code came from another author and isn't fully audited. Run it only in a
throwaway container, never on the host, which holds real credentials:

- Mount the repo read-only.
- Never mount `$HOME`, the keyring, credential directories or the container socket.
- Give the code no network. Only the dependency install gets one. That step skips
  the project, so a branch's `[build-system]` backend only runs in the offline step.
- Use a new virtualenv volume for each run, so code from one branch can't change
  what a later run executes. Only the dependency install mounts uv's shared cache.
- Read any change to `pyproject.toml` and `uv.lock` before running a branch. A
  dependency built from an sdist runs its build code during the dependency install,
  with the network and the shared cache.
- Cap anything that could run away: every step below that runs this repo's code has
  a memory limit, `ulimit -v` and a timeout. `/tmp` on the host is tmpfs and counts
  against memory.

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
box -v qbtk-uvcache:/uvcache -e UV_CACHE_DIR=/uvcache qbtk-dev \
  timeout 600 uv sync --all-extras -q --no-install-project
box --network none qbtk-dev timeout 600 sh -ec '
  ulimit -v 6000000
  uv sync --all-extras -q --offline
  uv run --no-sync pytest -q -p no:cacheprovider tests/unit
  uv run --no-sync ruff check --no-cache src tools tests
  uv run --no-sync ruff format --no-cache --check src tools tests
  uv run --no-sync ty check src tools tests || true
  uv run --no-sync python tools/build_coverage.py --target release-5.2.3 --strict'
SH
```

`tests/contract` shells out to a container runtime, so it can't run in that
sandbox; CI runs it. To check behavior against a real server locally, run this. It
starts a throwaway server on a network with no way out, runs this project's code
against it, and removes everything on exit. Names include the shell's PID, because
Dev and QA share a podman account. Replace the `qbt doctor` line with what you want
to run; `QBT_URL`, `QBT_USERNAME` and `QBT_PASSWORD` are already set.

`SIGKILL` skips that cleanup, and a tool that kills a timed-out command can send it.
Leftovers are named `qbtk-<pid>-*` (the checks command's is `qbtk-venv-<pid>`). Remove
your own by that PID, and never all `qbtk-*`, because another session may be using them.

```sh
sh -eu <<'SH'
root=$(git rev-parse --show-toplevel)
id=qbtk-$$
srv=$id-srv net=$id-net cfg=$id-cfg venv=$id-venv
trap 'podman rm -f -t 0 "$srv" >/dev/null 2>&1 || true
  podman volume rm -f "$cfg" "$venv" >/dev/null
  podman network rm "$net" >/dev/null 2>&1 || true' EXIT
box() {
  podman run --rm --security-opt label=disable --memory=4g \
    -v "$root":/work:ro -v "$venv":/venv -w /work \
    -e UV_PROJECT_ENVIRONMENT=/venv -e UV_FROZEN=1 -e PYTHONDONTWRITEBYTECODE=1 "$@"
}
podman network create --internal "$net" >/dev/null
podman run --rm --security-opt label=disable --network none --memory=4g \
  -v "$root":/work:ro -v "$cfg":/cfg -w /work \
  qbtk-dev timeout 60 sh -ec 'ulimit -v 6000000
    mkdir -p /cfg/qBittorrent/config
    python -I tests/contract/seed_config.py --password testpass123 \
      --out /cfg/qBittorrent/config/qBittorrent.conf'
podman run -d --name "$srv" --network "$net" -e QBT_LEGAL_NOTICE=confirm \
  -e QBT_WEBUI_PORT=8080 -v "$cfg":/config \
  docker.io/qbittorrentofficial/qbittorrent-nox:5.2.3-1 >/dev/null
box -v qbtk-uvcache:/uvcache -e UV_CACHE_DIR=/uvcache qbtk-dev \
  timeout 600 uv sync --all-extras -q --no-install-project
box --network "$net" -e QBT_URL="http://$srv:8080" \
  -e QBT_USERNAME=testuser -e QBT_PASSWORD=testpass123 qbtk-dev timeout 600 sh -ec '
  ulimit -v 6000000
  uv sync --all-extras -q --offline
  python -Ic "
import os, time, urllib.request
for _ in range(60):
    try:
        urllib.request.urlopen(os.environ[\"QBT_URL\"], timeout=3)
        break
    except OSError:
        time.sleep(1)
"
  uv run --no-sync qbt doctor
'
SH
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

- **Code PRs:** branch from this fork's `main`, and keep them small and suited to
  upstream, with a `CHANGELOG.md` entry. A change meant to stay on this fork is a
  code PR that never gets offered, and it gets an entry too.
- **Fork-only PRs:** these change only the files above, and get no `CHANGELOG.md`
  entry, because nothing changes for someone using the package.
- **Offering a change upstream:** only when the maintainer asks, build a separate
  branch from upstream with that change's own commits. Conflicts surface here, for
  a person to resolve. The branch has no copy of this file, so don't start new work
  on it. If the cherry-pick stops on a conflict, resolve it and run
  `git cherry-pick --continue`.

  ```sh
  sh -eu <<'SH'
  git fetch https://github.com/manueltarouca/qbt-kit.git main
  base=$(git rev-parse FETCH_HEAD)
  echo "base=$base"
  git switch -c offer/<name> "$base"
  git cherry-pick <commits>
  SH
  ```

  Run the checks above on that branch, then this, with the `base` it printed. It must
  exit 0. It exits 1 if fork-only files came along, and with another code if a ref is
  bad.

  ```sh
  git diff --exit-code --name-only <base>...HEAD -- CLAUDE.md .github/workflows/qa-gate.yml .github/workflows/pr-labels.yml .github/workflows/pr-labels-ci.yml
  ```

Fetching by URL avoids adding an `upstream` remote, which `gh` can resolve to.

## Syncing from upstream

This fork's `main` differs from upstream, and required checks block pushing to it, so
a sync is a PR. Upstream's code is outside code: read the incoming diff before
running it, especially workflows, `pyproject.toml`, `uv.lock` and test bootstrap files.

```sh
sh -eu <<'SH'
git fetch origin main
git fetch https://github.com/manueltarouca/qbt-kit.git main
git switch -c sync/<upstream version> origin/main
git merge FETCH_HEAD
SH
```

The upstream fetch has to come last, because each fetch writes over `FETCH_HEAD`.

`CHANGELOG.md` conflicts are expected, because code PRs add entries here and upstream
adds release sections. Resolve them by hand: keep upstream's sections, and keep this
fork's entries under `[Unreleased]` except any that upstream has now released. Merge
the PR with a merge commit. A squash drops upstream's history, so the next sync hits
the same conflicts again.

## Changes that affect what a model can do

The gates live in `Client`, so `qbt_call` can't bypass them. Treat the model as
something that can be manipulated by text it reads, and remember that `confirm` is
set by the model. A new endpoint's safety flags are what make the gates correct,
so set them deliberately.
