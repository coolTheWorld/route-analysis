#!/usr/bin/env bash
# Start route-analysis from source on Linux.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_python="${project_root}/.venv/bin/python"

if [ ! -x "${venv_python}" ]; then
    echo 'Virtual environment not found. Run scripts/bootstrap.sh first.' >&2
    exit 1
fi

if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ] && [ -z "${QT_QPA_PLATFORM:-}" ]; then
    echo 'No graphical display found: DISPLAY and WAYLAND_DISPLAY are both unset.' >&2
    echo 'Start a desktop session, or set QT_QPA_PLATFORM=offscreen for a headless run.' >&2
    exit 1
fi

cd "${project_root}"
exec "${venv_python}" -m route_analysis "$@"
