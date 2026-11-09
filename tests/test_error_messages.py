# -*- coding: utf-8 -*-
from core.error_messages import friendly_error


def test_no_android_device_is_explained():
    msg = friendly_error(RuntimeError("Can't find any android device/emulator"))
    assert "未找到 Android 设备/模拟器" in msg
    assert "adb devices" in msg
    assert "原始错误" in msg


def test_unauthorized_and_offline_are_explained():
    assert "允许 USB 调试" in friendly_error(Exception("device unauthorized"))
    assert "离线" in friendly_error(Exception("device offline"))
    assert "多个 Android 设备" in friendly_error(Exception("more than one device"))


def test_unknown_error_keeps_original_text():
    assert friendly_error(RuntimeError("some weird failure")) == "some weird failure"
