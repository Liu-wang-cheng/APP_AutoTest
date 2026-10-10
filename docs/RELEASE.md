# 发版手册

从改完代码到用户收到更新, 全程四步。前置: GitHub push 权限; `gh` CLI 已登录
(`gh auth status`), 或设置 `GITHUB_TOKEN` / `~/.github_token`。

## 1. 改版本号(三处必须一致)

| 位置 | 作用 | 不一致的后果 |
|---|---|---|
| `core/version.py` 的 `__version__` | 程序运行时读的(标题栏/检查更新的基准) | 界面版本号与实际不符 |
| `VERSION`(仓库根) | 打进包里, 更新 bat 用它验证替换成功 | 更新后 bat 报 VERSION 异常 |
| `CHANGELOG.md` 新增 `## [x.y] - 日期` 节 | 发布说明(Release 页 + version.json) | 发版脚本直接拒绝 |

顺带在 `CHANGELOG.md` 写清这次改了什么 —— 它就是用户在更新弹窗里看到的
"更新内容"。

## 2. 打包

```bat
build.bat
```

自动: 跑全部测试(不过就中止) → PyInstaller 打包 → 验证产物
(模型文件/关键 DLL/offscreen 启动)。产物是**单个** `dist\AutoTest.exe`。
**发布前手动双击它跑一遍真机用例** —— 自动验证只保证"能力都在", 不保证"行为正确"。

★ `build.bat` 必须是 CRLF 换行(仓里有守护测试)。裸 LF 会被 cmd 把每行切成碎片执行,
  表现为 `'python.exe' is not recognized` 之类, 第一步就过不去。

## 3. 发布

```bash
# 先预览(不碰网络):
python tools/release.py --version 1.1 --dry-run
# 确认无误后正式发布:
python tools/release.py --version 1.1
```

**发版前必须先过 CI**(默认强制): 脚本第 0 步会查这个提交的 CI 记录 ——
本地 HEAD 还没推就先推上去, 然后等它跑完(默认最多等 20 分钟);
**红的、还在跑、查不到, 一律拒绝发版**(发出去的版本收不回来)。
理由: CI 是唯一能证明"换台干净机器也跑得通"的环节。紧急情况用 `--no-ci-check` 跳过。
`--ci-wait N` 可改等待秒数。

脚本会依次: 校验三处版本一致 → 打 `dist\AutoTest_v1.1.zip`(**只装 exe +
_internal/**, 运行时生成的 config/backups 绝不进包) → 传 GitHub Release →
写仓库根 `version.json`(含 sha256) → commit → push master + tag v1.1。

- 工作区有未提交改动时脚本会拒绝(发版内容必须等于已提交内容)
- `--min-version 1.1` 可把"低于此版本强制更新"的线抬到当前版(默认 1.0 不强制)

## 4. 验证更新链路

```bash
python tools/verify_release.py --version 1.4      # 等 1~2 分钟让 CDN 生效
```

自动查四项(取清单 / sha256 与产物一致 / 资产 200 且大小一致 / 前 1KB 是 MZ),
全过就说明客户端那条路能拿到**内容正确**的包。仍建议在**旧版本**的 GUI 里实点一次:
点「立即更新」走完下载→校验→重启, 复查标题栏版本号, 且 `Test_cases/`、
`config/config.yaml` 原样未动(更新只换 exe)。

大文件下载想更快时用镜像前缀下(如 `https://gh-proxy.com/<原地址>`): 实测直连拉
208MB 几乎不可用, gh-proxy 约 1.2 MB/s。

## 用户侧如何收到更新

启动 3 秒后检查一次, 之后每 8 小时自动检测(执行用例期间不检测); 发现新版
弹窗(含更新说明), 点「立即更新」后自动完成替换并重启。用户数据
(`config/` `Test_cases/` `Test_preconditions/` `Test_img/` `reports/` `backups/`)
不在更新包里, 永远不会被更新覆盖; 且每次启动都会自动备份到 `backups/`。

## 出问题怎么办

- **更新后起不来**: 程序目录里会留一个 `xxx.exe.bak` —— 把它改回 `xxx.exe` 即可回到
  更新前的版本(更新只换 exe, 用户数据完好)。v1.4 起替换脚本自己也会做这件事: 启动
  新版本后 8 秒内发现它没起来, 就自动换回旧版本并重启。
- **自动更新点下去没反应/一闪而过**: 看更新窗口里的 `[ERROR]` 行; 目录里若只剩
  `xxx.exe.bak`, 改名回去即可恢复(这是 v1.3 及更早的已知缺陷, v1.4 已修)。
- **配置/用例丢了**: `backups/` 里有每天一份的 zip(保留最近 10 份), 解压即回。
- **检查更新一直失败**: 看运行日志里 `[更新]` 行; 多为网络问题(镜像自动测速,
  也可在 `config.yaml` 的 `update.mirrors` 里增删镜像)。
