"""Gates that keep the coverage claim honest, and the spec internally sound.

These run without containers, so they are the fast feedback loop. The claim
"100% of the endpoints" rests on test_spec_has_no_phantoms and
test_spec_covers_target below: one proves we did not invent endpoints, the other
proves we did not miss any.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from qbtkit import compat
from qbtkit.client import encode
from qbtkit.spec import load_all
from qbtkit.spec._types import Body, ReturnKind, Wire

ROOT = pathlib.Path(__file__).resolve().parents[2]
TARGET = "release-5.2.3"
# Scopes not yet written. The gate tightens as they land; an empty set means
# the spec must cover the target completely.
PENDING_SCOPES: set[str] = set()  # the spec must now cover the target completely


def inventory(ref: str = TARGET) -> dict[str, set[str]]:
    # Ships as package data so `qbt coverage` works from an installed wheel.
    doc = json.loads((ROOT / "src" / "qbtkit" / "inventory" / f"{ref}.json").read_text())
    out: dict[str, set[str]] = {}
    for e in doc["endpoints"]:
        out.setdefault(e["scope"], set()).add(e["name"])
    return out


# --------------------------------------------------------------------------- #
# coverage gates


def test_spec_has_no_phantoms() -> None:
    """Nothing in the spec may be absent from every upstream inventory.

    A phantom inflates the numerator and makes the coverage claim a lie. This
    also catches typos in endpoint names, which would otherwise only surface as
    a 404 at runtime.
    """
    known: set[tuple[str, str]] = set()
    for ref in ("release-4.1.9", "release-4.4.5", TARGET, "master"):
        for scope, names in inventory(ref).items():
            known |= {(scope, n) for n in names}
    phantoms = [e.path for e in load_all() if (e.scope, e.name) not in known]
    assert phantoms == [], f"specced but not upstream: {phantoms}"


def test_spec_covers_target() -> None:
    """Every endpoint on the target version is specced, scope by scope."""
    inv = inventory()
    missing: list[str] = []
    for scope, names in inv.items():
        if scope in PENDING_SCOPES:
            continue
        specced = {e.name for e in load_all() if e.scope == scope}
        missing += [f"{scope}/{n}" for n in sorted(names - specced)]
    assert missing == [], f"unspecced on {TARGET}: {missing}"


def test_pending_scopes_are_real() -> None:
    """The pending list may not hide a scope that does not exist upstream."""
    unknown = PENDING_SCOPES - set(inventory())
    assert unknown == set(), f"pending scopes not in the inventory: {unknown}"


# --------------------------------------------------------------------------- #
# spec integrity


def test_every_endpoint_is_described() -> None:
    for e in load_all():
        assert e.summary, f"{e.path} has no summary"
        assert e.method in {"GET", "POST"}, f"{e.path} has method {e.method!r}"


def test_post_with_params_declares_a_body() -> None:
    for e in load_all():
        if e.method == "POST" and e.params:
            assert e.body is not Body.NONE, f"{e.path} needs a body encoding"


def test_destructive_implies_mutating() -> None:
    for e in load_all():
        if e.destructive:
            assert e.mutating, f"{e.path} is destructive but not mutating"


def test_json_returns_name_a_model() -> None:
    for e in load_all():
        if e.returns.kind is ReturnKind.JSON:
            assert e.returns.model, f"{e.path} returns JSON with no model"


def test_cli_names_are_unique_per_scope() -> None:
    seen: set[tuple[str, str]] = set()
    for e in load_all():
        key = (e.scope, e.cli_command)
        assert key not in seen, f"duplicate CLI command {key}"
        seen.add(key)


def test_untestable_endpoints_state_a_reason() -> None:
    for e in load_all():
        if e.untestable is not None:
            assert e.untestable.strip(), f"{e.path} marked untestable with no reason"


def test_renamed_params_are_in_the_compat_table() -> None:
    """A param flagged `renamed` without a mapping would silently not translate."""
    for e in load_all():
        for p in e.params:
            if p.renamed:
                assert (e.scope, e.name, p.name) in compat.PARAM_RENAMES, (
                    f"{e.path}:{p.name} flagged renamed but absent from PARAM_RENAMES"
                )


# --------------------------------------------------------------------------- #
# generation


def test_cli_command_count_equals_spec_count() -> None:
    from qbtkit.cli import SUBS

    generated = sum(len(s.registered_commands) for s in SUBS.values())
    assert generated == len(load_all())


def test_cli_command_naming() -> None:
    from qbtkit.spec._types import Endpoint

    e = Endpoint(
        scope="x",
        name="setLocation",
        method="GET",
        summary="s",
        returns=__import__("qbtkit.spec._types", fromlist=["RawText"]).RawText,
    )
    assert e.cli_command == "set-location"


# --------------------------------------------------------------------------- #
# wire encoding


@pytest.mark.parametrize(
    ("wire", "value", "expected"),
    [
        (Wire.BOOL_STR, True, "true"),
        (Wire.BOOL_STR, False, "false"),
        (Wire.HASHES, "all", "all"),
        (Wire.HASHES, ["a", "b"], "a|b"),
        (Wire.PIPE_LIST, ["x", "y"], "x|y"),
        (Wire.PIPE_LIST, "x|y", "x|y"),
        (Wire.INT, 42, "42"),
        (Wire.INT, "42", "42"),
        (Wire.FLOAT, 1.5, "1.5"),
        (Wire.JSON_BLOB, {"a": 1}, '{"a": 1}'),
        (Wire.PLAIN, "s", "s"),
    ],
)
def test_encode(wire: Wire, value: object, expected: str) -> None:
    assert encode(wire, value) == expected


# --------------------------------------------------------------------------- #
# version logic, against the measured matrix


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.9.3", "pause"),  # 4.6.7
        ("2.11.2", "stop"),  # 5.0.2
        ("2.15.1", "stop"),  # 5.2.3
    ],
)
def test_stop_translates_by_version(version: str, expected: str) -> None:
    v = compat.parse_version(version)
    assert compat.wire_name("torrents", "stop", v) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("2.9.3", False),
        ("2.11.2", False),
        ("2.11.4", False),
        ("2.15.1", True),
    ],
)
def test_api_key_support_by_version(version: str, expected: bool) -> None:
    """Measured: rotateAPIKey 404s below 2.15.1 and 200s at it.

    An earlier draft gated this at 2.11.2, which is 5.0.0, and would have sent
    Bearer to servers that ignore it.
    """
    assert compat.supports_api_keys(compat.parse_version(version)) is expected


def test_parse_version_tolerates_short_versions() -> None:
    assert compat.parse_version("2.15") == (2, 15, 0)
    assert compat.parse_version(" 2.15.1\n") == (2, 15, 1)


@pytest.mark.parametrize(
    ("camel", "expected"),
    [
        ("savePath", "save-path"),
        ("deleteFiles", "delete-files"),
        ("autoTMM", "auto-tmm"),
        ("setSSLParameters", "set-ssl-parameters"),
        ("SSLParameters", "ssl-parameters"),
        ("toggleFirstLastPiecePrio", "toggle-first-last-piece-prio"),
        ("addTags", "add-tags"),
        ("info", "info"),
    ],
    ids=lambda v: str(v),
)
def test_camel_to_kebab_keeps_acronyms_whole(camel: str, expected: str) -> None:
    """`autoTMM` is one word to a human, so `--auto-t-m-m` is unusable.

    Splitting on every capital turned acronyms into stutters: `--auto-t-m-m`,
    `set-s-s-l-parameters`. Nobody guesses those, and the README examples were
    written as a person would type them, so the docs and the CLI disagreed.
    """
    from qbtkit.spec._types import camel_to_kebab

    assert camel_to_kebab(camel) == expected


def test_readme_cli_examples_exist() -> None:
    """Every `qbt ...` line in the README must name a real command.

    The README shipped `qbt torrents list` and a positional magnet for `add`;
    neither existed. Examples are the first thing a newcomer copies, so a wrong
    one costs more trust than a missing one.
    """
    import pathlib
    import re

    readme = pathlib.Path(__file__).resolve().parents[2] / "README.md"
    endpoints = load_all()
    scopes = {e.scope for e in endpoints}
    commands = {(e.scope, e.cli_command) for e in endpoints}
    top_level = {"doctor", "call", "coverage", "health", "ready"}

    for line in readme.read_text().splitlines():
        m = re.match(r"^qbt ([a-z0-9-]+)(?: ([a-z0-9-]+))?", line.strip())
        if not m:
            continue
        first, second = m.group(1), m.group(2)
        if first in top_level:
            continue
        assert first in scopes, f"README names unknown scope {first!r}: {line}"
        assert (first, second) in commands, (
            f"README names unknown command {first} {second!r}: {line}"
        )


@pytest.mark.parametrize(
    ("value", "old_server", "expected"),
    [
        ("stopped", True, "paused"),
        ("stopped", False, "stopped"),
        ("resumed", True, "resumed"),
        ("running", True, "resumed"),
        ("running", False, "running"),
        ("downloading", True, "downloading"),
        ("all", True, "all"),
    ],
    ids=lambda v: str(v),
)
def test_filter_values_are_translated_like_names(
    value: str, old_server: bool, expected: str
) -> None:
    """`filter=stopped` returns nothing on 4.x, where the value is `paused`.

    The 5.0 rename hit filter *values* as well as endpoint names, but only
    names were translated. The failure is quiet in the worst way: the server
    accepts the unknown filter and returns an empty list, so a caller on 4.6
    sees "no stopped torrents" rather than an error.
    """
    from qbtkit.compat import wire_filter_value

    version = (2, 8, 0) if old_server else (2, 11, 0)
    assert wire_filter_value(value, version) == expected


def test_readme_never_tells_people_to_run_a_package_that_does_not_exist() -> None:
    """`uvx qbt-mcp` resolves to nothing and must not appear as an instruction.

    `qbt-mcp` is a console script inside `qbt-kit`, not a package, so a bare
    `uvx qbt-mcp` fails with "not found in the package registry". That wrong
    command sat in the README through a release and reached the PyPI page,
    where it is the first thing a new user copies. The rule is mechanical, so
    a test can hold it: any `uvx` line naming qbt-mcp has to go through
    `--from`.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    for doc in (root / "README.md", root / "skills" / "qbt-kit" / "SKILL.md"):
        if not doc.exists():
            continue
        for n, line in enumerate(doc.read_text().splitlines(), 1):
            stripped = line.strip().lstrip("$ ")
            if not stripped.startswith("uvx "):
                continue
            if "qbt-mcp" in stripped or "qbt " in stripped:
                assert "--from" in stripped, (
                    f"{doc.name}:{n} runs a console script without --from, which "
                    f"cannot resolve: {stripped!r}"
                )


def test_readme_has_no_relative_links_for_pypi() -> None:
    """PyPI renders the README with no repository to resolve paths against.

    A relative target that works on GitHub resolves to nothing on the project
    page, so the logo shows as alt text and document links 404. An earlier pass
    absolutised Markdown `[text](target)` links but not `href=`/`src=` inside
    raw HTML, and `<a href="LICENSE">` reached a published release that way.
    Both forms are checked here because both end up in the same description.
    """
    import pathlib
    import re

    readme = (pathlib.Path(__file__).resolve().parents[2] / "README.md").read_text()
    offenders: list[str] = []

    # Markdown: [text](target)
    offenders += [
        t
        for t in re.findall(r"\[[^\]]*\]\(([^)]+)\)", readme)
        if not t.startswith(("http://", "https://", "#"))
    ]
    # Raw HTML attributes the Markdown pass does not see.
    offenders += [
        t
        for t in re.findall(r'(?:href|src|srcset)="([^"]+)"', readme)
        if not t.startswith(("http://", "https://", "#"))
    ]

    assert not offenders, (
        f"relative targets resolve to nothing on PyPI: {sorted(set(offenders))}"
    )
