# -*- coding: utf-8 -*-
"""临时调试: 组合控件勾选/文本行为。用后删除。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
import gui.main_window as mw
w = mw.MainWindow()
w.resize(900, 700)
w.show()
w.create_case("t", group="t")
w.add_step("click")
cards = [w.cards_lay.itemAt(i).widget() for i in range(w.cards_lay.count())]
cards = [c for c in cards if hasattr(c, "widgets")]
tf = cards[0].widgets["click"]
sys.stderr.write(f"初始 checked={tf.chk.isChecked()} value={tf.current_value()!r}\n")
tf.chk.setChecked(True)
app.processEvents()
sys.stderr.write(f"勾选后 combo_visible={tf.combo.isVisible()} currentText={tf.combo.currentText()!r}\n")
tf.combo.setEditText("开始清扫")
app.processEvents()
sys.stderr.write(f"setEditText 后 currentText={tf.combo.currentText()!r} value={tf.current_value()!r}\n")
w.close()
