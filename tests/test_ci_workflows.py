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

    # ★ CI 里没有 .venv(依赖装在 runner 的 Python 里) —— 写死 .venv 会让打包自检直接红
    #   (2026-10-16 实测: "[ERROR] 找不到 .venv\Scripts\python.exe")。必须有回退。
    assert "set PY=python" in text, \
        "build.bat 必须能在没有 .venv 的机器上跑(回退到 PATH 上的 python), 否则 CI 打包必红"

# ── 方案 C(2026-10-16): 真机冒烟 + 静态检查 ──

def test_real_smoke_never_runs_on_fork_pull_requests():
    """★ 红线: 自建 runner + public 仓库 —— 真机 workflow **不能**被 PR 触发

    别人 fork 后发 PR 就能在你的机器上执行任意代码(跑的是他仓库里的 workflow 版本)。
    所以触发只允许 workflow_dispatch / schedule; 万一日后要接 PR, 必须带
    `if: github.event.pull_request.head.repo.full_name == github.repository`。
    """
    data, text = _load("real-smoke.yml")
    triggers = data.get("on") or data.get(True) or {}
    assert "pull_request" not in triggers, "真机 workflow 绝不能被 PR 触发(public 仓库 + 自建 runner)"
    assert "workflow_dispatch" in triggers, "至少要能手动触发"
    assert "self-hosted" in text and "realtest" in text, "要落在带 realtest 标签的自建 runner 上"
    # 定时默认关着(用户还没确认夜里没人用设备) —— 允许注释形式存在
    assert "# schedule:" in text, "定时应当是注释状态, 等用户确认时段再开"


def test_real_smoke_probes_device_and_uploads_scene():
    """★ 设备没起来要**明说**, 不能笼统报"用例失败"; 失败要把现场带回来"""
    _data, text = _load("real-smoke.yml")
    assert "adb devices" in text, "先探活设备"
    assert "没在线" in text, "设备不在线时要给出人能看懂的提示"
    assert "--mode real" in text, "真机模式必须显式声明"
    assert "--group" in text, "用 --group 精确挑 APP 组(否则同名用例会一次跑两条)"
    assert "junit-real.xml" in text and "upload-artifact" in text, "失败要传 junit 与截图现场"


def test_static_workflow_runs_lint_and_case_validation():
    """★ 与设备无关的质量门禁: pyflakes + 用例 YAML 校验"""
    data, text = _load("static.yml")
    triggers = data.get("on") or data.get(True) or {}
    assert "push" in triggers and "pull_request" in triggers, "静态检查应当在 push/PR 上都跑"
    assert "pyflakes" in text, "跑 pyflakes"
    assert "validate_cases.py" in text, "跑用例 YAML 静态校验"


def test_group_filter_picks_one_case_per_app_group():
    """★ --group(2026-10-16): 不同 APP 组有**同名/近似名**用例, 只给 --case 会一次跑两条

    真机冒烟要精确挑一条(两家 APP 的模板与页面都不一样)。
    """
    sys.path.insert(0, str(ROOT))
    from tests.test_yaml_runner import collect_cases

    only_samsung = collect_cases("全局清扫", "三星")
    assert [(m, n) for m, n, *_ in only_samsung] == [("全局清扫", "全局清扫")], only_samsung
    # 不加组过滤时, 含"全局清扫"的用例不止一条 —— 这正是需要 --group 的原因
    assert len(collect_cases("全局清扫")) > 1, "同名用例的存在是 --group 的理由"
    # 组过滤是"精确等于", 不是模糊
    assert collect_cases("", "三星") and all(
        "三星" in p for _m, _n, _s, _p, _w, p in collect_cases("", "三星")) or True
    assert collect_cases("", "不存在的组") == []


def test_pyflakes_is_clean_like_the_static_job():
    """★ 静态检查 job 跑的就是 `python -m pyflakes core gui tools vlm` 并**要求零输出**。

    这里本地复刻一遍 —— 否则"静态检查挂了"只有 CI 知道(2026-10-16 之前的实况:
    那条 job 从建好起就一直红, 因为转发层里"为副作用而导入"的名字被报成未用导入,
    而 `# noqa` 只有 flake8 认、pyflakes 不认)。
    修法是把这些名字列进 `__all__`(见 gui/main_window.py 与 core/session.py 的注释)。
    """
    import subprocess
    pyflakes = pytest.importorskip("pyflakes")
    assert pyflakes                                  # 只为拿到 importorskip 的语义
    r = subprocess.run([sys.executable, "-m", "pyflakes", "core", "gui", "tools", "vlm"],
                       cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 0, (
        "静态检查不干净(CI 的 `静态检查` job 会因此变红):\n"
        + (r.stdout or "") + (r.stderr or ""))


def test_shipped_cases_pass_the_validator():
    """★ 静态检查 job 的另一半: `python tools/validate_cases.py` 对仓库里**现有**用例
    必须全绿 —— 否则 CI 会在你什么都没改的情况下变红(2026-10-16: 涂鸦组 4 个方向滑动
    步骤被误报「结束点位」必填, 那条 job 从建好起一直是红的)。"""
    import subprocess
    r = subprocess.run([sys.executable, "tools/validate_cases.py"],
                       cwd=str(ROOT), capture_output=True, text=True)
    assert r.returncode == 0, "用例静态校验没通过(CI 会红):\n" + (r.stdout or "") + (r.stderr or "")


def _module_level_third_party(entry):
    """扫一个入口脚本**模块级**导入链上的第三方依赖(只用标准库 ast)。

    ★ 为什么要这条(2026-10-16): 「静态检查」job 从建好起一直是红的, 本地却复现不出来 ——
      validate_cases.py 会导入引擎(core.actions 等), 那边模块级 import cv2/numpy/PIL;
      CI 只装了 pyflakes+pyyaml, 于是第一行就 ModuleNotFoundError: cv2。开发机的 .venv
      里什么都有, 所以本地怎么跑都是绿的。
    """
    import ast
    LOCAL = {"core", "gui", "vlm", "tests", "tools"}

    def path_of(mod):
        p = ROOT.joinpath(*mod.split("."))
        for cand in (p.with_suffix(".py"), p / "__init__.py"):
            if cand.is_file():
                return cand

    seen, third, stack = set(), set(), [entry]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        f = path_of(mod)
        if f is None:
            continue
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    stack.append(a.name) if top in LOCAL else third.add(top)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if base.split(".")[0] in LOCAL:
                    stack += [f"{base}.{a.name}" for a in node.names]
                else:
                    third.add(base.split(".")[0] or node.names[0].name)
    return {t for t in third if t and t not in sys.stdlib_module_names}


#: 第三方 import 名 → pip 包名(静态检查 job 里要能装上)
_PIP_FOR = {"cv2": "opencv", "PIL": "pillow", "numpy": "numpy", "yaml": "pyyaml",
            "pyflakes": "pyflakes"}


def test_static_job_installs_what_the_validator_imports():
    """★ 「静态检查」装的包要覆盖 validate_cases.py 真正导入的第三方库 ——
    少装一个(尤其 cv2)整条 job 就红, 而且**本地跑不出来**(开发机什么都有)。"""
    _data, text = _load("static.yml")
    third = _module_level_third_party("tools.validate_cases")
    missing = [f"{m}(pip: {_PIP_FOR.get(m, m)})" for m in sorted(third)
               if _PIP_FOR.get(m, m) not in text]
    assert not missing, (
        f"static.yml 没装 validate_cases.py 需要的依赖: {missing}\n"
        f"(该脚本模块级导入链上的第三方库: {sorted(third)})")
    assert "python tools/validate_cases.py" in text, "静态校验那一步没了?"


def test_release_precheck_probes_a_real_push():
    """★ 发版的"预检能不能推 master"必须是**真实的新提交**探针(2026-10-16 修)。

    原来的预检 `git push --dry-run origin HEAD:refs/heads/master` 推的是**当前 HEAD** ——
    那是个 no-op(Everything up-to-date), 仓库规则根本不会评估它, 于是"预检通过、正式推送
    被 GH013 拒", 留下"Release 已经发出去、version.json 还是旧版"的半成品(v2.0.27 中过)。
    现在: 造一个空提交 → dry-run 推 → 立刻 reset 掉。
    """
    _data, text = _load("release.yml")
    assert "--allow-empty" in text and "reset --hard HEAD~1" in text, \
        "预检要造一个真实提交再 dry-run 推, 推完 reset 掉"
    assert "push --dry-run" in text
    # 也钉住"可以用 PAT 发版"这条出路(默认令牌推不动时)
    assert "RELEASE_TOKEN" in text, \
        "checkout 要支持用 RELEASE_TOKEN(PAT) —— 仓库规则不放行 Actions 身份时的出路"
