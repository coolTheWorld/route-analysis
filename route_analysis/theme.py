"""Application-wide visual tokens and accessible Qt stylesheet."""

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
