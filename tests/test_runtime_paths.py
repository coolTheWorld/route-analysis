from pathlib import Path

from route_analysis.runtime_paths import resolve_data_dir


def test_source_mode_uses_project_data_directory(tmp_path: Path) -> None:
    package_file = tmp_path / "project" / "route_analysis" / "runtime_paths.py"

    result = resolve_data_dir(
        frozen=False,
        executable=tmp_path / "ignored" / "RouteAnalysis.exe",
        module_file=package_file,
    )

    assert result == tmp_path / "project" / "data"


def test_frozen_mode_uses_executable_sibling_data_directory(tmp_path: Path) -> None:
    executable = tmp_path / "release" / "RouteAnalysis.exe"

    result = resolve_data_dir(
        frozen=True,
        executable=executable,
        module_file=tmp_path / "ignored" / "runtime_paths.py",
    )

    assert result == tmp_path / "release" / "data"
