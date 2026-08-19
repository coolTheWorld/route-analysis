#!/usr/bin/env bash
# Build and verify the Linux x64 folder distribution.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_python="${project_root}/.venv/bin/python"
spec_file="${project_root}/route-analysis.spec"
distribution="${project_root}/dist/RouteAnalysis"
executable="${distribution}/RouteAnalysis"
documented_glibc='2.43'

run_gate() {
    local message="$1"
    shift
    if ! "$@"; then
        echo "${message}" >&2
        exit 1
    fi
}

architecture="$(uname -m)"
if [ "${architecture}" != 'x86_64' ]; then
    echo "Build the Linux x64 distribution on x86_64, not ${architecture}." >&2
    exit 1
fi

glibc_version="$(ldd --version | head -n 1 | awk '{ print $NF }')"
oldest_glibc="$(printf '%s\n%s\n' "${documented_glibc}" "${glibc_version}" | sort -V | head -n 1)"
if [ "${glibc_version}" != "${documented_glibc}" ] && [ "${oldest_glibc}" = "${documented_glibc}" ]; then
    echo "Warning: this host runs glibc ${glibc_version}, newer than the documented ${documented_glibc} baseline." >&2
    echo "Warning: the distribution will not start on systems older than glibc ${glibc_version}." >&2
fi

if [ ! -x "${venv_python}" ]; then
    echo 'Virtual environment not found. Run scripts/bootstrap.sh first.' >&2
    exit 1
fi
if [ ! -f "${spec_file}" ]; then
    echo 'route-analysis.spec is missing.' >&2
    exit 1
fi

cd "${project_root}"

run_gate 'Tests failed. Build stopped.' "${venv_python}" -m pytest
run_gate 'Lint failed. Build stopped.' "${venv_python}" -m ruff check route_analysis tests
run_gate 'Type checking failed. Build stopped.' "${venv_python}" -m mypy route_analysis
run_gate 'PyInstaller build failed.' "${venv_python}" -m PyInstaller --clean --noconfirm "${spec_file}"

if [ ! -x "${executable}" ]; then
    echo 'Build finished but dist/RouteAnalysis/RouteAnalysis was not found.' >&2
    exit 1
fi
if [ "$(head -c 4 -- "${executable}" | od -An -tx1 | tr -d ' \n')" != '7f454c46' ]; then
    echo 'dist/RouteAnalysis/RouteAnalysis is not an ELF executable.' >&2
    exit 1
fi

run_gate 'Packaged offscreen smoke test failed.' \
    env QT_QPA_PLATFORM=offscreen "${executable}" --smoke-test

if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    run_gate 'Packaged smoke test failed on the session Qt platform plugin.' \
        env -u QT_QPA_PLATFORM "${executable}" --smoke-test
    platform_smoke='passed on the session platform plugin'
else
    echo 'Warning: no display found, so the Qt platform plugin stayed unverified.' >&2
    platform_smoke='skipped, no display'
fi

file_count="$(find "${distribution}" ! -type d | wc -l)"
total_bytes="$(find "${distribution}" ! -type d -printf '%s\n' | awk '{ total += $1 } END { print total + 0 }')"
checksum="$(sha256sum "${executable}" | cut -d ' ' -f 1)"

echo "Build and smoke tests passed: ${executable}"
echo "Files: ${file_count}"
echo "Bytes: ${total_bytes}"
echo "RouteAnalysis SHA-256: ${checksum}"
echo "Runtime baseline: x86_64 with glibc >= ${glibc_version}"
echo "Offscreen smoke test: passed"
echo "Platform smoke test: ${platform_smoke}"
