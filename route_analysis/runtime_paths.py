"""Resolve writable runtime paths consistently in source and frozen builds."""

from __future__ import annotations

import sys
from pathlib import Path


def resolve_data_dir(
    *,
    frozen: bool | None = None,
    executable: str | Path | None = None,
    module_file: str | Path | None = None,
) -> Path:
    """Return the local data directory without creating it.

    Source runs store data in the repository root. PyInstaller builds store data
    beside the executable so the whole folder remains portable.
    """

    is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
    executable_path = Path(sys.executable if executable is None else executable).resolve()
    source_file = Path(__file__ if module_file is None else module_file).resolve()
    base_dir = executable_path.parent if is_frozen else source_file.parent.parent
    return base_dir / "data"
