from route_analysis.app import main


def test_smoke_mode_initializes_qt_and_geometry_without_opening_window() -> None:
    assert main(["route-analysis", "--smoke-test"]) == 0
