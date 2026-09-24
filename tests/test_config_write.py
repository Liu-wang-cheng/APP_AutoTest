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


def test_update_scalar_to_empty(tmp_path):
    """清空配置: 空值写成 "" 而不是留空 —— 留空会被读成 None, 与"空串"语义混淆"""
    import yaml
    p = _write(tmp_path)
    update_config({"target_device": ""}, path=p)
    out = p.read_text(encoding="utf-8")
    assert 'target_device: ""  # APP内设备名' in out, out
    assert yaml.safe_load(out)["target_device"] == ""
    # 嵌套键同样(删除当前 APP 名称时走这条)
    update_config({"app.name": ""}, path=p)
    out = p.read_text(encoding="utf-8")
    assert 'name: ""   # 被测APP' in out, out
    assert yaml.safe_load(out)["app"]["name"] == ""


def test_crlf_preserved(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(SAMPLE.replace("\n", "\r\n"), encoding="utf-8", newline="")
    update_config({"app.name": "SmartThings"}, path=p)
    # 必须用字节读: read_text 的通用换行模式会把 \r\n 归一化成 \n,
    # 用它断言等于什么都没验证。
    raw = p.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")   # 没有残留的裸 LF


# ── 值里含 '#' : 与"行内注释"的区分(2026-09-24 审查修复) ──

def test_hash_value_idempotent(tmp_path):
    """值里含 # 时, 同值回写必须字节不变。

    ★ 回归守护: 取"值"的正则曾用 `[^\n#]*?`, 分不清「引号内的 #」与「行内注释」,
      于是把 `"Robot #1"` 的尾巴 ` #1"` 当成注释原样保留 —— 每次回写都多累积一份
      (`"Robot #1" #1" #1"`)。真机场景: 设备名/APP 名带 # 号时, 每保存一次脏一分。
    """
    p = tmp_path / "config.yaml"
    p.write_text("app:\n  name: 旧\n  package: com.a\n", encoding="utf-8", newline="\n")
    update_config({"app.name": "Sweeper #2"}, path=p)
    first = p.read_bytes()
    assert b'name: "Sweeper #2"' in first
    for _ in range(2):                     # 再写两次同值
        update_config({"app.name": "Sweeper #2"}, path=p)
    assert p.read_bytes() == first, (
        f"同值回写累积了垃圾: {p.read_text(encoding='utf-8')!r}")


def test_hash_value_and_real_comment_coexist(tmp_path):
    """含 # 的值 + 真实行内注释并存: 注释保留, 值不被截断"""
    import yaml
    p = tmp_path / "config.yaml"
    p.write_text("app:\n  name: 旧  # 备注\n", encoding="utf-8", newline="\n")
    update_config({"app.name": "A #1"}, path=p)
    out = p.read_text(encoding="utf-8")
    assert "# 备注" in out, out
    assert yaml.safe_load(out)["app"]["name"] == "A #1", out


def test_hash_value_top_scalar_idempotent(tmp_path):
    """顶层键(如 target_device)同样要能处理含 # 的值"""
    import yaml
    p = _write(tmp_path)
    update_config({"target_device": "Floor #3"}, path=p)
    first = p.read_bytes()
    update_config({"target_device": "Floor #3"}, path=p)
    assert p.read_bytes() == first, p.read_text(encoding="utf-8")
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["target_device"] == "Floor #3"
    assert "# APP内设备名" in p.read_text(encoding="utf-8")   # 真注释仍在


# ── 原子写: 失败不得破坏原文件 ──

def test_write_failure_leaves_file_intact(tmp_path, monkeypatch):
    """写入阶段失败(磁盘满/被占用/中断)时, 原文件必须完好。

    ★ 回归守护: 原实现直接 `open(path, "w")` —— 该模式**立即截断**目标文件,
      之后才写入; 中途出错就只剩半截甚至 0 字节。用户的 config.yaml 被 gitignore
      忽略、没有版本保护, 丢了只能手工重建。
    """
    import os
    import pytest
    p = tmp_path / "config.yaml"
    original = "app:\n  name: 原值\n"
    p.write_text(original, encoding="utf-8", newline="\n")

    def boom(src, dst):
        raise OSError("模拟: 落盘阶段失败")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        update_config({"app.name": "新值"}, path=p)

    assert p.read_text(encoding="utf-8") == original, "写入失败后原文件被破坏了"
    leftovers = [f for f in os.listdir(tmp_path) if f.startswith(".tmp_")]
    assert not leftovers, f"临时文件未清理: {leftovers}"
