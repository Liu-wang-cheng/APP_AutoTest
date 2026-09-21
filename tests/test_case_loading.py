# -*- coding: utf-8 -*-
"""load_yaml_file 的可诊断性: 正常/空/加密/非UTF-8 各自报错形态(守护源项目踩坑设计)"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.driver import load_yaml_file, YamlFileError

# 真实加密文件是二进制密文: 头部是 ASCII,但整体不是合法 UTF-8,读取时会抛
# UnicodeDecodeError —— 只有这种情况才会走到加密检测分支。
# 若整份内容都是 ASCII,"%TSD-Header" 会被 YAML 当成指令语法,报的是"解析失败"。
ENC_HEADER = b"%TSD-Header-###%\n\xff\xfe\x00\x01cipher-binary-bytes"


def test_normal(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("key: 值\n", encoding="utf-8")
    assert load_yaml_file(p) == {"key": "值"}


def test_empty(tmp_path):
    p = tmp_path / "a.yaml"
    p.write_text("", encoding="utf-8")
    assert load_yaml_file(p) == {}


def test_encrypted_reports_header(tmp_path):
    p = tmp_path / "enc.yaml"
    p.write_bytes(ENC_HEADER)
    with pytest.raises(YamlFileError) as e:
        load_yaml_file(p)
    msg = str(e.value)
    assert "enc.yaml" in msg and "%TSD-Header" in msg and "白名单" in msg


def test_non_utf8_reports_filename(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_bytes(b"\xff\xfe key: v")
    with pytest.raises(YamlFileError) as e:
        load_yaml_file(p)
    assert "bad.yaml" in str(e.value) and "UTF-8" in str(e.value)
