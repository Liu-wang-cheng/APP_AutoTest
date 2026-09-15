# -*- coding: utf-8 -*-
"""用例 YAML 加载的可诊断性

背景: 本机装有企业透明加密(文件头 %TSD-Header-###%),被加密的文件
只有白名单进程(如 git.exe)能读到明文, Python 直接读会拿到密文。
此时 pytest 收集阶段抛的是一段 codec traceback, 既不说是哪个文件,
也不说为什么——排查成本很高。

load_yaml_file 负责把这类失败翻译成一句能直接定位问题的话。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.driver import load_yaml_file, YamlFileError


class TestLoadYamlFile:
    def test_正常utf8文件_能读出内容(self, tmp_path):
        p = tmp_path / "case.yaml"
        p.write_text("module: 冒烟\ncases:\n  - name: A\n    steps: []\n", encoding="utf-8")
        data = load_yaml_file(str(p))
        assert data["module"] == "冒烟"
        assert data["cases"][0]["name"] == "A"

    def test_空文件_返回空字典(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        assert load_yaml_file(str(p)) == {}

    def test_透明加密文件_报错含文件名(self, tmp_path):
        """密文文件必须以文件名报错,而不是抛裸 UnicodeDecodeError"""
        p = tmp_path / "快速建图.yaml"
        p.write_bytes(b"%TSD-Header-###%" + os.urandom(64))
        with pytest.raises(YamlFileError) as ei:
            load_yaml_file(str(p))
        assert "快速建图.yaml" in str(ei.value)

    def test_透明加密文件_报错说明原因和处置(self, tmp_path):
        p = tmp_path / "加密的.yaml"
        p.write_bytes(b"%TSD-Header-###%" + os.urandom(64))
        with pytest.raises(YamlFileError) as ei:
            load_yaml_file(str(p))
        msg = str(ei.value)
        assert "透明加密" in msg          # 说清是什么问题
        assert "明文" in msg or "git" in msg.lower()  # 说清怎么办

    def test_普通非utf8文件_也报YamlFileError(self, tmp_path):
        """不含加密头但编码不对的文件,同样要给出文件名"""
        p = tmp_path / "gbk.yaml"
        p.write_bytes("module: 中文\n".encode("gbk"))
        with pytest.raises(YamlFileError, match="gbk.yaml"):
            load_yaml_file(str(p))

    def test_YamlFileError_是ValueError子类(self):
        """便于调用方按配置错误统一处理"""
        assert issubclass(YamlFileError, ValueError)

    def test_yaml语法错误_报错含文件名(self, tmp_path):
        p = tmp_path / "bad.yaml"
        p.write_text("module: [unclosed\n", encoding="utf-8")
        with pytest.raises(YamlFileError, match="bad.yaml"):
            load_yaml_file(str(p))
