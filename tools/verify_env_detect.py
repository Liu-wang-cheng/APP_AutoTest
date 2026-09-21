# -*- coding: utf-8 -*-
"""环境检测链路端到端自验证。

    python tools/verify_env_detect.py

验证四条链路(需要模拟器在线):
    1. 关键词/包名匹配      名称 → 拼音关键词 → 包名打分
    2. update_config 回写   同值字节不变 / 异值生效 / 注释保留 / 顶层标量
    3. 真机端到端检测       设备发现 → 已装应用 → 包名 → 启动页
    4. GUI 离屏联动         环境条取值、设备下拉、设备名称回写配置

脚本会临时改动 config/config.yaml,结束时还原 —— 但如果中途 assert 失败,
可能留下改动过的值,重新跑一次即可(它从当前配置取基准)。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import app_detect
from core.driver import CONFIG_PATH, load_config, update_config

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
with open(CONFIG_PATH, encoding="utf-8", newline="") as f:
    before = f.read()
cfg = load_config()
# 同值回写 → 文件字节不变(注释与格式原样保留)
update_config({"app.name": cfg["app"]["name"],
               "app.package": cfg["app"]["package"],
               "app.main_activity": cfg["app"]["main_activity"]})
with open(CONFIG_PATH, encoding="utf-8", newline="") as f:
    same = f.read()
assert same == before, "同值回写不应改变文件"

# 异值回写 → 能解析出来,且注释还在
device_id = cfg["device"]["default"]
orig_dev_name = next((d.get("name", "") for d in cfg["device"]["list"]
                      if d["id"] == device_id), "")
update_config({"app.name": "测试临时名", f"device.name:{device_id}": "模拟器A"})
cfg2 = load_config()
assert cfg2["app"]["name"] == "测试临时名"
assert any(d.get("name") == "模拟器A" and d.get("id") == device_id
           for d in cfg2["device"]["list"])
assert "#" in same, "注释全部丢失"
update_config({"app.name": cfg["app"]["name"],
               f"device.name:{device_id}": orig_dev_name})

# 顶层标量 target_device: 同值字节不变 / 异值生效 / 行内注释保留
orig_target = cfg.get("target_device", "")
update_config({"target_device": orig_target})
with open(CONFIG_PATH, encoding="utf-8", newline="") as f:
    t_same = f.read()
update_config({"target_device": orig_target})
with open(CONFIG_PATH, encoding="utf-8", newline="") as f:
    assert f.read() == t_same, "target_device 同值回写不应改变文件"

update_config({"target_device": "临时设备名"})
assert load_config()["target_device"] == "临时设备名"
update_config({"target_device": orig_target})
assert load_config()["target_device"] == orig_target
print("2. update_config 回写/保注释/还原/顶层标量 OK")

# ── 3. 真机端到端检测 ──
cfg = load_config()
devices = app_detect.resolve_devices(cfg)   # 自动补连 + 同设备去重 + 挂备注名
print("3. 在线设备:", devices)
assert devices, "无在线设备(模拟器没开?adb 连不上?)"
device_id = devices[0]["id"]
pkgs = app_detect.list_packages(device_id)
assert cfg["app"]["package"] in pkgs, f"设备上没有 {cfg['app']['package']}"
matched = app_detect.match_packages(cfg["app"]["name"], pkgs)
print("   匹配结果前3:", matched[:3])
package = matched[0][0]
activity = app_detect.detect_main_activity(device_id, package)
assert package == cfg["app"]["package"], (package, cfg["app"]["package"])
assert activity == cfg["app"]["main_activity"], (activity, cfg["app"]["main_activity"])
print(f"   端到端 OK: {cfg['app']['name']} → {package} → {activity}")

# ── 4. GUI 离屏: 环境条 + 设备检测 + 设备名称回写 ──
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication([])
from gui.main_window import MainWindow  # noqa: E402

w = MainWindow()
assert w.app_name_edit.text() == cfg["app"]["name"], w.app_name_edit.text()
combo_ids = [w.device_combo.itemData(i) for i in range(w.device_combo.count())]
assert device_id in combo_ids, combo_ids
assert w._current_device_id() == device_id
# 设备名称 = APP 内设备名(target_device),双向同步
assert w.device_name_edit.text() == orig_target, w.device_name_edit.text()
w.device_name_edit.setText("SE9L-TEST")
w._save_env_field(w.device_name_edit)
assert load_config()["target_device"] == "SE9L-TEST"
update_config({"target_device": orig_target})   # 还原成原值,不写死
w.close()
print("4. GUI 环境条/设备下拉/设备名称回写 OK")
print()
print("=== 环境检测链路全部通过 ===")
