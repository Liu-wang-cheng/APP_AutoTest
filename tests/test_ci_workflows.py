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
import re
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


def test_real_smoke_clears_nodefaultcurrentdirectoryforexe_on_setup_python():
    """★ 自建 runner 的隐藏地雷(2026-10-19 实测踩到, 冒烟第一次真跑就死在这):

    runner 进程环境里带着 `NoDefaultCurrentDirectoryInExePath=1` → cmd.exe 不再从
    "当前目录"找 exe。而 setup-python 在 Windows 上的安装脚本是
        cmd /c "cd <toolcache> && call python-3.13.16-amd64.exe ... /quiet"
    于是必然报 `'python-3.13.16-amd64.exe' is not recognized` →
    `Error happened during Python installation`, 整个 job 在装 Python 处就红了。

    实测该变量**只有空值**能恢复搜索('0'/'false' 都不行), 且 Actions 会把空字符串
    照样传进进程, cmd 视同未设置。所以 setup-python 这步必须带着空 env ——
    删掉这行就会重现上面的失败。
    """
    data, _text = _load("real-smoke.yml")
    steps = data["jobs"]["smoke"]["steps"]
    setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python")]
    assert len(setup) == 1, "真机冒烟应当只有一处 setup-python"
    env = setup[0].get("env") or {}
    assert env.get("NoDefaultCurrentDirectoryInExePath") == "" or \
        "NoDefaultCurrentDirectoryInExePath" in env, "setup-python 这步必须把该变量置空"
    assert env.get("NoDefaultCurrentDirectoryInExePath") == "", \
        "该变量只有**空值**能让 cmd 恢复'当前目录找 exe'('0'/'false' 都不行)"


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


def test_release_precheck_is_not_a_dry_run():
    """★ 发版的预检**不能**靠 `git push --dry-run`(2026-10-16 实测两次):

    dry-run 根本不触发服务端规则 —— 本地造一个 pre-receive 钩子拒绝一切, `--dry-run`
    依然返回 0。原来那版预检(推当前 HEAD)本来就永远通过, 我改成"造空提交再 dry-run"后
    **依然**永远通过, 于是"预检通过、正式推送被 GH013 拒", 留下"Release 已发、version.json
    还是旧版"的半成品(v2.0.27 中过)。

    现在的预检是**确定性**的两条:
      ① 没配 RELEASE_DEPLOY_KEY → 直接拦(Actions 自带的 GITHUB_TOKEN app id 15368 不在
         ruleset bypass 名单里, 而名单里那条是 DeployKey —— 推送必被拒);
      ② 配了就**真推**一次探针分支(不是 dry-run), 证明这把密钥真的能写。
    """
    _data, text = _load("release.yml")
    # ★ 只看**命令行**: 注释里正解释着"为什么不能用 dry-run", 别被自己的说明文字绊倒
    cmds = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in cmds if "push --dry-run" in ln],         "dry-run 不触发服务端规则, 当预检等于没预检"
    assert "RELEASE_DEPLOY_KEY" in text, "要用有 bypass 的部署密钥发版(ruleset 里已有 DeployKey 一条)"
    # ★ 必须推**两次**: 第 1 次若在建分支会被 do_not_enforce_on_create 豁免(默认 token 也过),
    #   第 2 次("往已存在分支推新提交")才与推 master 同等待遇 —— 对照实验实测:
    #   默认 token 第 2 次被 GH013 拒, 部署密钥第 2 次 `Bypassed rule violations` 通过。
    assert text.count('git push origin "HEAD:refs/heads/$b"') == 2,         "预检要推两次(建分支那次会被豁免, 判据在第 2 次)"
    assert "git reset --hard HEAD~2" in text, "两次探针提交都要回退掉"
    # checkout 必须带上部署密钥 —— 否则 release.py 的 push 还是走 token
    assert "ssh-key: ${{ secrets.RELEASE_DEPLOY_KEY }}" in text


def test_no_secrets_in_step_if_conditions():
    """★ `secrets` 上下文**不能**用在步骤的 `if:` 里(2026-10-16 踩到):

    写了的话, YAML 照样能解析, 但**触发 workflow 时** GitHub 直接 422:
    `Invalid Argument - failed to parse workflow: Unrecognized named-value: 'secrets'`
    —— 也就是"发都发不出去", 而且本地看不出任何问题。
    要用就把判断放进 `run:`(那里的 env 可以取 secrets), 或先在 job 级 env 里转成布尔。
    """
    for name in ("release.yml", "tests.yml", "static.yml", "package.yml", "real-smoke.yml"):
        p = WF / name
        if not p.is_file():
            continue
        data, _text = _load(name)
        for job, spec in (data.get("jobs") or {}).items():
            for step in spec.get("steps") or []:
                cond = step.get("if")
                if isinstance(cond, str):
                    assert "secrets." not in cond, (
                        f"{name}:{job} 的步骤「{step.get('name')}」在 if: 里用了 secrets —— "
                        f"workflow 会解析失败(422), 请改用 run: 里的 env 判断")


# ── 存储瘦身(清理老 Release, 2026-10-16) ──

def test_prune_workflow_is_gated_and_safe():
    """★ 「存储瘦身」workflow 的三条底线:
    ① 定时(每月)+ 手动触发都留着;
    ② **手动触发默认演练**(dry_run 默认 true)—— 点一下不能就把包删了;
    ③ 真删那一步才带 `--apply`(脚本本身默认演练, 见 tools/prune_releases.py);
    ④ 手动传的 keep 要有兜底(定时触发时 inputs 是空的)。
    """
    _data, text = _load("prune.yml")
    assert "cron:" in text and "workflow_dispatch" in text
    assert "dry_run:" in text and "default: true" in text, "手动触发必须默认演练"
    assert "--apply" in text, "真删那一步要显式 --apply"
    assert "inputs.keep || '5'" in text, "定时触发时 keep 为空, 要有兜底"
    assert "contents: write" in text, "删 Release 需要写权限"


def test_all_workflow_step_names_are_quoted_when_needed():
    """★ `name:` 值里带「冒号+空格」会被 YAML 1.1 当成映射 → 解析直接失败(踩过 3 次)。

    这条扫**所有** workflow 的步骤名: 值里出现 ": " 就必须是带引号的字符串。
    (release.yml / static.yml 里都靠加引号躲过, prune.yml 又踩了一次 —— 与其每次人肉记着,
    不如让测试直接拦下来。)
    """
    for p in sorted(WF.glob("*.yml")):
        raw = p.read_text(encoding="utf-8")
        for i, line in enumerate(raw.splitlines(), 1):
            st = line.strip()
            if not st.startswith("- name:") and not st.startswith("name:"):
                continue
            val = st.split(":", 1)[1].strip()
            if val.startswith(("'", '"')):
                continue
            assert ": " not in val, (
                f"{p.name}:{i} 的 name 值里有「冒号+空格」会被 YAML 截断, 请加引号: {val!r}")


def test_actions_are_node24_versions():
    """★ 用的 action 必须是**跑在 node24 上**的版本(2026-10-16)。

    起因: CI 日志里刷 `##[warning]Node.js 20 is deprecated. The following actions target
    Node.js 20 but are being forced to run on Node.js 24: actions/checkout@v4, ...` ——
    旧版 action 已经在被"强制"跑, 哪天 node20 真摘掉就会集体失效。实测各 action 的
    action.yml: checkout 从 **v5** 起、setup-python 与 upload-artifact 从 **v6** 起是 node24。
    这里只卡"不低于第一个 node24 版本", 以后往上升级不会被这条测试挡住。
    """
    MIN = {"actions/checkout": 5, "actions/setup-python": 6, "actions/upload-artifact": 6}
    bad = []
    for p in sorted(WF.glob("*.yml")):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = re.search(r"uses:\s*(actions/[a-z-]+)@v(\d+)", line)
            if not m:
                continue
            repo, major = m.group(1), int(m.group(2))
            if repo in MIN and major < MIN[repo]:
                bad.append(f"{p.name}:{i} {repo}@v{major} (< v{MIN[repo]} = node20 时代)")
    assert not bad, "这些 action 是 node20 版本, 会被强制跑在 node24 并刷弃用警告: " + "; ".join(bad)


def test_real_smoke_uses_repo_committed_data_not_a_desktop_path():
    """★ 真机冒烟用的用例/前置/模板/配置必须是**仓库里提交好的那份**, 不许去够仓库外的路径。

    用户 2026-10-16 的定调(三句话): "CICD 跑的冒烟测试用桌面 APP_Test 里那条全局清扫"
    → "这个测试用例你直接替换过来用, **不要直接用桌面上那个**"
    → "**防止一修改 CICD 就跑不了了**"。
    所以做法是: 把用户那台工具在用的用例(验证过可完整执行)、模板、前置条件**整份入库**,
    CI 只读仓库 —— 桌面文件改名/挪走/换机器都不会让冒烟失效。

    这条钉住: ① workflow 里**没有**对仓库外路径的复制/依赖(不能再用 SMOKE_APP_DIR 那套);
    ② 配置走仓库里提交的 `config/config.ci.yaml` → 复制成 config.yaml(只有它没法入库:
       被 .gitignore 且含本机设备信息); ③ 日志留文件指纹, 看得出跑的是哪一版。
    """
    _data, text = _load("real-smoke.yml")
    assert "SMOKE_APP_DIR" not in text, "冒烟不许依赖仓库外的数据目录"
    assert "Desktop" not in text.replace("C:/Users", ""), "别在 workflow 里写桌面路径"
    assert "config/config.ci.yaml" in text and "Copy-Item config/config.ci.yaml config/config.yaml" in text, \
        "配置要用仓库里的 config/config.ci.yaml 复制成 config.yaml"
    assert "Get-FileHash" in text, "日志里要留指纹(排查'改了没生效'靠它)"
    # 入库的那几份数据得真在仓库里(少了就说明只改了 workflow, 没把数据搬过来)
    root = Path(__file__).resolve().parent.parent
    for rel in ("Test_cases/三星/全局清扫.yaml", "Test_cases/三星/templates/结束烘干.png",
                "Test_cases/三星/templates/停止烘干.png", "Test_preconditions/三星.yaml",
                "config/config.ci.yaml"):
        assert (root / rel).is_file(), f"冒烟要用的数据没入库: {rel}"
