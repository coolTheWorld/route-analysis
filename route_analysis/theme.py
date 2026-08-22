"""Application-wide visual tokens and accessible Qt stylesheet."""

from __future__ import annotations

import sys

from PySide6.QtGui import QFont, QFontDatabase

WINDOWS_FONT_FILE = "C:/Windows/Fonts/msyh.ttc"
CJK_FONT_FAMILIES = (
    "Microsoft YaHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "WenQuanYi Micro Hei",
)


def select_cjk_font(point_size: int = 9) -> QFont:
    """Return an installed CJK family so rendered output never shows tofu blocks."""

    if sys.platform == "win32":
        QFontDatabase.addApplicationFont(WINDOWS_FONT_FILE)
    families = set(QFontDatabase.families())
    for family in CJK_FONT_FAMILIES:
        if family in families:
            return QFont(family, point_size)
    fallback = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    fallback.setPointSize(point_size)
    return fallback


APPLICATION_STYLESHEET = """
QMainWindow, QDialog, QWidget {
    background: #f4f7fb;
    color: #172033;
    font-family: "Segoe UI", "Microsoft YaHei UI";
    font-size: 13px;
}
QFrame#navigationPanel, QFrame#controlPanel {
    background: #ffffff;
    border: 1px solid #dce3ed;
    border-radius: 8px;
}
QPushButton, QToolButton {
    min-height: 28px;
    padding: 2px 10px;
    border: 1px solid #b9c5d6;
    border-radius: 5px;
    background: #ffffff;
}
QPushButton:hover, QToolButton:hover { background: #edf4ff; border-color: #4784d5; }
QPushButton:pressed, QToolButton:pressed { background: #dbeaff; }
QPushButton:disabled { color: #8c96a5; background: #eef1f5; }
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    min-height: 28px;
    padding: 1px 6px;
    border: 1px solid #b9c5d6;
    border-radius: 4px;
    background: #ffffff;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 2px solid #2474d8;
}
QTableWidget, QListWidget {
    background: #ffffff;
    alternate-background-color: #f7f9fc;
    border: 1px solid #d5dde8;
    border-radius: 5px;
    selection-background-color: #dceaff;
    selection-color: #10213d;
}
QHeaderView::section {
    background: #eaf0f7;
    padding: 6px;
    border: 0;
    border-right: 1px solid #d5dde8;
    font-weight: 600;
}
QGroupBox {
    font-weight: 600;
    border: 1px solid #d5dde8;
    border-radius: 6px;
    margin-top: 10px;
    padding-top: 8px;
    background: #ffffff;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QTabWidget::pane { border: 1px solid #d5dde8; background: #ffffff; }
QStatusBar { background: #eaf0f7; border-top: 1px solid #d5dde8; }
"""


# Palette shared by the stylesheet above and by QPainter drawing code. Values match
# canvas.py and the clearance design tokens; ``DANGER`` is canvas.RADIUS_ENDPOINT_COLOR.
TEXT_PRIMARY = "#172033"
TEXT_SECONDARY = "#3d4a60"
TEXT_MUTED = "#6e7f93"
TEXT_FAINT = "#8290a3"

ACCENT = "#2474d8"
ACCENT_DEEP = "#14395f"
ACCENT_TINT = "#dbeaff"
ACCENT_BORDER = "#4784d5"

AREA_FILL = "#4f8dd8"
AREA_STROKE = "#3972b6"
SWEEP_FILL = "#14395f"

DANGER = "#b4233f"
DANGER_DEEP = "#a10f2b"
DANGER_POINT = "#ef476f"
DANGER_TINT = "#fbe3e8"
DANGER_TINT_SOFT = "#fdf1f4"
DANGER_BORDER = "#e59aab"

WARNING = "#9a6500"
WARNING_BAR = "#e0a800"
WARNING_POINT = "#ffd166"
WARNING_TINT = "#fff6dd"
WARNING_BORDER = "#e6c46a"

SUCCESS = "#00705a"
SUCCESS_BAR = "#00a884"
SUCCESS_TINT = "#e2f6f1"
SUCCESS_TINT_SOFT = "#f2fbf8"
SUCCESS_BORDER = "#7fd3c1"

PANEL_BASE = "#f4f7fb"
HEADER_BASE = "#eaf0f7"
CANVAS_BASE = "#f7f9fc"
BORDER = "#d5dde8"
BORDER_SOFT = "#dce3ed"
BORDER_FAINT = "#cfdae8"
INPUT_BORDER = "#b9c5d6"
GRID = "#e6ebf2"
GRID_STRONG = "#dfe5ee"
CARD = "#ffffff"

# Qt rounds stylesheet pixel sizes to whole pixels, so the design's 10.5 px labels
# render at 11 px in widgets. QPainter drawing keeps the fractional size via
# QFont.setPointSizeF, which is why chart text and widget text differ by a hair.
CLEARANCE_STYLESHEET = f"""
QFrame#clearanceCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QFrame#clearanceCard[state="danger"] {{ border-color: {DANGER_BORDER}; }}
QFrame#clearanceCard[state="warning"] {{ border-color: {WARNING_BORDER}; }}
QFrame#clearanceCard[state="success"] {{ border-color: {SUCCESS_BORDER}; }}
QFrame#clearanceCardBar {{ background: {BORDER}; border-radius: 2px; }}
QFrame#clearanceCardBar[state="danger"] {{ background: {DANGER}; }}
QFrame#clearanceCardBar[state="warning"] {{ background: {WARNING_BAR}; }}
QFrame#clearanceCardBar[state="success"] {{ background: {SUCCESS_BAR}; }}
QLabel#clearanceCardLabel {{ color: {TEXT_MUTED}; font-size: 11px; }}
QLabel#clearanceCardValue {{
    color: {TEXT_PRIMARY};
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 19px;
    font-weight: 700;
}}
QLabel#clearanceCardValue[state="danger"] {{ color: {DANGER}; }}
QLabel#clearanceCardValue[state="warning"] {{ color: {WARNING}; }}
QLabel#clearanceCardValue[state="success"] {{ color: {SUCCESS}; }}
QLabel#clearanceCardUnit {{ color: {TEXT_MUTED}; font-size: 11px; }}
QFrame#clearanceStatusBar {{
    background: {HEADER_BASE};
    border-top: 1px solid {BORDER};
}}
QLabel#clearanceStatusText {{ color: {TEXT_SECONDARY}; font-size: 11px; }}
QLabel#clearanceStatusSeparator {{ color: {INPUT_BORDER}; font-size: 11px; }}
QLabel#clearancePill {{
    border: 1px solid {INPUT_BORDER};
    border-radius: 8px;
    padding: 1px 7px;
    color: {TEXT_MUTED};
    font-size: 10px;
}}
QFrame#suggestionCard {{
    background: {CANVAS_BASE};
    border: 1px solid {BORDER_FAINT};
    border-radius: 6px;
}}
QFrame#suggestionCard[rank="primary"] {{
    background: {SUCCESS_TINT_SOFT};
    border-color: {SUCCESS_BORDER};
}}
QLabel#suggestionTitle {{ color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; }}
QLabel#suggestionBody {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
QLabel#suggestionBody[rank="discouraged"] {{ color: {TEXT_MUTED}; }}
QLabel#clearanceSectionTitle {{ color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 600; }}
QLabel#clearanceSectionHint {{ color: {TEXT_MUTED}; font-size: 11px; }}
QLabel#clearanceNotice {{
    background: {WARNING_TINT};
    border: 1px solid {WARNING_BORDER};
    border-radius: 4px;
    padding: 6px 8px;
    color: {WARNING};
    font-size: 11px;
}}
QTableWidget#bottleneckTable {{ border: 1px solid {BORDER}; border-radius: 5px; }}
QFrame#cornerInputColumn {{
    background: {PANEL_BASE};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#cornerDegreeTitle {{ color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; }}
QLabel#cornerDegreeTitle[emphasis="third"] {{ color: {ACCENT_DEEP}; }}
QLabel#cornerDegreeValue {{
    color: {ACCENT_DEEP};
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
    font-weight: 700;
}}
QLabel#cornerHint {{ color: {TEXT_MUTED}; font-size: 11px; }}
"""


SCENARIO_STYLESHEET = f"""
QWidget#scenarioSidebar {{
    background: {PANEL_BASE};
    border-right: 1px solid {BORDER};
}}
QWidget#scenarioResults {{
    background: {CARD};
    border-left: 1px solid {BORDER};
}}
QToolButton#scenarioSegment {{
    min-height: 26px;
    padding: 2px 8px;
    border: 1px solid {INPUT_BORDER};
    border-radius: 4px;
    background: {CARD};
    color: {TEXT_SECONDARY};
    font-size: 11px;
}}
QToolButton#scenarioSegment:checked {{
    background: {ACCENT_TINT};
    border-color: {ACCENT_BORDER};
    color: {ACCENT_DEEP};
    font-weight: 700;
}}
QToolButton#scenarioSegment:disabled {{ color: {TEXT_FAINT}; background: {HEADER_BASE}; }}
QToolButton#scenarioCard {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background: {CARD};
    padding: 6px 4px;
    color: {TEXT_PRIMARY};
    font-size: 11px;
    font-weight: 600;
}}
QToolButton#scenarioCard:checked {{
    background: {ACCENT_TINT};
    border-color: {ACCENT_BORDER};
}}
QLabel#scenarioCardSubtitle {{ color: {TEXT_MUTED}; font-size: 10px; }}
QLabel#scenarioFixedGear {{
    background: {HEADER_BASE};
    border: 1px solid {BORDER};
    border-radius: 4px;
    padding: 4px 8px;
    color: {TEXT_MUTED};
    font-size: 11px;
}}
QLabel#scenarioHint, QLabel#scenarioFootnote {{ color: {TEXT_MUTED}; font-size: 11px; }}
QLabel#scenarioRowLabel {{ color: {TEXT_SECONDARY}; font-size: 12px; }}
QLabel#scenarioRowValue {{
    color: {TEXT_SECONDARY};
    font-family: "Consolas", "DejaVu Sans Mono", monospace;
    font-size: 12px;
}}
QLabel#scenarioRowValue[kind="solved"] {{ color: {SUCCESS}; font-weight: 700; }}
QLabel#scenarioRowValue[kind="offset"] {{ color: {ACCENT_DEEP}; font-weight: 700; }}
QLabel#scenarioRowValue[state="danger"] {{ color: {DANGER}; font-weight: 700; }}
QLabel#scenarioRowValue[state="warning"] {{ color: {WARNING}; font-weight: 700; }}
QLabel#scenarioRowValue[state="success"] {{ color: {SUCCESS}; font-weight: 700; }}
QFrame#scenarioCanvas {{
    background: {CANVAS_BASE};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QLabel#scenarioTitle {{ color: {TEXT_PRIMARY}; font-size: 13px; font-weight: 600; }}
QFrame#scenarioCard {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 6px;
}}
QFrame#scenarioCard[tone="quiet"] {{ background: {CANVAS_BASE}; border-color: {BORDER_FAINT}; }}
QLabel#scenarioCardTitle {{ color: {TEXT_PRIMARY}; font-size: 12px; font-weight: 600; }}
QLabel#scenarioNotice {{
    background: {WARNING_TINT};
    border: 1px solid {WARNING_BORDER};
    border-radius: 4px;
    padding: 6px 8px;
    color: {WARNING};
    font-size: 11px;
}}
QGroupBox#scenarioGroup {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    background: {CARD};
    margin-top: 9px;
    padding-top: 6px;
    font-size: 12px;
    font-weight: 600;
}}
QGroupBox#scenarioGroup::title {{
    subcontrol-origin: margin;
    left: 9px;
    padding: 0 4px;
    color: {TEXT_PRIMARY};
}}
"""
