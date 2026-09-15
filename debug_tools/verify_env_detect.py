# -*- coding: utf-8 -*-
"""环境检测链路验证: 关键词匹配 / 配置回写(保注释) / 真机端到端检测 / GUI 离屏
运行: python debug_tools/verify_env_detect.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import app_detect
from common.driver import load_config, update_config, CONFIG_PATH

# ── 1. 关键词与包名匹配 ──
kws = app_detect.name_keywords("涂鸦智能")
assert "tuya" in kws, kws
matched = app_detect.match_packages(
    "涂鸦智能", ["com.tuya.smartiot", "com.android.chrome", "com.example.game"])
assert matched[0][0] == "com.tuya.smartiot", matched
m2 = app_detect.match_packages("tuya", ["com.tuya.smartiot", "com.foo"])
assert m2[0][0] == "com.tuya.smartiot"
m3 = app_detect.match_packages("不存在的应用xyz", ["com.tuya.smartiot"])
assert m3 == []
print("1. 关键词/包名匹配 OK:", matched)

# ── 2. update_config 回写保注释 ──
with open(CONFIG_PATH, encoding="utf-8") as f:
    before = f.read()
cfg = load_config()
# 同值回写 → 文件字节不变(注释与格式保留)
update_config({"app.name": cfg["app"]["name"],
               "app.package": cfg["app"]["package"],
               "app.main_activity": cfg["app"]["main_activity"]})
with open(CONFIG_PATH, encoding="utf-8") as f:
    same = f.read()
assert same == before, "同值回写不应改变文件"
# 异值回写 → yaml 解析正确且注释仍在
update_config({"app.name": "测试临时名", "device.name:127.0.0.1:5555": "模拟器A"})
cfg2 = load_config()
assert cfg2["app"]["name"] == "测试临时名"
assert any(d["name"] == "模拟器A" and d["id"] == "127.0.0.1:5555"
           for d in cfg2["device"]["list"])
assert "# ── APP 配置 ──" in same and "被测APP名称" in before
# 还原
update_config({"app.name": cfg["app"]["name"],
               "device.name:127.0.0.1:5555": cfg["device"]["list"][0].get("name", "模拟器")})
# 顶层标量 target_device(APP 内设备名称): 同值字节不变 / 异值生效且保注释
update_config({"target_device": cfg.get("target_device", "SE3L")})
update_config({"target_device": "临时设备名"})
cfg_t = load_config()
assert cfg_t["target_device"] == "临时设备名"
update_config({"target_device": cfg.get("target_device", "SE3L")})
with open(CONFIG_PATH, encoding="utf-8") as f:
    final_text = f.read()
assert "进入哪个设备页面" in final_text, "行内注释丢失"
print("2. update_config 回写/保注释/还原/顶层标量 OK")

# ── 3. 真机端到端检测 ──
cfg = load_config()
# 用 resolve_devices(自动补连 + 同设备去重 + 挂备注名),与 GUI 下拉框同源
devices = app_detect.resolve_devices(cfg)
print("3. 在线设备:", devices)
assert devices, "无在线设备"
device_id = devices[0]["id"]
pkgs = app_detect.list_packages(device_id)
assert "com.tuya.smartiot" in pkgs
matched = app_detect.match_packages("涂鸦智能", pkgs)
print("   匹配结果前3:", matched[:3])
package = matched[0][0]
activity = app_detect.detect_main_activity(device_id, package)
assert package == cfg["app"]["package"], (package, cfg["app"]["package"])
assert activity == cfg["app"]["main_activity"], (activity, cfg["app"]["main_activity"])
print(f"   端到端 OK: 涂鸦智能 → {package} → {activity}")

# ── 4. GUI 离屏: 环境条 + 设备检测 + 保存联动 ──
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication
app = QApplication([])
from gui.main_window import MainWindow

w = MainWindow()
assert w.app_name_edit.text() == cfg["app"]["name"]
# 包名/启动页字段已从界面移除(检测结果只写配置),不应再存在
assert not hasattr(w, "pkg_edit") and not hasattr(w, "act_edit")
combo_ids = [w.device_combo.itemData(i) for i in range(w.device_combo.count())]
assert device_id in combo_ids, combo_ids
assert w._current_device_id() == device_id
# 设备名称 = APP 内设备名称(target_device) 同步
orig_target = cfg.get("target_device", "")
assert w.device_name_edit.text() == orig_target
w.device_name_edit.setText("SE9L-TEST")
w._save_cfg_field(w.device_name_edit)
assert load_config()["target_device"] == "SE9L-TEST"
update_config({"target_device": orig_target})  # 还原成原值(不要写死)
print("4. GUI 环境条/设备检测/设备名称(target_device)回写 OK")
print()
print("=== 环境检测链路全部通过 ===")
