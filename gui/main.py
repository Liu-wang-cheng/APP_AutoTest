"""GUI 入口: python gui/main.py 或 python -m gui.main"""
import os
import sys

# 支持直接 python gui/main.py 启动(把项目根加进 sys.path)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication

from gui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("vacuum_case_studio")
    win = MainWindow()
    # 默认 1120x640,适配 1366x768 笔记本;用户仍可自由拉伸
    win.resize(1120, 640)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
