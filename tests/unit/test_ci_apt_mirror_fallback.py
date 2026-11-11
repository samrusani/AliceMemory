"""Tests that run scripts/ci_apt_get.sh with stand-ins on the path.

The runner's package setup, as run 43093032731's step log shows and the
branch's diagnostic run prints: /etc/apt/apt-mirrors.txt lists the azure
mirror first, and /etc/apt/sources.list.d/ubuntu.sources names the list
with ``URIs: mirror+file:/etc/apt/apt-mirrors.txt``. The sandbox below
stands in for the commands whose behaviour the case needs to control:
uname (the script's own platform check), sudo, setsid and killall
(pass-through or no-op), timeout (a small emulator of the GNU flags the
script uses), apt-get (it decides from what the real apt-get would read
on a runner: it hangs when the active configuration resolves to the dead
first mirror), and the dpkg-lock wait (a no-op that counts its calls).
The other commands run for real, with the sources files pointed at
temporary paths, so no test touches a real apt configuration.

Because of the stand-ins the script-running tests run on every platform;
CI runs the same tests on real ubuntu-24.04 runners, where the real
commands stand behind the same fakes.

The outage these tests encode: on 2026-11-11 the mirror list's first
entry stopped answering, and every attempt spent its whole update cap
waiting on that one entry, so the job died. The fix: attempt 1 keeps the
runner's setup; a failed attempt restores the original sources and
rewrites mirror-list URIs to the default Ubuntu archive, so the
remaining attempts never read the failed list again.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APT_SCRIPT = ROOT / "scripts" / "ci_apt_get.sh"
OPS_WORKFLOW = ROOT / ".github" / "workflows" / "ops-evidence.yml"

RUNNER_SOURCES = """Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble noble-updates noble-backports
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

## Ubuntu security updates. Aside from URIs and Suites,
## this should mirror your choices in the previous section.
Types: deb
URIs: mirror+file:/etc/apt/apt-mirrors.txt
Suites: noble-security
Components: main universe restricted multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""
RUNNER_MIRROR_LIST = """http://azure.archive.ubuntu.com/ubuntu/	priority:1
https://archive.ubuntu.com/ubuntu/	priority:2
https://security.ubuntu.com/ubuntu/	priority:3
"""
DEFAULT_ARCHIVE_URI = "http://archive.ubuntu.com/ubuntu"
AZURE_MIRROR = "http://azure.archive.ubuntu.com/ubuntu/"

FAKE_PASSTHROUGH = """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
while args and args[0].startswith("-"):
    args.pop(0)
os.execvp(args[0], args)
"""

FAKE_TIMEOUT = """#!/usr/bin/env python3
import signal
import subprocess
import sys

args = sys.argv[1:]
grace = 5.0
if args and args[0] == "--signal=TERM":
    args = args[1:]
if args and args[0].startswith("--kill-after="):
    grace = float(args[0].split("=", 1)[1].rstrip("s"))
    args = args[1:]
duration = float(args[0].rstrip("s"))
command = args[1:]
process = subprocess.Popen(command)
try:
    code = process.wait(timeout=duration)
except subprocess.TimeoutExpired:
    process.send_signal(signal.SIGTERM)
    try:
        code = process.wait(timeout=grace) or 124
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        code = 137
sys.exit(code)
"""

FAKE_APT_GET = """#!/usr/bin/env bash
echo "apt-get $*" >> {call_log}
if [ "$(echo "$*" | grep -c update)" -eq 0 ]; then
  exit 0
fi
active="$(grep -m1 'URIs:' {sources} | awk '{{print $2}}')"
case "$active" in
  "mirror+file:"*)
    active="$(awk '$1 ~ /^http/ {{print $1; exit}}' {mirrors_list})"
    ;;
esac
if [ "$active" = "{azure}" ] && [ "$(cat {mode_file})" = "azure_dead" ]; then
  echo "fake: the first mirror is dead; hanging until the cap" >> {call_log}
  exec sleep 600
fi
if [ "$(cat {mode_file})" = "always_dead" ]; then
  echo "fake: every source is dead; hanging until the cap" >> {call_log}
  exec sleep 600
fi
exit 0
"""


def test_apt_step_worst_case_stays_inside_the_step_cap() -> None:
    """Guard: the caps and the step cap move together.

    The single edits that must fail this test: the step's timeout-minutes
    back to 10, or the script's caps back to the old 120/180. The script's
    worst case is 3 x (update + grace) + 3 x (install + grace) + one
    dpkg-lock wait per wait the script performs (the initial wait plus one
    between attempts; none after the last), and that sum must stay under
    the step's timeout-minutes.
    """
    text = APT_SCRIPT.read_text(encoding="utf-8")
    ops = OPS_WORKFLOW.read_text(encoding="utf-8")
    select = ops.split("- name: Select PostgreSQL 16 client tools", 1)[1].split("- name:", 1)[0]
    step_cap = int(re.search(r"timeout-minutes:\s*(\d+)", select).group(1))
    update = int(re.search(r"CI_APT_GET_UPDATE_TIMEOUT:-(\d+)", text).group(1))
    install = int(re.search(r"CI_APT_GET_INSTALL_TIMEOUT:-(\d+)", text).group(1))
    grace = int(re.search(r"--kill-after=(\d+)s", text).group(1))
    lock_wait = int(
        re.search(
            r"still held after (\d+)s",
            (ROOT / "scripts" / "ci_wait_for_dpkg_lock.sh").read_text(encoding="utf-8"),
        ).group(1)
    )
    waits = _measured_dpkg_waits()
    worst = 3 * (update + grace) + 3 * (install + grace) + waits * lock_wait
    assert worst < step_cap * 60, (worst, step_cap)


def _measured_dpkg_waits() -> int:
    """Count the dpkg-lock waits one all-fail run performs, by running the
    script in the sandbox and counting the stand-in wait script's calls."""
    with _sandbox("always_dead") as box:
        result = _run_script(box)
        assert result.returncode == 1
        return len((box["tmp"] / "wait_calls.log").read_text(encoding="utf-8").splitlines())


def test_apt_script_pins_are_true_for_the_new_script() -> None:
    """Guard: the script keeps the timed-and-retried shape the older pins read."""
    text = APT_SCRIPT.read_text(encoding="utf-8")
    assert APT_SCRIPT.stat().st_mode & stat.S_IXUSR
    assert "setsid --wait timeout" in text
    assert "Acquire::http::Timeout=15" in text
    assert "all attempts failed" in text
    assert "exit 1" in text
    assert "fallback_to_default_archive" in text


class _Box:
    def __init__(self, value: dict, paths: tuple):
        self.value = value
        self.paths = paths

    def __enter__(self):
        return self.value

    def __exit__(self, *exc):
        import shutil

        for path in self.paths:
            shutil.rmtree(path, ignore_errors=True)
        return False


def _sandbox(mode: str) -> _Box:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="apt-sandbox-"))
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    scripts_dir = tmp / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(APT_SCRIPT, scripts_dir / "ci_apt_get.sh")
    wait_calls = tmp / "wait_calls.log"
    _write(scripts_dir / "ci_wait_for_dpkg_lock.sh", f"#!/usr/bin/env bash\necho wait >> {wait_calls}\n")
    _write(bin_dir / "uname", "#!/bin/sh\nprintf 'Linux\\n'\n")
    _write(bin_dir / "sudo", FAKE_PASSTHROUGH)
    _write(bin_dir / "setsid", FAKE_PASSTHROUGH)
    _write(bin_dir / "killall", "#!/bin/sh\nexit 0\n")
    _write(bin_dir / "timeout", FAKE_TIMEOUT)
    sources = tmp / "ubuntu.sources"
    sources.write_text(RUNNER_SOURCES, encoding="utf-8")
    mirrors = tmp / "apt-mirrors.txt"
    mirrors.write_text(RUNNER_MIRROR_LIST, encoding="utf-8")
    log_dir = tmp / "calls"
    log_dir.mkdir()
    mode_file = tmp / "apt_mode"
    mode_file.write_text(f"{mode}\n")
    call_log = log_dir / "calls.log"
    _write(
        bin_dir / "apt-get",
        FAKE_APT_GET.format(call_log=call_log, sources=sources, mirrors_list=mirrors, azure=AZURE_MIRROR, mode_file=mode_file),
    )
    env = {
        **{name: value for name, value in os.environ.items()},
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "CI_APT_GET_UPDATE_TIMEOUT": "1",
        "CI_APT_GET_INSTALL_TIMEOUT": "1",
        "CI_APT_SOURCES_LIST": str(sources),
        "CI_APT_SOURCES_LIST_D": str(tmp / "absent.sources"),
        "CI_APT_MIRRORS_LIST": str(mirrors),
    }
    return _Box(
        {
            "env": env,
            "script": scripts_dir / "ci_apt_get.sh",
            "sources": sources,
            "mirrors": mirrors,
            "call_log": call_log,
            "mode_file": mode_file,
            "wait_calls": wait_calls,
            "tmp": tmp,
        },
        (tmp,),
    )


def _write(path: Path, text: str, mode: int = 0o755) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def _run_script(sandbox: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(sandbox["script"]), "postgresql-client-16"],
        env=sandbox["env"], text=True, capture_output=True, timeout=120, check=False,
    )


def test_dead_first_mirror_costs_one_attempt_then_the_default_archive_serves() -> None:
    """Guard: fallback_to_default_archive after a failed attempt.

    The single edit that must fail this test: deleting the
    fallback_to_default_archive call from the attempt loop (or leaving the
    mirror-list URIs in place), so every attempt spends its cap on the
    dead first entry and the script exits 1 with nothing installed.
    """
    with _sandbox("azure_dead") as sandbox:
        result = _run_script(sandbox)
        assert result.returncode == 0, result.stderr
        assert "all attempts failed" not in result.stderr
        calls = sandbox["call_log"].read_text(encoding="utf-8").splitlines()
        update_calls = [line for line in calls if " update" in line]
        assert len(update_calls) >= 2, calls
        assert "fake: the first mirror is dead; hanging until the cap" in calls[1]
        sources_now = sandbox["sources"].read_text(encoding="utf-8")
        assert sources_now == RUNNER_SOURCES.replace("mirror+file:/etc/apt/apt-mirrors.txt", DEFAULT_ARCHIVE_URI)
        served = [line for line in result.stderr.splitlines() if "reads" in line]
        assert served == [f"ci_apt_get: attempt 2 reads {DEFAULT_ARCHIVE_URI}"]


def test_healthy_mirror_stays_one_attempt() -> None:
    """Guard: a working first mirror must not trigger a fallback or a retry."""
    with _sandbox("healthy") as sandbox:
        result = _run_script(sandbox)
        assert result.returncode == 0, result.stderr
        calls = sandbox["call_log"].read_text(encoding="utf-8").splitlines()
    assert len([line for line in calls if " update" in line]) == 1
    assert len([line for line in calls if " install" in line]) == 1
    assert "attempt 1 failed" not in result.stderr
    served = [line for line in result.stderr.splitlines() if "reads" in line]
    assert served == [f"ci_apt_get: attempt 1 reads {AZURE_MIRROR}"]


def test_every_source_dead_still_fails_closed_inside_the_cap() -> None:
    """Guard: the retry loop ends and reports inside the stated worst case."""
    import time

    with _sandbox("always_dead") as sandbox:
        started = time.monotonic()
        result = _run_script(sandbox)
        elapsed = time.monotonic() - started
        assert result.returncode == 1
        assert "attempt 1 failed" in result.stderr
        assert "attempt 3 failed" in result.stderr
        assert "all attempts failed" in result.stderr
        calls = sandbox["call_log"].read_text(encoding="utf-8").splitlines()
        assert len([line for line in calls if " update" in line]) == 3
        waits = len(sandbox["wait_calls"].read_text(encoding="utf-8").splitlines())
    text = APT_SCRIPT.read_text(encoding="utf-8")
    grace = int(re.search(r"--kill-after=(\d+)s", text).group(1))
    # The test's own caps: one second per update, three of them, the kill
    # grace after each, and a few seconds of process start-up.
    bound = 3 * (1 + grace) + 15
    assert elapsed < bound, (elapsed, bound)


def test_the_worst_case_arithmetic_uses_the_waits_a_run_performs() -> None:
    """Guard: the step cap's arithmetic counts the waits a run performs.

    The single edit that must fail this test: the attempt loop calling
    clear_orphans after the last attempt too (or a cap change), so the
    wait count or the caps move and the sum crosses the step.
    """
    ops = OPS_WORKFLOW.read_text(encoding="utf-8")
    select = ops.split("- name: Select PostgreSQL 16 client tools", 1)[1].split("- name:", 1)[0]
    step_cap = int(re.search(r"timeout-minutes:\s*(\d+)", select).group(1))
    text = APT_SCRIPT.read_text(encoding="utf-8")
    update = int(re.search(r"CI_APT_GET_UPDATE_TIMEOUT:-(\d+)", text).group(1))
    install = int(re.search(r"CI_APT_GET_INSTALL_TIMEOUT:-(\d+)", text).group(1))
    grace = int(re.search(r"--kill-after=(\d+)s", text).group(1))
    lock_wait = int(
        re.search(
            r"still held after (\d+)s",
            (ROOT / "scripts" / "ci_wait_for_dpkg_lock.sh").read_text(encoding="utf-8"),
        ).group(1)
    )
    waits = _measured_dpkg_waits()
    assert waits == 3, waits
    worst = 3 * (update + grace) + 3 * (install + grace) + waits * lock_wait
    assert worst < step_cap * 60, (worst, step_cap)
