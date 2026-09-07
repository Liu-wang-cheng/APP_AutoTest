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
    # 默认 1000x600;顶部横条为流式布局,窗口任意拖小会自动换行
    win.resize(1000, 600)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
