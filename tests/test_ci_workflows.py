# -*- coding: utf-8 -*-
"""CI/CD 配置守护(用户 2026-10-16: "先做 A 和 B")。

两个 workflow 的分工:
  · `package.yml`(方案 A) —— 只证明"这个提交能出包", 传 artifact, **不发布**;
  · `release.yml`(方案 B) —— 全自动 CD, 传 GitHub Release + 更新 version.json + 打 tag。

这条测试把三件"写错了会在 CI 上以奇怪方式失败"的事钉住(都实际踩过):
  ① CD 里若让 release.py 自己查 CI 闸门 → 它查的是**自己这一轮**(in_progress) → 自我卡死;
     必须用 `--no-ci-check`, 由同 job 里 build.bat 的 pytest 兜住;
  ② tag 触发的运行是 detached HEAD → 必须显式 checkout master, 否则推不回 master;
  ③ CI 里没有 git 身份 → release.py 的 commit 会失败 → 必须先配 user.name/email。
另外钉住: CD 需要 contents: write(job 级声明, 不动仓库默认设置)、发布前的 tag↔master 校验、
方案 A 绝不触碰 Release/version.json。
"""
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"


def _load(name):
    p = WF / name
    assert p.is_file(), f"缺少 {name}"
    return yaml.safe_load(p.read_text(encoding="utf-8")), p.read_text(encoding="utf-8")


def test_workflows_are_valid_yaml_and_windows():
    for name in ("tests.yml", "package.yml", "release.yml"):
        data, _text = _load(name)
        # ★ YAML 1.1 会把裸键 `on:` 解析成布尔 True(经典坑) —— 两种情况都要认
        triggers = data.get("on") or data.get(True)
        assert triggers, f"{name} 没有触发条件"
        for job, spec in (data.get("jobs") or {}).items():
            assert spec.get("runs-on") == "windows-latest", f"{name}:{job} 必须在 Windows 上跑"


def test_package_workflow_never_publishes():
    """★ 方案 A 的边界: 只打包 + 传 artifact, **绝不**碰 Release/version.json/tag"""
    _data, text = _load("package.yml")
    assert "release.py --version" not in text, "方案 A 不该发布任何东西"
    assert "upload-artifact" in text, "方案 A 要把 zip 传成 artifact 供下载"
    assert "build.bat" in text, "方案 A 就是跑 build.bat"


def test_release_workflow_avoids_the_three_known_traps():
    """★ CD 的三个坑, 一个都不能漏(每个都在真实发版时踩过)"""
    _data, text = _load("release.yml")

    # 坑①: 不能用 release.py 自己的 CI 闸门(会查到自己 → 卡死)
    assert "--no-ci-check" in text, "CD 里必须跳过 release.py 的 CI 自查(否则自我卡死)"
    assert "build.bat" in text, "测试要由同 job 的 build.bat 兜住(它内部跑 pytest)"
    # 坑②: tag 触发是 detached HEAD → 显式 checkout master
    assert "ref: master" in text, "必须基于 master 发版(tag 触发时是 detached HEAD)"
    # 坑③: CI 里没有 git 身份
    assert "git config user.name" in text and "git config user.email" in text, \
        "CI 里要先配 git 身份, 否则 release.py 的 commit 会失败"


def test_release_workflow_permissions_and_guards():
    """★ 权限用 job 级最小声明; 发布前校验 tag 就在 master 上; 支持审批闸门"""
    data, text = _load("release.yml")
    assert (data.get("permissions") or {}).get("contents") == "write", \
        "建 Release/推提交需要 contents: write(写在 workflow 里, 不动仓库默认设置)"
    # ★ 全自动(用户 2026-10-16): 不设 Environment 审批闸门; 但要有 dry_run 演练路径,
    #   否则"验证流水线"就等于"真发一版"
    assert "environment:" not in text, "用户要求全自动, 不该有审批闸门"
    assert "dry_run" in text and "--pack-only" in text, "要有 dry_run 演练(只打包不发布)"
    assert "rev-list -n 1" in text, "tag 触发时要确认 tag 指向 master HEAD, 避免发错提交"
    assert "--dry-run" in text,         "发布前要预检「这个身份能不能推 master」—— 否则会留下半成品发布(Release 发了但清单没更新)"
    job = data["jobs"]["release"]
    assert job.get("timeout-minutes"), "发版 job 要有超时, 免得卡住占着 runner"


def test_build_bat_still_never_publishes():
    """★ 双保险: build.bat(CI 里跑的)自己绝不能发布 —— 只打包(--pack-only)"""
    text = (ROOT / "build.bat").read_text(encoding="utf-8", errors="replace")
    assert "--pack-only" in text, "build.bat 应该只打包"
    assert "--version %APPVER%" in text or "release.py --pack-only" in text
    assert "gh release" not in text and "create_release" not in text, \
        "build.bat 里不该出现任何发布动作"
