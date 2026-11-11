#!/usr/bin/env bash
# Timed, retried apt-get for GitHub-hosted Linux runners.
# apt-get update/install has sat until the job timeout on ubuntu-24.04
# even after the dpkg lock was free. Bound each attempt and kill the
# process group so a hung mirror cannot eat the job budget.
#
# Mirror fallback (the outage of 2026-11-11): the runner's sources name a
# mirror list (/etc/apt/apt-mirrors.txt) whose first entry is the azure
# mirror. That mirror stopped answering, and every attempt spent its whole
# update cap waiting on it ("Ign:" for every list; killed at the cap), so
# the outage cost the job. A failed attempt now puts the runner's original
# sources back and, when they name the mirror list, rewrites those URIs to
# the default Ubuntu archive, so the remaining attempts never read the
# list that just failed. The first attempt keeps the runner's own setup.
#
# Worst case, every cap at its maximum and every kill after the full kill
# grace: 3 x (90 s update + 5 s grace) plus 3 x (120 s install + 5 s
# grace) plus 3 dpkg-lock waits of up to 90 s (the initial wait and one
# between attempts) = 930 s, and the calling step allows 16 minutes. The
# wait between attempts is skipped after the last attempt: no further
# command runs, so no further wait is needed.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  printf '%s\n' "ci_apt_get: not Linux" >&2
  exit 1
fi

if [[ "$#" -lt 1 ]]; then
  printf '%s\n' "usage: ci_apt_get.sh <apt-package>..." >&2
  exit 1
fi

# Test hooks: the unit tests point the sources and the mirror list at
# temporary files and set one-second caps so the timeout paths run in
# seconds. Production runs set none of these and keep the caps below.
UPDATE_TIMEOUT_SECONDS="${CI_APT_GET_UPDATE_TIMEOUT:-90}"
INSTALL_TIMEOUT_SECONDS="${CI_APT_GET_INSTALL_TIMEOUT:-120}"
SOURCES_LIST="${CI_APT_SOURCES_LIST:-/etc/apt/sources.list}"
SOURCES_LIST_D="${CI_APT_SOURCES_LIST_D:-/etc/apt/sources.list.d/ubuntu.sources}"
MIRRORS_LIST="${CI_APT_MIRRORS_LIST:-/etc/apt/apt-mirrors.txt}"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ci_wait_for_dpkg_lock.sh
bash "${script_dir}/ci_wait_for_dpkg_lock.sh"

rewrite_apt_mirrors() {
  local src
  for src in "${SOURCES_LIST}" "${SOURCES_LIST_D}"; do
    if [[ -f "${src}" ]] && grep -q 'archive.ubuntu.com' "${src}"; then
      sudo sed -i.bak \
        -e 's|http://archive.ubuntu.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' \
        -e 's|http://security.ubuntu.com/ubuntu|http://azure.archive.ubuntu.com/ubuntu|g' \
        "${src}" || true
    fi
  done
}

fallback_to_default_archive() {
  # Undo the rewrite, then stop reading the mirror list that just failed:
  # a later attempt must reach the default archive without spending its
  # time cap on the first entry again.
  local src
  for src in "${SOURCES_LIST}" "${SOURCES_LIST_D}"; do
    [[ -f "${src}" ]] || continue
    if [[ -f "${src}.bak" ]]; then
      sudo mv "${src}.bak" "${src}"
    fi
    if grep -q 'mirror+file:' "${src}"; then
      # -i with an explicit suffix works on both GNU and BSD sed.
      sudo sed -i.bak-fallback 's|mirror+file:/etc/apt/apt-mirrors.txt|http://archive.ubuntu.com/ubuntu|g' "${src}"
    fi
  done
}

run_timed() {
  local seconds="$1"
  shift
  sudo -E env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a \
    setsid --wait timeout --signal=TERM --kill-after=5s "${seconds}s" "$@"
}

clear_orphans() {
  sudo killall -9 apt-get apt 2>/dev/null || true
  bash "${script_dir}/ci_wait_for_dpkg_lock.sh"
}

served_by() {
  # The first address the active configuration names, for the log. This is
  # what the sources name, not which of them answered: on the runners the
  # mirror list is a priority list and apt itself falls through a dead
  # entry to the next one.
  local src url
  for src in "${SOURCES_LIST}" "${SOURCES_LIST_D}"; do
    if [[ -f "${src}" ]]; then
      url="$(grep -m1 -oh 'http[s]*://[a-z.]*archive.ubuntu.com/ubuntu[/]*' "${src}" 2>/dev/null || true)"
      if [[ -n "${url}" ]]; then
        printf '%s' "${url}"
        return 0
      fi
    fi
  done
  if [[ -f "${MIRRORS_LIST}" ]]; then
    url="$(head -1 "${MIRRORS_LIST}" 2>/dev/null | awk '{print $1}')"
    if [[ -n "${url}" ]]; then
      printf '%s' "${url}"
      return 0
    fi
  fi
  printf 'the configured sources'
}

apt_opts=(
  -o Acquire::Retries=3
  -o Acquire::http::Timeout=15
  -o Acquire::https::Timeout=15
  -o Acquire::ForceIPv4=true
  -o Dpkg::Use-Pty=0
)

rewrite_apt_mirrors

for attempt in 1 2 3; do
  if run_timed "${UPDATE_TIMEOUT_SECONDS}" apt-get "${apt_opts[@]}" update \
    && run_timed "${INSTALL_TIMEOUT_SECONDS}" apt-get "${apt_opts[@]}" install --yes --no-install-recommends "$@"; then
    printf 'ci_apt_get: attempt %s reads %s\n' "${attempt}" "$(served_by)" >&2
    exit 0
  fi
  printf '%s\n' "ci_apt_get: attempt ${attempt} failed" >&2
  fallback_to_default_archive
  if [[ "${attempt}" -lt 3 ]]; then
    clear_orphans
  fi
done

printf '%s\n' "ci_apt_get: all attempts failed" >&2
exit 1
