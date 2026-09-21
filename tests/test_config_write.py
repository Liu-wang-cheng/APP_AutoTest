# -*- coding: utf-8 -*-
"""update_config 保注释回写: 注释保留/CRLF保留/同值幂等/三种键形式"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.driver import update_config

SAMPLE = """# 顶部注释
app:
  name: 涂鸦智能   # 被测APP
  package: com.tuya.smartiot
target_device: SE3L  # APP内设备名
device:
  default: 127.0.0.1:5555
  list:
    - id: 127.0.0.1:5555
      name: 模拟器
runner:
  step_interval: 3
"""


def _write(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE, encoding="utf-8", newline="\n")
    return p


def test_update_scalar_keeps_comment(tmp_path):
    p = _write(tmp_path)
    update_config({"app.package": "com.samsung.android.oneconnect"}, path=p)
    out = p.read_text(encoding="utf-8")
    assert "package: com.samsung.android.oneconnect" in out
    assert "# 被测APP" in out          # 行内注释保留
    assert "# 顶部注释" in out         # 其他行不动


def test_update_top_scalar(tmp_path):
    p = _write(tmp_path)
    update_config({"target_device": "T4"}, path=p)
    out = p.read_text(encoding="utf-8")
    assert "target_device: T4  # APP内设备名" in out


def test_update_device_name_and_append(tmp_path):
    p = _write(tmp_path)
    update_config({"device.name:127.0.0.1:5555": "夜神"}, path=p)
    assert "name: 夜神" in p.read_text(encoding="utf-8")
    update_config({"device.name:emulator-5554": "新机"}, path=p)
    assert "name: 新机" in p.read_text(encoding="utf-8")   # 不存在则追加


def test_idempotent_same_value(tmp_path):
    p = _write(tmp_path)
    before = p.read_bytes()
    update_config({"target_device": "SE3L"}, path=p)
    assert p.read_bytes() == before       # 同值回写字节不变


def test_crlf_preserved(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SAMPLE.replace("\n", "\r\n"), encoding="utf-8", newline="")
    update_config({"app.name": "SmartThings"}, path=p)
    # 必须用字节读: read_text 的通用换行模式会把 \r\n 归一化成 \n,
    # 用它断言等于什么都没验证。
    raw = p.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")   # 没有残留的裸 LF
