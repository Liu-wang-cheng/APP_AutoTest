# -*- coding: utf-8 -*-
"""临时验证: 新增历史后下拉尺寸(含 popup 从未打开过的场景)。用后删除。"""
import sys, os, faulthandler
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
faulthandler.dump_traceback_later(25, exit=True)
out = open("reports/_dbg_add2.txt", "w", encoding="utf-8")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QSettings
app = QApplication([])
st = QSettings("vacuum_test", "case_studio")
st.setValue("hist/app_name", ["甲", "乙", "丙"])
st.sync()

import gui.main_window as mw
w = mw.MainWindow()
w.resize(1180, 400)
w.show()
app.processEvents()

combo = w.app_name_edit
LONG = "超级无敌长的应用名称测试用例ABCDEF"

# 场景A: popup 从未打开 → 新增长名 → 再打开(用户报的路径)
combo.setCurrentText(LONG)
w._save_env_field_of(combo)
app.processEvents()
out.write(f"A. 历史={[combo.itemText(i) for i in range(combo.count())]}\n")
combo.showPopup()
app.processEvents()
p = combo.view().window()
out.write(f"   popup={p.width()}x{p.height()}  (4项应约 4 行高, 不能是单行小框)\n")
combo.hidePopup()
app.processEvents()

# 场景B: 打开状态下新增
combo.showPopup()
app.processEvents()
out.write(f"B. 打开后 popup={combo.view().window().width()}x{combo.view().window().height()}\n")
combo.setCurrentText("另一个名字")
w._save_env_field_of(combo)
app.processEvents()
p2 = combo.view().window()
out.write(f"   新增后 popup={p2.width()}x{p2.height()} 历史={combo.count()}项\n")
out.close()
os._exit(0)
