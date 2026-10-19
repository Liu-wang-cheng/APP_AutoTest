# -*- coding: utf-8 -*-
"""tools/selector_health.py 的单元测试(不连设备, 用合成 APK)。"""
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "selector_health.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("selector_health", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CASE_YAML = """\
cases:
- name: 演示
  steps:
  - desc: 断言
    assert: 正在清洁
  - desc: 条件
    if: 今天,已取消
    then:
    - desc: 清除
      click: 540,660
    else:
    - desc: 菜单
      click: 菜单
  - desc: 模板
    click_template: 开始清扫.png
  - desc: 等待
    wait_for: 安静,标准
  - desc: 电量
    if: 电量>50
  - desc: 开关
    switch_label: 定制模式
"""

PRE_YAML = """\
preconditions:
- type: enter_device
  ready_text: 设备控制,服务
- type: map_load
  ready_text: 地图编辑
  loading_text: 地图正在加载
- type: charging
  ready_text: 充电,满电
  recharge_text: 返回
- type: steps
  steps:
  - desc: 烘干
    if not: 正在干燥拖布
"""

CFG_YAML = """\
app:
  package: com.example
runner:
  idle_texts: 待机,空闲
  fault_texts: 故障,错误
"""


@pytest.fixture
def fake_repo(tmp_path):
    cases = tmp_path / "Test_cases" / "三星"
    cases.mkdir(parents=True, exist_ok=True)
    (cases / "用例.yaml").write_text(CASE_YAML, encoding="utf-8")
    # ★ 外层 conftest 的 _isolate_group_preconditions 会预先建好 tmp_path/Test_preconditions
    pre = tmp_path / "Test_preconditions"
    pre.mkdir(parents=True, exist_ok=True)
    (pre / "三星.yaml").write_text(PRE_YAML, encoding="utf-8")
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(CFG_YAML, encoding="utf-8")
    return tmp_path


def make_apk(path, dex_texts=(), arsc_texts=(), asset_texts=()):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("classes.dex",
                   b"\x00" + b"\x00".join(t.encode("utf-8") for t in dex_texts) + b"\x00")
        z.writestr("resources.arsc",
                   b"\x00".join(t.encode("utf-16-le") for t in arsc_texts))
        z.writestr("assets/plugin/app.js",
                   " ".join(asset_texts).encode("utf-8"))
    return path


def test_clean_selector_filters_non_text_targets():
    sh = load_tool()
    assert sh.clean_selector("开始清扫.png") is None
    assert sh.clean_selector("540,660") is None
    assert sh.clean_selector("540") is None
    assert sh.clean_selector("50%") is None
    assert sh.clean_selector("电量>50") is None
    assert sh.clean_selector("http://x/y") is None
    assert sh.clean_selector("=已暂停") == ("text", "已暂停")
    assert sh.clean_selector("菜单") == ("text", "菜单")
    assert sh.clean_selector("com.x:id/btn") == ("resource-id", "com.x:id/btn")
    assert sh.clean_selector("2次") == ("text", "2次")


def test_collect_from_repo_extracts_texts_and_skips_targets(fake_repo):
    sh = load_tool()
    selectors = sh.collect_from_repo(str(fake_repo))
    expected = {
        "正在清洁", "今天", "已取消", "菜单", "安静", "标准", "定制模式",
        "设备控制", "服务", "地图编辑", "地图正在加载", "充电", "满电", "返回",
        "正在干燥拖布", "待机", "空闲", "故障", "错误",
    }
    assert expected <= set(selectors)
    for bad in ("开始清扫.png", "540", "660", "电量>50"):
        assert bad not in selectors
    # 来源可追溯
    assert any("用例.yaml" in s for s in selectors["正在清洁"]["sources"])
    assert any("三星.yaml" in s for s in selectors["正在干燥拖布"]["sources"])


def test_scan_apk_finds_dex_arsc_and_assets(tmp_path):
    sh = load_tool()
    apk = make_apk(tmp_path / "app.apk",
                   dex_texts=["正在清洁"],
                   arsc_texts=["菜单"],
                   asset_texts=["打扫报告"])
    selectors = {"正在清洁": {"kind": "text", "sources": set()},
                 "菜单": {"kind": "text", "sources": set()},
                 "打扫报告": {"kind": "text", "sources": set()},
                 "不存在文案": {"kind": "text", "sources": set()}}
    found = sh.scan_apk(str(apk), selectors)
    assert found["正在清洁"] == ["classes.dex"]
    assert found["菜单"] == ["resources.arsc"]
    assert found["打扫报告"] == ["assets/plugin/app.js"]
    assert found["不存在文案"] == []


def test_main_regression_exit_codes(fake_repo, tmp_path):
    sh = load_tool()
    apk = make_apk(tmp_path / "app.apk", dex_texts=["正在清洁"])
    baseline = tmp_path / "baseline.json"
    # 基线声称"菜单"曾经存在 → 当前 APK 找不到 = 回归
    baseline.write_text(json.dumps(
        {"selectors": {"菜单": {"found": True}, "正在清洁": {"found": True}}},
        ensure_ascii=False), encoding="utf-8")
    report = tmp_path / "report.json"

    code = sh.main(["--apk", str(apk), "--repo", str(fake_repo),
                    "--baseline", str(baseline), "--json", str(report)])
    assert code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    assert "菜单" in data["regressions"]
    assert data["summary"]["regressions"] >= 1

    # 允许回归时只报告不失败
    assert sh.main(["--apk", str(apk), "--repo", str(fake_repo),
                    "--baseline", str(baseline), "--allow-regressions"]) == 0


def test_main_without_baseline_only_reports(fake_repo, tmp_path):
    sh = load_tool()
    apk = make_apk(tmp_path / "app.apk", dex_texts=["正在清洁"])
    assert sh.main(["--apk", str(apk), "--repo", str(fake_repo)]) == 0
    # 有基线且 --strict 时，缺失(含无法判定)才失败
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"selectors": {}}, ensure_ascii=False), encoding="utf-8")
    assert sh.main(["--apk", str(apk), "--repo", str(fake_repo),
                    "--baseline", str(baseline), "--strict"]) == 1


def test_write_baseline(fake_repo, tmp_path):
    sh = load_tool()
    apk = make_apk(tmp_path / "app.apk", dex_texts=["正在清洁", "菜单"])
    out = tmp_path / "out" / "selector_baseline.json"
    assert sh.main(["--apk", str(apk), "--repo", str(fake_repo),
                    "--write-baseline", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["apk"]["sha256"]
    assert data["selectors"]["正在清洁"]["found"] is True
    assert data["selectors"]["菜单"]["found"] is True
    assert data["summary"]["selectors"] > 0
