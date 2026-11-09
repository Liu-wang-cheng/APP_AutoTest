# -*- coding: utf-8 -*-
"""APOS SDK 目录管理：校验 / 复制 / license 提取。"""
from core.apos import sdk


def _make_src(tmp_path, license_text='self.apos_license = "test-lic"\n'):
    src = tmp_path / "src"
    src.mkdir()
    for name in ("librpc_base.dll", "libapos_client.dll"):
        (src / name).write_bytes(b"dll")
    (src / "libzmq-v141-mt-4_3_5.dll").write_bytes(b"zmq")
    if license_text is not None:
        (src / "AposSdkHelp.py").write_text(license_text, encoding="utf-8")
    return src


def test_check_sdk_reports_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = sdk.check_sdk(empty)
    assert res["ok"] is False
    assert "librpc_base.dll" in res["missing"]
    assert sdk.ZMQ_GLOB in res["missing"]
    assert sdk.LICENSE_NAME in res["missing"]


def test_install_copies_dlls_and_extracts_license(tmp_path):
    src = _make_src(tmp_path)
    dst = tmp_path / "dst"
    res = sdk.install_sdk(src, dst)
    assert res["ok"] is True, res
    assert (dst / "librpc_base.dll").exists()
    assert (dst / "libzmq-v141-mt-4_3_5.dll").exists()
    assert (dst / "license.dat").read_text(encoding="utf-8") == "test-lic"
    assert (dst / sdk.MANIFEST).exists()
    manifest = (dst / sdk.MANIFEST).read_text(encoding="utf-8")
    assert "source" in manifest and "files" in manifest


def test_extract_license_fails_when_missing(tmp_path):
    src = _make_src(tmp_path, license_text="print('no license')\n")
    try:
        sdk.extract_license(src)
    except ValueError as e:
        assert "apos_license" in str(e)
    else:
        raise AssertionError("应当抛 ValueError")


def test_install_refuses_missing_dll(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "AposSdkHelp.py").write_text('self.apos_license = "x"', encoding="utf-8")
    try:
        sdk.install_sdk(src, tmp_path / "dst")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("缺 DLL 时应抛 FileNotFoundError")


def test_load_license(tmp_path):
    (tmp_path / sdk.LICENSE_NAME).write_text("abc\n", encoding="utf-8")
    assert sdk.load_license(tmp_path) == "abc"
    assert sdk.load_license(tmp_path / "none") == ""
