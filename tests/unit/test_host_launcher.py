"""host_launcher: what install's entries and hooks run, and how hooks are quoted.

Why this exists. Review round 3 of the S4 install work, 2026-09-23:

- The shape check accepted any uvx entry with ``alice-memory`` and ``mcp``
  somewhere in its args, so ``uvx mcp-proxy mcp --name alice-memory`` counted
  as install's own.
- The script search took ``alice-memory`` and ``alice-memory-session-start``
  from wherever each was found first, looked on PATH before the running
  install, never looked in the pip --user scripts dir, and would write a
  path inside a uv cache, which uv may delete.
- Hook commands were built with an f-string, so a space, quote, ``$`` or
  ``;`` in a path changed what the host's shell ran.

How they escaped: the round-2 tests seeded only entries install writes
itself, let this machine's PATH decide the launcher search, and never built
a uv cache layout, so no test met a look-alike entry or a cached script.

Review round 4, 2026-09-23, found more in the same places, fixed as
classes rather than cases: a script in a uv cache was kept and got a hook
into the cache (P1); hooks were read back with a "backslash means Windows"
rule that shlex never follows, so a quoted hook duplicated or churned (S2);
a data dir relying on shell expansion was trusted (S3); a script missing
its session-start sibling counted as live (S4); the hook dropped the
entry's uvx options (S5); ``alice-memory@latest`` counted as pinned; caches
set by uv.toml or --cache-dir were missed; and Windows let through words
Git Bash or PowerShell rewrite.

These tests pin each rule on synthetic trees. No binary is run here; the
shell tests live in test_hook_shell_quoting.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from alicebot_api import host_launcher
from alicebot_api.host_launcher import (
    UV_TEMP_ENV_WARNING,
    UVX_MISSING_WARNING_PREFIX,
    find_launcher,
    format_command,
    hook_command,
    in_uv_cache,
    is_session_start_command,
    launcher_in_uv_cache,
    launcher_problem,
    parse_launcher,
    read_hook_data_dir,
    script_launcher,
    split_command,
)
from tests.unit.launcher_helpers import executable, make_scripts, pin_launcher_search


# --- shape (review 5) ------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["alice-memory", "mcp"],
        ["alice-memory===0.16.0", "mcp"],
        ["alice-memory==0.16.0", "mcp", "--data-dir", "/v"],
        ["alice-memory!=0.15.0", "mcp"],
        ["alice-memory~=0.16", "mcp"],
        ["alice_memory[extra]>=0.15", "mcp"],
        ["alice-memory<=0.17", "mcp"],
        ["alice-memory>0.15", "mcp"],
        ["alice-memory<0.17", "mcp"],
        ["alice-memory@0.16.0", "mcp"],
        ["--from", "alice-memory==0.16.0", "alice-memory", "mcp"],
        ["--from=alice-memory", "alice-memory", "mcp"],
        ["--python", "3.12", "-q", "alice-memory", "mcp"],
    ],
)
def test_uvx_shapes_install_writes_or_a_user_might_pin_are_accepted(args: list[str]) -> None:
    """One case per spec operator (=== == != ~= >= <= > < @), and --from.

    A comparison operator must be followed by a version that starts with a
    digit, so dropping ``>=`` cannot fall back to ``>`` plus "=0.15".
    Mutation: drop any one operator, or --from support. A case fails.
    """

    parsed = parse_launcher({"command": "uvx", "args": args})
    assert parsed is not None, args
    launcher, rest = parsed
    assert launcher.kind == "uvx"
    assert launcher.prefix[-1] == "mcp"
    assert [*launcher.prefix, *rest] == args


@pytest.mark.parametrize(
    "args",
    [
        ["mcp-proxy", "mcp", "--name", "alice-memory"],
        ["alice-memory-foo", "mcp"],
        ["alice-memoryx", "mcp"],
        ["my-alice-memory", "mcp"],
        ["alice-memory", "serve", "mcp"],
        ["alice-memory"],
        ["--from", "other-pkg", "alice-memory", "mcp"],
        ["--from", "alice-memory", "other", "mcp"],
        ["other", "alice-memory", "mcp"],
    ],
)
def test_look_alike_uvx_entries_are_not_install_shaped(args: list[str]) -> None:
    """``uvx mcp-proxy mcp --name alice-memory`` and similar are someone else's.

    Mutation: accept any args holding alice-memory and mcp (the round-2
    check). This test fails.
    """

    assert parse_launcher({"command": "uvx", "args": args}) is None, args


@pytest.mark.parametrize(
    "args",
    [
        ["alice-memory", "mcp"],
        ["alice_memory", "mcp"],
        ["ALICE-MEMORY", "mcp"],
        ["alice-memory@latest", "mcp"],
        ["--from", "alice-memory", "alice-memory", "mcp"],
        ["--from=alice_memory@latest", "alice-memory", "mcp"],
    ],
)
def test_the_default_spelled_another_way_is_not_customised(args: list[str]) -> None:
    """Review round 4 P3: ``alice-memory@latest`` and friends counted as pinned.

    Mutation: count any prefix other than ``alice-memory mcp`` as customised
    (the round-3 rule). A case fails.
    """

    parsed = parse_launcher({"command": "uvx", "args": args})
    assert parsed is not None
    assert parsed[0].customisation is None


@pytest.mark.parametrize(
    ("args", "words"),
    [
        (["alice-memory==0.16.0", "mcp"], "asks for alice-memory==0.16.0"),
        (["alice-memory[extra]", "mcp"], "asks for alice-memory[extra]"),
        (["--index-url", "https://x/simple", "alice-memory", "mcp"], "sets uvx options --index-url https://<hidden>"),
        (["--prerelease", "allow", "alice-memory>=1", "mcp"], "sets uvx options --prerelease allow and asks for alice-memory>=1"),
    ],
)
def test_real_choices_are_customised_and_named(args: list[str], words: str) -> None:
    """A version constraint, extras or a uvx option is the user's; the receipt says which.

    Mutation: drop the options check or the spec check. A case fails.
    """

    parsed = parse_launcher({"command": "uvx", "args": args})
    assert parsed is not None
    assert parsed[0].customisation == words


@pytest.mark.parametrize(
    ("args", "hook"),
    [
        (
            ["--prerelease", "allow", "--index-url", "https://x/simple", "--python", "3.12", "alice-memory==1.0", "mcp"],
            ["uvx", "--prerelease", "allow", "--index-url", "https://x/simple", "--python", "3.12", "--from", "alice-memory==1.0", "alice-memory-session-start"],
        ),
        (
            ["--python", "3.12", "--from", "alice-memory==1.0", "alice-memory", "mcp"],
            ["uvx", "--python", "3.12", "--from", "alice-memory==1.0", "alice-memory-session-start"],
        ),
        (["alice-memory", "mcp"], ["uvx", "--from", "alice-memory", "alice-memory-session-start"]),
    ],
    ids=["options-then-spec", "options-then-from", "plain"],
)
def test_the_hook_carries_the_entrys_uvx_options(args: list[str], hook: list[str]) -> None:
    """Review round 4 S5: the hook resolves the same version from the same index.

    Every uvx option before the spec goes before ``--from <spec>``.
    Mutation: drop the options from the hook argv. This test fails.
    """

    parsed = parse_launcher({"command": "uvx", "args": args})
    assert parsed is not None
    assert parsed[0].hook_argv() == hook


def test_masked_args_hides_url_credentials_and_secret_flag_values() -> None:
    """What a paste hides, and the words the keep line uses for it.

    Since the round-7 ruling every URL shows only its scheme. Mutation: drop
    one of key, token, secret or password from the flag words, or show more
    of a URL than its scheme. This test fails.
    """

    shown, hidden = host_launcher.masked_args(
        [
            "--index-url", "https://u:p@pkgs.example/simple",
            "--find-links=https://files.example/w?sig=s",
            "--api-key", "k", "--token=t", "--client-secret", "s", "--password", "p",
            "--python", "3.12",
        ]
    )
    assert shown == [
        "--index-url", "https://<hidden>",
        "--find-links=https://<hidden>",
        "--api-key", "<hidden>", "--token=<hidden>", "--client-secret", "<hidden>",
        "--password", "<hidden>", "--python", "3.12",
    ]
    url = "a URL (everything after its scheme)"
    assert hidden == [
        url, url, "the value of --api-key", "the value of --token",
        "the value of --client-secret", "the value of --password",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://pypi.org/simple",
        "https://dl.cloudsmith.io/AbCdEf0123456789/acme/repo/python/simple/",
        "https://pypi.fury.io/A1b2C3d4E5f6G7h8I9j0/acme/",
        "https://pkgs.example/abcdefghijklmnopqrstuvwxyz/simple/",
        "https://tok9secret.pkgs.example/simple/",
        "https://u:p'w@pkgs.example/simple",
        "https://u:p w@pkgs.example/simple",
        "file:///srv/wheels",
    ],
    ids=["pypi", "cloudsmith-16", "gemfury-20", "letters-only", "host-label", "quote", "space", "file"],
)
def test_every_url_prints_as_its_scheme_and_hidden(url: str) -> None:
    """Round 7 ruling: no content test can tell a token from a repo name, so stop trying.

    Every URL, in an argv word or in free text, prints as scheme://<hidden>,
    host included. Mutation: keep the host, or any part after the scheme.
    A case fails.
    """

    scheme = url.split("://", 1)[0]
    assert host_launcher.mask_url(url) == f"{scheme}://<hidden>"
    assert host_launcher.masked_args(["--index-url", url])[0] == ["--index-url", f"{scheme}://<hidden>"]
    assert host_launcher.masked_args([f"--index-url={url}"])[0] == [f"--index-url={scheme}://<hidden>"]
    assert url.split("://", 1)[1][:12] not in host_launcher.mask_text(f"see {url} now")


@pytest.mark.parametrize(
    ("command", "windows", "expected"),
    [
        (
            'uvx  --from "alice-memory" alice-memory-session-start --data-dir /old',
            False,
            "uvx  --from \"alice-memory\" alice-memory-session-start --data-dir '/new dir'",
        ),
        (
            "/v/alice-memory-session-start --data-dir=/old # note",
            False,
            "/v/alice-memory-session-start '--data-dir=/new dir' # note",
        ),
        (
            "C:\\x\\alice-memory-session-start.exe --data-dir C:\\old",
            True,
            'C:\\x\\alice-memory-session-start.exe --data-dir "C:/new dir"',
        ),
    ],
    ids=["posix-spacing-and-quotes-kept", "equals-form-comment-kept", "windows"],
)
def test_replace_hook_data_dir_changes_only_the_value(
    command: str, windows: bool, expected: str
) -> None:
    """Review round 6 P2-A: a kept hook moves its --data-dir and nothing else.

    Mutation: rebuild the command from its words (shlex.join), which
    rewrites the launcher's spacing and quoting. A case fails.
    """

    new = "C:\\new dir" if windows else "/new dir"
    assert host_launcher.replace_hook_data_dir(command, new, windows=windows) == (expected, None)


def test_replace_hook_data_dir_needs_a_data_dir_word() -> None:
    assert host_launcher.replace_hook_data_dir("x --flag y", "/n", windows=False) == (
        None,
        "the hook has no --data-dir",
    )


def test_mask_text_masks_a_url_to_the_end_of_its_word() -> None:
    """Round 8: RFC 3986 allows ' and ) in user info, so neither may end a URL.

    Round 6 stopped a text URL at ' or ) to keep the punctuation after it;
    a password holding either then leaked its tail. Mutation: stop a text
    URL at ) or '. This test fails.
    """

    assert host_launcher.mask_text("see https://me:p)w-SECRET@h.example/x now") == "see https://<hidden> now"
    assert host_launcher.mask_text("see https://me:p'w-SECRET@h.example/x now") == "see https://<hidden> now"
    assert host_launcher.mask_text("see (https://u:p@h.example/x); next") == "see (https://<hidden> next"
    # install's own masked URL keeps the punctuation of its message; anything more is masked.
    assert host_launcher.mask_text("(a https://<hidden>), then") == "(a https://<hidden>), then"
    assert host_launcher.mask_text("see https://<hidden>:tok@h.example now") == "see https://<hidden> now"


@pytest.mark.parametrize(
    ("args", "index_words", "off_list", "carried"),
    [
        (["--prerelease", "allow", "--python", "3.12"], [], [], ["--prerelease", "allow", "--python", "3.12"]),
        (["-p3.12", "--python-preference=managed"], [], [], ["-p3.12", "--python-preference=managed"]),
        (["--native-tls", "--offline", "--no-cache", "--refresh"], [], [], ["--native-tls", "--offline", "--no-cache", "--refresh"]),
        (["--with", "alice-memory==0.15.7"], [], ["--with alice-memory==0.15.7"], []),
        (["-walice-memory==0.15.7"], [], ["-walice-memory==0.15.7"], []),
        (["--exclude-newer", "2025-01-01"], [], ["--exclude-newer 2025-01-01"], []),
        (["-q"], [], ["-q"], []),
        (["--index-url", "https://pypi.org/simple"], ["--index-url https://<hidden>"], [], []),
        (["--find-links", "./wheels"], ["--find-links ./wheels"], [], []),
        (["-fhttps://files.example/w"], ["-fhttps://<hidden>"], [], []),
        (["--with", "https://files.example/a.whl"], ["--with https://<hidden>"], [], []),
        (["--with-editable=file:///src/alice"], ["--with-editable=file://<hidden>"], [], []),
    ],
    ids=[
        "carried-values", "carried-attached-and-equals", "carried-flags", "with-pin", "w-attached",
        "exclude-newer", "quiet", "index-url", "find-links-local", "f-attached-url", "with-url",
        "with-editable-url",
    ],
)
def test_options_are_carried_only_from_the_allowlist(
    args: list[str], index_words: list[str], off_list: list[str], carried: list[str]
) -> None:
    """Round 7 ruling: carry --prerelease, --python/-p, --python-preference and four flags.

    An index option, or any word holding scheme://, is an index case; any
    other option is off the list. Mutation: carry an index option, a URL
    value or an off-list option, or read an attached short option as a flag.
    A case fails.
    """

    parsed = parse_launcher({"command": "uvx", "args": [*args, "alice-memory", "mcp"]})
    assert parsed is not None
    launcher = parsed[0]
    assert launcher.uncarried_options == (index_words, off_list)
    assert launcher.carry_options == carried


def test_launcher_descriptions_hide_credentials() -> None:
    """describe() and customisation are what launcher lines print; neither shows a secret.

    Mutation: build describe() from the raw prefix. This test fails.
    """

    parsed = parse_launcher(
        {"command": "uvx", "args": ["--index-url", "https://u:tok@pkgs.example/simple", "alice-memory==1.0", "mcp"]}
    )
    assert parsed is not None
    launcher = parsed[0]
    assert "tok" not in launcher.describe() and "tok" not in (launcher.customisation or "")
    assert launcher.describe() == "uvx --index-url https://<hidden> alice-memory==1.0 mcp"
    assert launcher.uncarried_options == (["--index-url https://<hidden>"], [])


@pytest.mark.parametrize(
    ("spec", "answer"),
    [
        ("alice-memory", "yes"),
        ("alice-memory@latest", "yes"),
        ("alice-memory==0.15.7", "no"),
        ("alice-memory==0.16.0", "yes"),
        ("alice-memory===0.15.7", "no"),
        ("alice-memory===0.16.0", "yes"),
        ("alice-memory@0.15.3", "no"),
        ("alice-memory@0.16.1", "yes"),
        ("alice-memory<0.16", "no"),
        ("alice-memory<0.16.1", "yes"),
        ("alice-memory<=0.15.9", "no"),
        ("alice-memory<=0.16.0", "yes"),
        ("alice-memory<=0.16.0rc1", "no"),
        ("alice-memory~=0.15.0", "no"),
        ("alice-memory~=0.15", "yes"),
        ("alice-memory~=0.16.0", "yes"),
        ("alice-memory>=0.1", "yes"),
        ("alice-memory>0.15", "yes"),
        ("alice-memory!=0.16.0", "yes"),
        ("alice-memory==0.15.*", "no"),
        ("alice-memory==0.16.*", "yes"),
        ("alice-memory==0.*", "yes"),
        ("alice-memory>=0.15,<0.16", "no"),
        ("alice-memory>=0.15,<0.17", "yes"),
        ("alice-memory<0.16.0.post1", "yes"),
        ("alice-memory<0.16.0.post0", "yes"),
        ("alice-memory==0.16.0.post1", "yes"),
        ("alice-memory<0.16.0.dev1", "no"),
        ("alice-memory==0.16.0.dev3", "no"),
        ("alice-memory==0.16.0rc1", "no"),
        ("alice-memory!=0.16.*", "unknown"),
        ("alice-memory===weird", "unknown"),
        ("alice-memory~=1", "unknown"),
    ],
)
def test_session_start_support_reads_each_operator(spec: str, answer: str) -> None:
    """Round 5 P2-1: can the spec resolve to 0.16.0 or later, where the script first shipped?

    Mutation: change any one operator's rule (for example ``<`` as ``<=``,
    or ``~=`` without its upper bound). A case fails.
    """

    assert host_launcher.session_start_support(spec) == answer


def test_script_shapes() -> None:
    """The installed script with ``mcp`` first is install's; anything else is not.

    Mutation: accept ``mcp`` anywhere in the args. This test fails.
    """

    assert parse_launcher({"command": "/v/bin/alice-memory", "args": ["mcp", "--data-dir", "/d"]})
    assert parse_launcher({"command": "C:\\P\\alice-memory.exe", "args": ["mcp"]})
    assert parse_launcher({"command": "/v/bin/alice-memory", "args": ["serve", "mcp"]}) is None
    assert parse_launcher({"command": "/v/bin/alice-memory-x", "args": ["mcp"]}) is None


# --- uv cache (ruling A2) ---------------------------------------------------------------


def test_archive_layout_is_a_uv_cache(tmp_path: Path) -> None:
    """uv's layout: a bucket, an id, more path, under a root uv would own.

    Round 4 moved to "any archive-vN or environments-vN with an id after
    it", which the round-4 review showed also matched a user's Archive-V2
    folder and a project venv under environments-v3. Round 5: the bucket's
    parent must be named uv, be a known root, or hold uv's CACHEDIR.TAG,
    which is how a --cache-dir cache elsewhere is found. Mutation: drop the
    parent rule, the CACHEDIR.TAG check, or the "more path" rule. This test
    fails.
    """

    script = executable(tmp_path / "cache" / "uv" / "archive-v0" / "A1b2" / "bin" / "alice-memory")
    assert in_uv_cache(script)
    moved = executable(tmp_path / "anything" / "archive-v0" / "A1b2" / "bin" / "alice-memory")
    assert not in_uv_cache(moved)
    (tmp_path / "anything" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8")
    assert in_uv_cache(moved)
    assert not in_uv_cache(tmp_path / "anything" / "environments-v2" / "env-1")
    assert not in_uv_cache(tmp_path / "anything" / "archive-v0")
    user = executable(tmp_path / "Documents" / "Archive-V2" / "2024" / "alice-memory")
    assert not in_uv_cache(user)
    venv = executable(tmp_path / "project" / "environments-v3" / "venv" / "bin" / "alice-memory")
    assert not in_uv_cache(venv)
    assert not in_uv_cache(tmp_path / "venv" / "bin" / "alice-memory")


def test_uv_toml_cache_dir_is_a_uv_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 4 P3: a cache set by ``cache-dir`` in uv.toml was not detected.

    Mutation: stop reading uv.toml. This test fails.
    """

    config_home = tmp_path / "config"
    (config_home / "uv").mkdir(parents=True)
    cache = tmp_path / "custom-cache"
    script = executable(cache / "tools" / "bin" / "alice-memory")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    assert not in_uv_cache(script)
    (config_home / "uv" / "uv.toml").write_text(f'cache-dir = "{cache}"\n', encoding="utf-8")
    assert in_uv_cache(script)


def test_environments_layout_is_a_uv_cache(tmp_path: Path) -> None:
    """``uv/environments-v2/<name>`` symlinks into archive-v0; both paths count.

    Mutation: match only archive-vN. This test fails on the unresolved path.
    """

    archive = tmp_path / "cache" / "uv" / "archive-v0" / "Xyz9"
    make_scripts(archive / "bin")
    link = tmp_path / "cache" / "uv" / "environments-v2" / "alice-memory-3f"
    link.parent.mkdir(parents=True)
    link.symlink_to(archive, target_is_directory=True)
    unresolved = link / "bin" / "alice-memory"
    assert unresolved.is_file()
    assert in_uv_cache(unresolved)
    assert in_uv_cache(os.path.realpath(unresolved))


def test_uv_cache_dir_override_is_a_uv_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache moved with UV_CACHE_DIR has no ``uv`` segment; the variable names it.

    Mutation: ignore UV_CACHE_DIR. This test fails.
    """

    cache = tmp_path / "my-cache"
    script = executable(cache / "tools" / "Q" / "bin" / "alice-memory")
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    assert not in_uv_cache(script)
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    assert in_uv_cache(script)


def test_scripts_in_a_uv_cache_are_never_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair inside the cache is skipped, and the uv warning says why.

    The control run shows the same pair outside a cache is offered, so the
    refusal is about the cache. Mutation: skip the cache check on found
    scripts. This test fails.
    """

    outside = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=outside)
    assert find_launcher().launcher == script_launcher(str(outside / "alice-memory"))

    inside = make_scripts(tmp_path / "cache" / "uv" / "archive-v0" / "Z" / "bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=inside)
    search = find_launcher()
    assert search.launcher is None
    assert search.warning == UV_TEMP_ENV_WARNING


def test_a_symlink_into_the_cache_is_caught_by_its_resolved_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bin dir outside the cache that links into it is still the cache.

    Mutation: check only the unresolved path. This test fails.
    """

    inside = make_scripts(tmp_path / "cache" / "uv" / "archive-v0" / "Z" / "bin")
    linked = tmp_path / "friendly-bin"
    linked.symlink_to(inside, target_is_directory=True)
    assert not in_uv_cache(linked / "alice-memory")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=linked)
    assert find_launcher().launcher is None


def test_running_from_a_uv_cache_offers_no_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sys.prefix inside a uv cache means a temporary uvx env: write uvx by name.

    Mutation: drop the sys.prefix check. This test fails.
    """

    good = make_scripts(tmp_path / "venv" / "bin")
    pin_launcher_search(
        monkeypatch,
        tmp_path,
        uvx=None,
        user=good,
        prefix=str(tmp_path / "c" / "uv" / "archive-v0" / "P"),
    )
    search = find_launcher()
    assert search.launcher is None
    assert search.warning == UV_TEMP_ENV_WARNING


# --- script lookup order (review 10) -----------------------------------------------------


def test_running_install_wins_over_user_scheme_and_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interpreter scripts dir first, then next to Python, then --user, PATH last.

    Mutation: look on PATH first. This test fails.
    """

    interpreter = make_scripts(tmp_path / "interp")
    user = make_scripts(tmp_path / "user")
    on_path = make_scripts(tmp_path / "path")
    pin_launcher_search(
        monkeypatch,
        tmp_path,
        uvx=None,
        interpreter=interpreter,
        user=user,
        on_path={"alice-memory": str(on_path / "alice-memory")},
    )
    assert find_launcher().launcher == script_launcher(str(interpreter / "alice-memory"))
    pin_launcher_search(
        monkeypatch,
        tmp_path,
        uvx=None,
        user=user,
        on_path={"alice-memory": str(on_path / "alice-memory")},
    )
    assert find_launcher().launcher == script_launcher(str(user / "alice-memory"))
    pin_launcher_search(
        monkeypatch, tmp_path, uvx=None, on_path={"alice-memory": str(on_path / "alice-memory")}
    )
    assert find_launcher().launcher == script_launcher(str(on_path / "alice-memory"))


def test_both_scripts_must_come_from_one_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """alice-memory here and alice-memory-session-start there is not a launcher.

    The warning names every dir searched. Mutation: take each script from
    the first dir that has it. This test fails.
    """

    only_mcp = make_scripts(tmp_path / "a", session_start=False)
    only_hook = tmp_path / "b"
    executable(only_hook / "alice-memory-session-start")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, interpreter=only_mcp, user=only_hook)
    search = find_launcher()
    assert search.launcher is None
    assert search.warning is not None
    assert search.warning.startswith(UVX_MISSING_WARNING_PREFIX)
    assert str(only_mcp) in search.warning and str(only_hook) in search.warning


def test_user_scheme_scripts_are_found_and_not_called_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip --user puts scripts in the user scheme dir; install finds them there.

    Mutation: drop the user scheme dir from the search. This test fails.
    """

    user = make_scripts(tmp_path / "user-bin")
    pin_launcher_search(monkeypatch, tmp_path, uvx=None, user=user)
    search = find_launcher()
    assert search.launcher == script_launcher(str(user / "alice-memory"))
    assert search.warning is None


# --- quoting (ruling A1) -----------------------------------------------------------------


def test_posix_quoting_leaves_ordinary_paths_byte_identical() -> None:
    """Mutation: quote every token. This test fails."""

    argv = ["/usr/local/bin/alice-memory-session-start", "--data-dir", "/Users/sam/.alice"]
    assert format_command(argv, windows=False) == (" ".join(argv), None)
    assert format_command(["a b", "c"], windows=False) == ("'a b' c", None)


def test_windows_text_is_shell_neutral() -> None:
    """Forward slashes, and double quotes only around a token with a space.

    Mutation: quote every token, or keep backslashes. This test fails.
    """

    launcher = script_launcher("C:\\Tools\\alice-memory.exe")
    text, problem = hook_command(launcher, "C:\\Users\\Sam Smith\\.alice", windows=True)
    assert problem is None
    assert text == 'C:/Tools/alice-memory-session-start.exe --data-dir "C:/Users/Sam Smith/.alice"'


@pytest.mark.parametrize(
    "char",
    ['"', "$", "`", "%", "!", "'", "{", "}", ",", "[", "]", "\n", "\r",
     chr(0x2018), chr(0x2019), chr(0x201A), chr(0x201B), chr(0x201C), chr(0x201D), chr(0x201E)],
)
def test_windows_refuses_characters_no_one_quoting_keeps(char: str) -> None:
    """cmd, PowerShell and Git Bash disagree about these; install writes none.

    Review round 4 P3 added bash brace expansion and globs ({ } , [ ]),
    line breaks, and the curly quotes PowerShell reads as quotes.
    Mutation: drop the character from the refused set. That case fails.
    """

    text, problem = format_command(
        ["C:/Tools/alice-memory-session-start.exe", "--data-dir", f"C:/Users/O{char}B/.alice"],
        windows=True,
    )
    assert text == "" and problem is not None and repr(char) in problem


def test_windows_refuses_a_quoted_command_path() -> None:
    """PowerShell will not run a quoted path without &.

    Mutation: quote the first token like any other. This test fails.
    """

    _text, problem = format_command(
        ["C:/Program Files/x/alice-memory-session-start.exe", "--data-dir", "C:/d"],
        windows=True,
    )
    assert problem is not None and "PowerShell" in problem


# --- reading hooks back ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "windows", "raw", "trusted"),
    [
        ("uvx --from alice-memory alice-memory-session-start --data-dir /x", False, "/x", True),
        ("alice-memory-session-start", False, None, False),
        ("'/a b/alice-memory-session-start' --data-dir '/x y'", False, "/x y", True),
        ("/v/alice-memory-session-start --data-dir /a --data-dir /b", False, "/b", True),
        ("/v/alice-memory-session-start --data-dir '/unbalanced", False, "'/unbalanced", False),
        ("C:\\x\\alice-memory-session-start.EXE --data-dir C:\\y", False, "C:y", False),
        ("/v/alice-memory-session-start --data-dir '/a\\b'", False, "/a\\b", True),
        ("/v/alice-memory-session-start --data-dir  /x", False, "/x", True),
        ("/v/alice-memory-session-start --data-dir vault", False, "vault", False),
        ("C:\\x\\alice-memory-session-start.EXE --data-dir C:\\y", True, "C:\\y", True),
        ('C:/Tools/alice-memory-session-start.exe --data-dir "C:/d e"', True, "C:/d e", True),
        ('"C:/Program Files/x/alice-memory-session-start.exe" --data-dir C:/d', True, "C:/d", True),
        ('/v/alice-memory-session-start --data-dir "/Users/me/My Vault"', False, "/Users/me/My Vault", True),
        ("/v/alice-memory-session-start --data-dir '$HOME/x'", False, "$HOME/x", False),
        ("/v/alice-memory-session-start --data-dir /ABSOLUTE/PATH/TO/.alice", False, "/ABSOLUTE/PATH/TO/.alice", False),
        ("/v/alice-memory-session-start --data-dir /a # --data-dir /b", False, "/a", True),
        ("/v/alice-memory-session-start --data-dir /a; y --data-dir /b", False, "/a", True),
        ("/v/alice-memory-session-start --data-dir /a && y --data-dir /b", False, "/a", True),
        ("/v/alice-memory-session-start --data-dir /a || y --data-dir /b", False, "/a", True),
        ("/v/alice-memory-session-start --data-dir /a | tee --data-dir /b", False, "/a", True),
        ("/v/alice-memory-session-start --data-dir /a\ny --data-dir /b", False, "/a", True),
        ("/v/alice-memory-session-start --data-dir /v/{a,b}", False, "/v/{a,b}", False),
        ("C:/x/alice-memory-session-start.exe --data-dir C:/a & y --data-dir C:/b", True, "C:/a", True),
    ],
    ids=[
        "v0.16.0", "bare", "posix-quoted", "repeated", "bad-quote", "posix-mode-backslashes",
        "shlex-written-backslash", "double-space", "relative", "windows-backslash",
        "windows-written", "windows-quoted-program", "hand-double-quoted",
        "single-quoted-dollar-is-literal-but-relative", "docs-placeholder",
        "comment", "semicolon", "and-and", "or-or", "pipe", "newline", "brace", "windows-ampersand",
    ],
)
def test_hooks_read_back_by_the_rules_they_were_written_with(
    command: str, windows: bool, raw: str | None, trusted: bool
) -> None:
    """POSIX hooks are read as a POSIX shell reads them, Windows hooks as Windows does.

    Every case is recognised as Alice's by its script's basename. Round 4
    (S2) trusted a dir only when the text round-tripped through shlex.join,
    which the round-4 review showed overwrote a hand-written
    ``--data-dir "/Users/me/My Vault"`` onto ~/.alice. Round 5: a dir is
    trusted when the shell reads the word literally (P2-2) and it is an
    absolute path other than the docs placeholder, whatever the spacing or
    quoting style. Mutation: restore the backslash rule, treat a
    double-quoted word as not literal, or trust the placeholder. A case
    fails.
    """

    assert is_session_start_command(command, windows=windows)
    read = read_hook_data_dir(command, windows=windows)
    assert (read.raw, read.trusted) == (raw, trusted)


@pytest.mark.parametrize(
    ("command", "windows"),
    [
        ("uvx --from alice-memory alice-memory-session-start --data-dir $HOME/.alice", False),
        ('uvx --from alice-memory alice-memory-session-start --data-dir "$HOME/.alice"', False),
        ("uvx --from alice-memory alice-memory-session-start --data-dir ~/.alice", False),
        ("uvx --from alice-memory alice-memory-session-start --data-dir /x/`whoami`", False),
        ("uvx --from alice-memory alice-memory-session-start --data-dir %USERPROFILE%/.alice", True),
    ],
    ids=["dollar", "quoted-dollar", "tilde", "backtick", "percent"],
)
def test_a_data_dir_that_relies_on_shell_expansion_is_never_trusted(
    command: str, windows: bool
) -> None:
    """Review round 4 S3: ``$HOME/.alice`` was read as a literal path under home.

    Round 5 judges this by shell semantics, not by characters: $, a backtick
    or a backslash outside single quotes, a leading unquoted ~, or % in a
    Windows hook. Mutation: treat a double-quoted $ as literal, or drop the ~
    or % rule. A case fails.
    """

    read = read_hook_data_dir(command, windows=windows)
    assert read.shell and not read.trusted


def test_posix_hooks_are_never_split_by_whitespace_when_shlex_can_read_them() -> None:
    """shlex.join writes '/a b\\c' quoted; reading it back must not split it."""

    assert split_command("x --data-dir '/a b\\c'", windows=False) == ["x", "--data-dir", "/a b\\c"]


def test_look_alike_hook_is_not_alices() -> None:
    assert not is_session_start_command("/v/alice-memory-session-starter --data-dir /x")


# --- liveness (ruling A3) ----------------------------------------------------------------


def test_liveness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launcher is live when what it runs exists here, and not in a uv cache.

    Mutation: treat an absolute uvx as live only when uvx is on PATH. This
    test fails.
    """

    pin_launcher_search(monkeypatch, tmp_path, uvx=None)
    uvx = executable(tmp_path / "tools" / "uvx")
    live_uvx = parse_launcher({"command": str(uvx), "args": ["alice-memory", "mcp"]})
    assert live_uvx is not None and launcher_problem(live_uvx[0]) is None
    bare = parse_launcher({"command": "uvx", "args": ["alice-memory", "mcp"]})
    assert bare is not None and "not on PATH" in (launcher_problem(bare[0]) or "")
    gone = script_launcher(str(tmp_path / "gone" / "alice-memory"))
    assert "missing" in (launcher_problem(gone) or "")
    cached = make_scripts(tmp_path / "c" / "uv" / "archive-v0" / "K")
    assert "uv cache" in (launcher_problem(script_launcher(str(cached / "alice-memory"))) or "")
    here = make_scripts(tmp_path / "venv")
    assert launcher_problem(script_launcher(str(here / "alice-memory"))) is None
    assert host_launcher.hook_script_problem(script_launcher(str(here / "alice-memory"))) is None


def test_a_script_without_its_session_start_sibling_is_dead(tmp_path: Path) -> None:
    """Review round 4 S4: liveness is the pair test find_launcher applies.

    Mutation: judge a script launcher by alice-memory alone. This test fails.
    """

    lonely = make_scripts(tmp_path / "old-venv" / "bin", session_start=False)
    problem = launcher_problem(script_launcher(str(lonely / "alice-memory")))
    assert problem is not None and "alice-memory-session-start is missing" in problem


def test_a_cached_launcher_is_known_as_cached(tmp_path: Path) -> None:
    """Review round 4 P1 needs to tell a cached launcher from a merely missing one."""

    cached = make_scripts(tmp_path / "c" / "archive-v0" / "K" / "bin")
    (tmp_path / "c" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55\n", encoding="utf-8")
    assert launcher_in_uv_cache(script_launcher(str(cached / "alice-memory")))
    assert not launcher_in_uv_cache(script_launcher(str(tmp_path / "gone" / "alice-memory")))
