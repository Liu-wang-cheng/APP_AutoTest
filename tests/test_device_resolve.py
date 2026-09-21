# -*- coding: utf-8 -*-
"""app_detect: host:port 识别/配置地址收集/android_id 去重/备注名(假 adb,不碰真机)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import core.app_detect as ad


def test_is_tcp_addr():
    assert ad.is_tcp_addr("127.0.0.1:5555")
    assert not ad.is_tcp_addr("emulator-5554")
    assert not ad.is_tcp_addr("")


def test_configured_addrs_dedup():
    cfg = {"device": {"default": "127.0.0.1:5555",
                      "list": [{"id": "127.0.0.1:5555"}, {"id": "10.0.0.2:5555"}]}}
    assert ad.configured_addrs(cfg) == ["127.0.0.1:5555", "10.0.0.2:5555"]


def test_resolve_devices_dedup_by_android_id(monkeypatch):
    # 同一台模拟器两种寻址: android_id 相同 → 只留一条,优先保留配置登记的
    cfg = {"device": {"default": "auto",
                      "list": [{"id": "127.0.0.1:5555", "name": "模拟器"}]}}
    monkeypatch.setattr(ad, "ensure_connected", lambda cfg: None)
    monkeypatch.setattr(ad, "list_devices",
                        lambda: [{"id": "emulator-5554", "state": "device"},
                                 {"id": "127.0.0.1:5555", "state": "device"}])
    monkeypatch.setattr(ad, "_device_key", lambda did: "android-id-1")
    devs = ad.resolve_devices(cfg)
    assert len(devs) == 1
    assert devs[0]["id"] == "127.0.0.1:5555"      # 配置里登记过的优先
    assert devs[0]["label"] == "模拟器 (127.0.0.1:5555)"


def test_resolve_devices_offline_suffix(monkeypatch):
    cfg = {"device": {"default": "auto", "list": []}}
    monkeypatch.setattr(ad, "ensure_connected", lambda cfg: None)
    monkeypatch.setattr(ad, "list_devices",
                        lambda: [{"id": "emulator-5554", "state": "offline"}])
    monkeypatch.setattr(ad, "_device_key", lambda did: None)   # 探测不到 → 按 id 独立
    devs = ad.resolve_devices(cfg)
    assert devs[0]["label"] == "emulator-5554 [offline]"


def test_name_keywords_chinese():
    kws = ad.name_keywords("涂鸦智能")
    assert "tuya" in kws                 # 拼音窗口切出 tuya


def test_name_keywords_alias():
    kws = ad.name_keywords("SmartThings")
    assert "oneconnect" in kws           # 别名表生效(包名对不上显示名的情况)


def test_name_keywords_english_passthrough():
    kws = ad.name_keywords("com.tuya.smartiot")
    assert "com.tuya.smartiot" in kws or "comtuyasmartiot" in kws


def test_match_packages_scores():
    pkgs = ["com.tuya.smartiot", "com.example.tuya.helper", "com.other.app"]
    scored = ad.match_packages("涂鸦智能", pkgs)
    assert scored[0][0] == "com.tuya.smartiot"    # 同分取更短包名
    assert all(p != "com.other.app" for p, _ in scored)


def test_match_packages_smartthings_alias():
    pkgs = ["com.samsung.android.oneconnect", "com.other.app"]
    scored = ad.match_packages("SmartThings", pkgs)
    assert scored and scored[0][0] == "com.samsung.android.oneconnect"


def test_别名对大小写和空格不敏感():
    """用户在 GUI 里填 APP 名时大小写/空格很随意,别名匹配必须扛得住"""
    pkgs = ["com.samsung.android.oneconnect"]
    for name in ("SmartThings", "smartthings", "Smart Things", " SmartThings "):
        assert ad.match_packages(name, pkgs), f"未匹配: {name!r}"


def test_别名不污染无关包():
    """别名只补关键词,不能让无关包被误判命中"""
    matched = ad.match_packages("SmartThings", ["com.tencent.mtt", "com.tuya.smartiot"])
    assert matched == []


def test_parse_brief_activity():
    out = "priority=0\ncom.tuya.smartiot/com.smart.ThingSplashActivity"
    assert ad._parse_brief_activity(out, "com.tuya.smartiot") == "com.smart.ThingSplashActivity"
