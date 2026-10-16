# CI/CD 方案（含真机回归）

> 用户 2026-10-16 要求："方案 C 还需要做什么" → "你给个完整方案出来"。
> 本文件是**方案 C 的完整落地方案**：谁做什么、怎么做、怎么验、怎么回退。
> 与 `docs/RELEASE.md`（本机发版步骤）配套看。

## 0. 现状（已完成的部分）

| 组件 | 状态 | 位置 |
|---|---|---|
| 单元/集成测试 CI | ✅ 每次 push/PR 在 windows-latest 上跑 854 条 | `.github/workflows/tests.yml` |
| 分支门禁 | ✅ ruleset「合入规则」：要求 `pytest` 通过 + 禁强推/删分支；bypass = 你本人 / RepositoryRole(admin) / 一个 Integration；允许建新分支 | 仓库 Settings → Rules |
| 打包自检（方案 A） | ✅ 手动/推 `v*` tag → `build.bat` → zip 传 artifact（7 天） | `.github/workflows/package.yml` |
| 全自动发版（方案 B） | ✅ 手动填版本号/推 `v*.*.*` tag → 全套发版；勾 `dry_run` 可演练（不发布） | `.github/workflows/release.yml` |
| 真机用例入口 | ✅ `pytest tests/test_yaml_runner.py --mode real --device <id> --case <名>`（`--mode` 默认 mock，裸跑不会碰设备） | `conftest.py` |

**唯一缺口**：能连模拟器/真机的那台机器还没注册成 self-hosted runner。

---

## 1. 目标全景

```
push/PR ──► [tests.yml]  854 条测试            ─┐
                                                ├─► 绿 → 可合并 / 可发版
手动 ────► [static.yml]  pyflakes + 用例校验   ─┘        （C1，新增）

推 tag ──► [package.yml] 打包自检 → artifact           （方案 A，已有）
推 tag ──► [release.yml] 打包 → 传 Release → 清单 → tag （方案 B，已有）

手动/定时 ─► [real-smoke.yml] 自建 runner 上跑真机冒烟  （C2/C3，新增）
                              → junit + 失败截图 artifact
```

---

## 2. C1 质量门禁（我来写，零风险，不碰设备）

**新增 `.github/workflows/static.yml`**（push/PR 触发，几十秒）：

```yaml
name: 静态检查
on:
  push: { branches: [master] }
  pull_request: { branches: [master] }
jobs:
  lint:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.13', cache: pip }
      - run: pip install pyflakes pyyaml
      - name: pyflakes(除有意 noqa 外应当干净)
        run: python -m pyflakes core gui tools vlm
      - name: 用例 YAML 静态校验(键名/取值/阈值范围)
        run: python tools/validate_cases.py
```

要点：
- **只报不卡**的地方要明确：`pyflakes` 全绿才卡（本地已扫过，除 `# noqa` 是有意的）；`validate_cases.py` 返回非 0 就卡 —— 它拦的是"用例里写了不存在的键/越界阈值"这类**静默失效**。
- **覆盖率先只观测**：装 `pytest-cov`，在 tests.yml 里加 `--cov=core --cov-report=term-missing`，但**先不设门槛**（门槛要在你看到几个月的真实数字之后再定，否则就是逼自己改测试数量）。
- 这部分**与设备无关**，可以现在就上。

---

## 3. C2 真机冒烟（self-hosted runner）

### 3.1 你做的事：注册 runner（一次性，约 10 分钟）

在**那台跑着模拟器的机器**上（当前就是 `127.0.0.1:7555` / `5557` 那台）：

1. 仓库 → Settings → Actions → **Runners** → **New self-hosted runner** → 选 Windows x64
2. 页面会给出下载链接与一条带**一次性 token**（1 小时有效）的命令，照抄：

```bat
mkdir C:\actions-runner && cd C:\actions-runner
:: 下载并解压页面上给的 actions-runner-win-x64-*.zip
config.cmd --url https://github.com/Liu-wang-cheng/APP_AutoTest --token <页面上的TOKEN> ^
           --name smoke-pc --labels realtest,emulator
:: 前台跑(关窗口就停) —— 先这样验证通不通:
run.cmd
:: 验证通过后装成服务(机器重启也活着; 需要管理员权限):
svc.cmd install
svc.cmd start
```

3. 验证：Settings → Actions → Runners 里应看到 `smoke-pc`，状态 **Idle**（绿色）。

**为什么用 `--labels realtest,emulator`**：workflow 里写 `runs-on: [self-hosted, windows, realtest]`，这样**只有这台**会接真机任务，不会误落到云端 runner（云端连不上你的模拟器）。

### 3.2 这台机器上要预置的东西

| 需要什么 | 为什么 | 怎么做 |
|---|---|---|
| **本机 `config/config.yaml`** | 它被 gitignore，checkout **不会**带来；real 模式要知道连哪台设备 | 手工建一份（或从你本机拷），确保 `target_device` / `device.default` 指向模拟器 |
| **模拟器/真机在线** | 没设备 = 全红 | runner 机器上常开；workflow 会先探活并给明确提示 |
| **ADB 可用** | `uiautomator2.connect` 走 adb | `adb devices` 能看到设备 |
| **盘空间** | 每次真机会写截图/debug 产物 | 定期清 `C:\actions-runner\_work`（下面 3.5） |

> 注意：workflow 里我会显式传 `--device`（默认 `127.0.0.1:7555`，可用仓库变量覆盖），**不完全依赖** config.yaml —— 少一层"为什么连不上"的排查。

### 3.3 我做的事：`real-smoke.yml`（草稿，待你确认后落地）

```yaml
name: 真机冒烟
# ★ 触发**只给手动和定时** —— 见下面"红线"第 1 条(public 仓库 + 自建 runner 的 fork PR 风险)
on:
  workflow_dispatch:
    inputs:
      case:
        description: 跑哪个用例(模糊匹配, 留空=冒烟集)
        required: false
        default: ''
  schedule:
    - cron: '0 18 * * *'      # UTC 18:00 = 北京 02:00 —— 想开定时再启用(见红线第 3 条)

jobs:
  smoke:
    # 只落在这台自建 runner 上(标签在注册时给)
    runs-on: [self-hosted, windows, realtest]
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 1 }        # 用户数据(config/Test_cases)在 runner 本机, 不被覆盖

      - uses: actions/setup-python@v5
        with: { python-version: '3.13', cache: pip }
      - name: 安装依赖
        run: |
          python -m pip install --upgrade pip
          pip install -r requirements.txt

      - name: 探活设备(探不到就明确报"设备没起来", 而不是让用例背锅)
        shell: pwsh
        run: |
          $dev = "${{ vars.SMOKE_DEVICE }}"; if (-not $dev) { $dev = "127.0.0.1:7555" }
          adb connect $dev | Out-Host
          $out = adb devices | Out-String
          if ($out -notmatch [regex]::Escape($dev) + "\s+device") {
            throw "模拟器/真机没在线($dev) —— 请先启动模拟器, 再重跑本 workflow"
          }
          "DEVICE=$dev" >> $env:GITHUB_ENV

      - name: 跑冒烟(真机)
        shell: pwsh
        run: |
          $case = "${{ github.event.inputs.case }}"
          if (-not $case) { $case = "全局清扫" }      # ← 冒烟集(见 3.4)
          python -m pytest tests/test_yaml_runner.py -q -rs `
            --mode real --device "$env:DEVICE" --case "$case" `
            --junit-xml=reports/junit-real.xml

      - name: 失败时上传现场(截图 + junit)
        if: failure()
        uses: actions/upload-artifact@v4
        with:
          name: real-failure-${{ github.run_id }}
          path: |
            reports/junit-real.xml
            Test_cases/*/screenshots/**
            Test_img/debug/**
          retention-days: 7
          if-no-files-found: ignore
```

### 3.4 冒烟集怎么定（**需要你拍板**）

| 候选 | 成本 | 特点 |
|---|---|---|
| `快速建图` | 4 步 | 最短；但会真的建图（改变设备状态） |
| `全局清扫`（三星，11 步） | 中 | 覆盖点击/模板点击/断言/暂停/继续 —— 对"整条链路通不通"最有代表性（**我建议先用它**） |
| `勿扰模式` 等 | 4~14 步 | 按需再加 |

先跑一个；稳定一两周后再加第二个。**不要一上来全量**（一轮十几个用例要十几分钟，且任何一步失败都得人工复位机器人）。

### 3.5 清理与复位（真机 runner 特有）

- 真机跑完会在 runner 的 workspace 里留 `Test_cases/*/screenshots/`（用例截图，按"保留最近 3 次运行"自清）和 `Test_img/debug/`（调试产物，启动时自清 200 个/目录）——**大多自管**；
- 但每次 checkout 不会删这些 → 在 runner 上加一条**每周一次的清理**（Windows 任务计划或一个 cleanup workflow）：删 `C:\actions-runner\_work\APP_AutoTest\APP_AutoTest\{Test_cases,Test_img}\**\screenshots`、`build\`、`dist\`；
- 机器人侧复位：冒烟用例自己会回充/停止；万一中途失败留在清扫中，下一步的 `restart` 前置会重启 APP，但**机器人本体**要人工确认 —— 这也是"先只跑只读冒烟"的理由。

---

## 4. C3 定时与通知

- **定时**：`schedule` 用 **UTC**（北京 02:00 = `0 18 * * *`）。建议**先注释掉**，等你确认"夜里没人用设备"再打开。
- **通知**：GitHub 默认会给仓库 watcher（你）发失败邮件 ✓ 够用。想更即时可以：
  - 手机装 GitHub App（推送通知）；
  - 或加一步 webhook（企业微信/钉钉机器人）—— 需要你给 URL，我再写。
- **别做的事**：不要 nightly 发版（版本发布应是人工决策；`release.yml` 保持手动/tag 触发）。

---

## 5. C4 运维手册（自建 runner 的长期成本）

| 事项 | 频率 | 说明 |
|---|---|---|
| runner 服务是否活着 | 每月看一眼 | Settings → Actions → Runners 里状态 Idle；红了就在机器上 `svc.cmd restart` |
| runner 自动更新 | 自动 | 空闲时会自更新；若报版本过旧，重跑 `config.cmd` 即可 |
| ADB/模拟器掉线 | 偶发 | 探活步骤会明确报出来；手动 `adb connect` 恢复 |
| 磁盘 | 每季度 | 见 3.5 的清理 |
| 依赖变化 | 跟着仓库 | workflow 每次 `pip install -r requirements.txt` ✓ 自动 |

**常见故障对照**：
- 任务一直 **Queued** → runner 没在线/标签不匹配（检查 `runs-on` 的标签与注册时的 `--labels`）；
- **"模拟器/真机没在线"** → 启动模拟器，或改 `vars.SMOKE_DEVICE`；
- 用例失败但设备正常 → 就是真 bug，看 artifact 里的截图与 junit（这正是这套东西的价值）。

### 5.1 发版"半成品"：Release 有了、`version.json` 没更新

**症状**：`发版` workflow 红在第 12 步（`发布…`），日志尾是
`remote: error: GH013: Repository rule violations found for refs/heads/master … Required status check "pytest" is expected`。
此时 **Release 与 zip 资产已经传上去了**，但 `version.json` 还是上一版 → 用户在"检查更新"里
**看不到新版**（OTA 读的就是仓库根这份清单）。v2.0.27 中过一次。

**原因**：仓库 ruleset「合入规则」要求 `pytest` 状态检查，而 `release.py` 推 `version.json`
用的是 Actions 自己的 `GITHUB_TOKEN`（`github-actions[bot]`）—— 它不在 ruleset 的 bypass
名单里，于是"新增提交"的推送被服务端拒绝（本机推得动，是因为 `RepositoryRole(admin)` 在名单里）。

**已采用的修法（2026-10-16 落地，实测通过）：写权限部署密钥**。
ruleset 的 bypass 名单里本来就有一条 `DeployKey`（`actor_id: null` = 任何部署密钥都放行），
所以走 SSH 部署密钥就绕过了这条规则：

1. `ssh-keygen -t ed25519` 生成密钥对（私钥备份在桌面 `AutoTest_部署密钥\`）；
2. 仓库 Settings → Deploy keys 添加**公钥**并勾 **Allow write access**；
3. 仓库 Settings → Secrets and variables → Actions 建 secret **`RELEASE_DEPLOY_KEY`**（值=私钥）；
4. `release.yml` 的 checkout 带上 `ssh-key: ${{ secrets.RELEASE_DEPLOY_KEY }}` —— 之后
   `release.py` 的 `git push` 全部走这把密钥。

**对照实验（实测数据，别再凭感觉）**：往**探针分支**推两次，第 2 次（往已存在分支推新提交）
与推 master 同等待遇（第 1 次在建分支，被 `do_not_enforce_on_create` 豁免）：

| 推送身份 | 第 1 次（建分支） | 第 2 次（推新提交） |
|---|---|---|
| 默认 `GITHUB_TOKEN` | 通过（豁免） | **被拒** `GH013 Required status check "pytest" is expected` |
| 部署密钥 | 通过 | 通过，服务端明说 `Bypassed rule violations …` |

**另外两种没采用的备选**：① ruleset 的 bypass 名单里加 **GitHub Actions**（注意：应用 id 是
**15368**，名单里那条 4518051 不是它）；② 建 PAT 存成 secret 供 checkout 用。

**万一已经卡在这一步（补完的办法）**：
```bash
# ① 下载**已上传**的资产并算 sha256(清单必须与资产一致, 不能拿本地重打包的包顶替)
# ② 用 release.py 内部那一步补齐(它做的事就是: 写 version.json → commit → tag -f → push)
python - <<'PY'
import sys; sys.path.insert(0, ".")
import tools.release as R
v, zip_path = "2.0.27", r"<刚下载的 zip>"
R.update_version_json_and_tag(
    "Liu-wang-cheng/APP_AutoTest", v, R.compute_sha256(zip_path),
    f"https://github.com/Liu-wang-cheng/APP_AutoTest/releases/download/v{v}/AutoTest_v{v}.zip",
    R.load_changelog_section("CHANGELOG.md", v)[1])
PY
# ③ 验一遍用户那条链路(把下载来的包当 --pkg, 这样 sha 才对得上)
python tools/verify_release.py --version 2.0.27 --pkg <刚下载的 zip>
```

**预检被"假阳性"坑过两次（2026-10-16 定稿）**：
- 第 1 版是 `git push --dry-run origin HEAD:refs/heads/master` —— 推**当前 HEAD** 是 no-op
  （Everything up-to-date），规则根本没机会拦；
- 第 2 版"造个空提交再 dry-run 推"**照样是假阳性** —— 本地造一个拒绝一切的 pre-receive 钩子
  实测：`git push --dry-run` **完全不触发服务端钩子/规则**，依然返回 0。**dry-run 不能当预检。**

现在的预检是两步：① 没配 `RELEASE_DEPLOY_KEY` → 直接拦（这种身份必被拒，说清怎么修）；
② 配了就**真推两次**探针分支（见上面的对照实验），第 2 次的成败才是判据。`tests/test_ci_workflows.py`
里有一条守护盯着："命令行里不许出现 `push --dry-run`" + "必须推两次"。

---

## 6. 红线与风险

1. **public 仓库 + 自建 runner = 绝不给 fork PR 跑**。别人 fork 后发 PR，workflow 会在**你的机器**上执行任意代码。所以 `real-smoke.yml` 的触发**只有** `workflow_dispatch` / `schedule`，**没有** `pull_request`。万一日后要接 PR，必须加：
   ```yaml
   if: github.event.pull_request.head.repo.full_name == github.repository
   ```
2. **别让两件事同时用设备**：nightly 定时和你的手动测试会抢设备（表现为用例随机失败）。要么只手动触发，要么把定时放在你确定不用的时段。
3. **别用 CI 造的 zip 当发布包**：CI 的 artifact 与本地产物 sha256 不同（PyInstaller 嵌入构建时间戳），替换线上资产会让用户的更新校验失败。发布永远走 `release.yml`（它会重算 sha256 写进 `version.json`）。
4. **真机任务会真实操控机器人**：不是 mock。冒烟集要挑"可自恢复"的用例；失败后先看设备状态，必要时人工复位。
5. **自建 runner 不计 GitHub 分钟数**（免费额度只有云端 runner 才涉及），但**成本转移到你自己机器的常开与维护**。

---

## 7. 落地顺序与工作量

| 步骤 | 谁 | 工作量 | 风险 |
|---|---|---|---|
| ① `static.yml`（静态检查） | 我 | 10 分钟 | 零（不碰设备） |
| ② 注册 runner + 预置 config.yaml | 你 | 10 分钟 | 需要管理员权限（装服务） |
| ③ `real-smoke.yml`（默认只手动触发） | 我 | 15 分钟 | 首次跑会真的动设备 |
| ④ 手动点一次真机冒烟，看产物 | 你/我 | 5 分钟 | —— |
| ⑤ 打开 `schedule`（可选） | 你拍板 | 1 分钟 | 与手动测试抢设备 |

**建议**：先做 ①，再做 ②③④；⑤ 等你确认时段再开。

---

## 8. 验收清单（做完长这样）

- [ ] push/PR 上出现三个检查：`pytest`、`静态检查`、（未来）`真机冒烟`
- [ ] Actions 页面能手动跑"打包自检"，7 天内可下载 zip artifact
- [ ] Actions 页面能手动跑"发版"（`dry_run` 演练不发布；不勾则真发布并更新 `version.json`）
- [ ] Settings → Actions → Runners 里有 `smoke-pc`（Idle），能手动跑"真机冒烟"并拿到 junit + 失败截图
- [ ] 失败时你收到邮件通知；`Test_img/debug/failures` 里的现场图能直接看

---

## 9. 回退

- 任何 workflow **删文件即停**（`.github/workflows/*.yml`），不影响程序与已发布版本；
- runner 可随时停：`svc.cmd stop` / `svc.cmd uninstall`，或在 Settings → Runners 里 Remove；
- ruleset 的 bypass 与 `do_not_enforce_on_create` 都可改回（Settings → Rules）。
