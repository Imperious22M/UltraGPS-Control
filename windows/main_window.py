import os

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QFrame,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont

from ultragps_client import UltraGPSClient
from SettingsModule import SettingsModule
from ultragps_position import UltraGPSPositionLib
from ultragps_calibration import UltraGPSCalibration


class UltraGPSMainWindow(QMainWindow):
    """Top-level application window.  Owns shared resources and manages panel navigation."""

    def __init__(self, ip_address: str = "127.0.0.1", config_path: str = None,
                 server=None):
        super().__init__()
        self.setWindowTitle("UltraGPS Control")
        self.setMinimumSize(1200, 900)
        self.setStyleSheet("background-color: black;")
        self.server = server

        # Shared resources
        self.client = UltraGPSClient(host=ip_address)
        self.client.connect()

        self.settings_module = SettingsModule()
        if config_path is None:
            config_path = self.settings_module._config_path
        self._config_path = config_path

        self.position_lib = UltraGPSPositionLib(config_path)
        self.cal = UltraGPSCalibration(config_path, self.client)

        # Central stacked widget — each panel occupies a full page
        self._stack = QStackedWidget()
        self.setCentralWidget(self._stack)

        self._panels: dict[str, QWidget] = {}
        self._init_panels()
        self.show_panel('main')

    def _init_panels(self) -> None:
        from windows.position_window import PositionPanel
        from windows.calibration_window import CalibrationPanel
        from windows.arena_maker_window import ArenaMakerPanel
        from windows.barrier_drawer_window import BarrierDrawerPanel

        panels = [
            ('main',           MainMenuPanel(main_window=self, server=self.server)),
            ('position',       PositionPanel(
                                   client=self.client,
                                   position_lib=self.position_lib,
                                   settings_module=self.settings_module,
                                   main_window=self,
                                   server=self.server)),
            ('calibration',    CalibrationPanel(
                                   cal=self.cal,
                                   settings_module=self.settings_module,
                                   main_window=self)),
            ('arena_maker',    ArenaMakerPanel(
                                   settings_module=self.settings_module,
                                   main_window=self)),
            ('barrier_drawer', BarrierDrawerPanel(
                                   settings_module=self.settings_module,
                                   main_window=self)),
        ]

        for name, panel in panels:
            self._stack.addWidget(panel)
            self._panels[name] = panel

    def show_panel(self, name: str) -> None:
        current = self._stack.currentWidget()
        if current and hasattr(current, 'on_panel_hide'):
            current.on_panel_hide()

        panel = self._panels[name]
        self._stack.setCurrentWidget(panel)

        if hasattr(panel, 'on_panel_show'):
            panel.on_panel_show()

    def closeEvent(self, event) -> None:
        for panel in self._panels.values():
            if hasattr(panel, 'on_panel_hide'):
                try:
                    panel.on_panel_hide()
                except Exception:
                    pass
        try:
            self.client.disconnect()
        except Exception:
            pass
        event.accept()


class ServerStatusWidget(QFrame):
    """A compact status panel that shows UltraGPSServer state."""

    def __init__(self, server=None):
        super().__init__()
        self._server = server
        self.setStyleSheet(
            "background-color: #111111; border: 1px solid #333333; "
            "border-radius: 4px;"
        )
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        title_lbl = QLabel("Server Status")
        title_lbl.setStyleSheet("color: #FF8800; font: bold 11px Arial;")
        layout.addWidget(title_lbl)

        self._ip_label = QLabel("")
        self._ip_label.setStyleSheet("color: #AAAAAA; font: 10px Arial;")
        layout.addWidget(self._ip_label)

        self._ports_label = QLabel("")
        self._ports_label.setStyleSheet("color: #AAAAAA; font: 10px Arial;")
        layout.addWidget(self._ports_label)

        self._clients_label = QLabel("")
        self._clients_label.setStyleSheet("color: #AAAAAA; font: 10px Arial;")
        layout.addWidget(self._clients_label)

        self._refresh()

        if self._server is not None:
            self._timer = QTimer(self)
            self._timer.setInterval(1500)
            self._timer.timeout.connect(self._refresh)
            self._timer.start()

    def _refresh(self) -> None:
        if self._server is None:
            for lbl in (self._ip_label, self._ports_label, self._clients_label):
                lbl.setText("Server not running")
            return

        status = self._server.get_status()

        streaming = status['streaming']
        ip_text = f"IP: {status['ip']}  |  Streaming: {'ON' if streaming else 'OFF'}"
        self._ip_label.setText(ip_text)
        color = "#39FF14" if streaming else "#AAAAAA"
        self._ip_label.setStyleSheet(f"color: {color}; font: 10px Arial;")

        ports_parts = [
            f"{p['proto']} {p['name']}:{p['port']} ({p['clients']} clients)"
            for p in status['ports']
        ]
        self._ports_label.setText("  |  ".join(ports_parts))
        self._ports_label.setStyleSheet("color: #AAAAAA; font: 10px Arial;")

        self._clients_label.setText(f"Total connected: {status['total_clients']}")
        self._clients_label.setStyleSheet("color: #AAAAAA; font: 10px Arial;")


class MainMenuPanel(QWidget):
    """Main menu with navigation buttons."""

    def __init__(self, main_window: UltraGPSMainWindow, server=None):
        super().__init__()
        self._main_window = main_window
        self._server = server
        self.setStyleSheet("background-color: black;")
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(0)

        title = QLabel("Ultra GPS Control")
        title.setFont(QFont('Arial', 32, QFont.Weight.Bold))
        title.setStyleSheet("color: #39FF14;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addSpacing(10)

        status_widget = ServerStatusWidget(self._main_window.server)
        layout.addWidget(status_widget, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(20)

        buttons = [
            ("Position",    'position',       '#39FF14', 'black',  '#2BCC10'),
            ("Calibration", 'calibration',    '#FF00FF', 'white',  '#CC00CC'),
            ("Setup",       'arena_maker',    '#00FFFF', 'black',  '#00CCCC'),
            ("Barriers",    'barrier_drawer', '#FF8800', 'black',  '#CC7000'),
        ]

        for text, panel_name, bg, fg, hover in buttons:
            btn = QPushButton(text)
            btn.setStyleSheet(f"""
                QPushButton {{
                    background-color: {bg};
                    color: {fg};
                    font: bold 16px Arial;
                    padding: 15px 40px;
                    border-radius: 5px;
                    min-width: 220px;
                }}
                QPushButton:hover {{ background-color: {hover}; }}
            """)
            btn.clicked.connect(
                lambda checked, p=panel_name: self._main_window.show_panel(p))
            layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)
            layout.addSpacing(10)
