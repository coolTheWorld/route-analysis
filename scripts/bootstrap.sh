#!/usr/bin/env bash
# Create the Linux development environment for route-analysis.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "${script_dir}/.." && pwd)"
venv_python="${project_root}/.venv/bin/python"

select_python() {
    local candidate
    for candidate in python3.12 python3.13 python3.14 python3; do
        command -v "${candidate}" >/dev/null 2>&1 || continue
        if "${candidate}" -c 'import sys; raise SystemExit(0 if (3, 12) <= sys.version_info < (3, 15) else 1)' \
            >/dev/null 2>&1; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

if [ ! -x "${venv_python}" ]; then
    if ! python_command="$(select_python)"; then
        echo 'No Python 3.12-3.14 interpreter found. Install a supported interpreter first.' >&2
        exit 1
    fi
    echo "Creating the virtual environment with $("${python_command}" -V)."
    "${python_command}" -m venv "${project_root}/.venv"
fi

"${venv_python}" -m pip install --upgrade pip
"${venv_python}" -m pip install -r "${project_root}/requirements-dev.txt"
"${venv_python}" -m pip install --no-deps -e "${project_root}"

echo "Environment ready with $("${venv_python}" -V). Run scripts/run.sh to start."
