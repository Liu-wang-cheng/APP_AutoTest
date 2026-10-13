# -*- coding: utf-8 -*-
"""首次运行的初始化: 把随包的程序资源铺到用户数据目录。

★ 为什么需要它(打包后才会暴露的问题)
  打包时 `config/locators.yaml` 与 `config/config.example.yaml` 作为**程序资源**进了包里
  (在 `_internal/config/` 下), 而程序运行期读的是 `DATA_DIR/config/...`(exe 旁边的用户
  数据目录)。不铺一次就找不到 —— 定位器配置缺失会让 click/assert 的 `${段.键}` 引用
  全部失效, 这种失败还很安静(报"找不到元素", 不像"配置没读到")。

★ 为什么 config.yaml 只"生成"不"复制"
  它含设备序列号等, 是**用户数据**、不进包、也绝不能被更新覆盖。首次运行从
  config.example.yaml 生成一份, 用户照着填 —— 比让程序读不到配置时报一堆错友好。

★ 幂等
  已存在的一律不动(用户改过的配置不能被覆盖)。开发环境下源与目标同目录, 直接跳过。

★ 清理类操作一律先过「空闲闸门」(core/idle.py, 用户要求 2026-10-12)
  `cleanup_update_leftovers` / `cleanup_old_backup` / `prune_debug_artifacts` /
  `prune_run_artifacts` / `trim_failures` / `trim_traces` / `trim_case_screenshots`
  都挂着 `@idle.gated(...)` 装饰器 —— 正在执行用例时**推迟**到本轮结束再做, 免得目录
  遍历与跑用例的内存峰值撞在一起。**新增清理函数请照挂**, 否则它就成了漏网的那个
  (有一条结构性守护测试扫描 @idle.gated)。注意: 只做"推迟", 不做"跳过" ——
  封顶最终一定会执行(见 end_run 的补做)。
"""
import os
import re
import shutil
import sys

from core import idle
from core.logger import get_logger

log = get_logger()

#: (包内相对路径, 目标相对 DATA_DIR 的路径) —— 首次运行需要铺出去的程序资源
_SEED_FILES = (
    ("config/locators.yaml", "config/locators.yaml"),
    ("config/config.example.yaml", "config/config.example.yaml"),
)
#: 默认用户数据的**目录**(逐文件"缺才补"): 用例与模板随包带一套默认的,
#: 本地没有的文件解压出来当默认值, 本地已有的**绝不覆盖** —— 更新/重装都
#: 不会动用户改过的用例与模板(用户要求 2026-09-24)。
#: screenshots 之类的运行产物在 spec 里就不进包, 这里只管"缺才补"。
# ★ 2026-10-11: 去掉 "Test_img/templates" —— 模板一律按 APP 组存放
#   (Test_cases/<组>/templates/), 那个全局目录已删(用户确认没有老版本了);
#   留着它会让程序每次启动又把空目录建回来。
_SEED_DIRS = ("Test_cases",)
#: 用户配置的模板(只生成, 之后由用户在 GUI 里改)
_CONFIG_TEMPLATE = "config/config.example.yaml"
_CONFIG_TARGET = "config/config.yaml"

#: OTA 自替换可能留下的残留(更新 bat 删不掉时的兜底, 见 updater.generate_update_bat)
#: ★ 三样**故意不在**这里:
#:   ① `*.exe.bak` —— 它是"刚更新的那一版起不来就回滚"的唯一退路, 而回滚脚本要等
#:      新进程起来几秒后才判定。启动(约 1 秒)就把它删了 = 把退路拆掉, 于是 F2 那
#:      套回滚形同虚设。改由 cleanup_old_backup() 在跑稳之后删。
#:   ② `_update.bat` —— **它此刻正在运行**(就是它把本进程拉起来的)。删一个正在执行
#:      的 bat 会让 cmd 后续读不到下一行, 更新流程可能断在半路。它自己会自删,
#:      真残留了也由 cleanup_old_backup() 收尾。
#:   ③ `_internal_old` —— 与 .bak 同理, 是目录模式下"新版本起不来就换回来"的退路。
#:   ④ `_update_extracted` —— 更新脚本刚用它把新载荷 move 到位; 虽然它只做 rmdir,
#:      但没必要跟正在跑的脚本抢。
#:   ⑤ 目录里的其它文件一律不碰。
_UPDATE_LEFTOVERS = ("_update_download.zip",        # 下好的分发包(已解压或已作废)
                     "_update_download.zip.part",   # 下载中断留下的半截(200MB)
                     "_update_target.txt")          # 目标程序名标记
#: 旧程序备份: 目录模式把当前 exe 改名成 `<名>.exe.bak`。名字不固定(用户可能重命名过
#: exe), 用 glob 找。只在 cleanup_old_backup() 里用。
_LEFTOVER_GLOB = "*.exe.bak"
#: 延迟清理时一并收尾的更新残渣(跑稳 2 分钟后 / 正常关窗时)
_LEFTOVER_LATE = ("_internal_old",                  # 旧载荷(回滚用, 跑稳后才删)
                  "_update.bat", "_update_download.zip", "_update_download.zip.part",
                  "_update_extracted",              # 解压出来的新版本
                  "_update_target.txt")


@idle.gated("清理更新残渣", empty=list)
def cleanup_old_backup(app_dir=None):
    """新版本**跑稳之后**再删旧程序备份(.bak)与更新残渣; 返回清掉的条目。

    ★ 为什么必须晚于启动: `*.exe.bak` 是新版本起不来时唯一的退路 —— 更新脚本靠它
      在 8 秒内判定新进程是否存活并回滚; 用户也能在几十秒内手工改回旧版本。
      `gui/main_window` 在窗口稳定运行 BACKUP_CLEANUP_DELAY_MS 之后(以及正常关窗
      时)调用这里, 那一刻才谈得上"更新确实成功了"。
    ★ 顺带收 `_update.bat`: 启动时它还在运行(不能删), 此刻必然已经跑完。
    """
    import glob as _glob
    root = _app_root(app_dir)
    if not root:
        return []
    cleaned = []
    for name in _LEFTOVER_LATE:
        if _remove(os.path.join(root, name)):
            cleaned.append(name)
    for p in _glob.glob(os.path.join(root, _LEFTOVER_GLOB)):
        if _remove(p):
            cleaned.append(os.path.basename(p))
    if cleaned:
        log.info(f"[初始化] 已清理更新残渣: {', '.join(cleaned)}")
    return cleaned


def _app_root(app_dir=None):
    """程序目录: 显式给了就用它; 否则仅打包环境可从 sys.executable 推出。

    ★ 开发环境**不返回**项目根 —— 免得脚本/测试"顺手"删掉仓库里的文件
      (与 cleanup_update_leftovers 同样的保守策略)。
    """
    if app_dir:
        root = os.path.abspath(app_dir)
    elif getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        return ""
    return root if root and os.path.isdir(root) else ""


def _remove(path):
    import shutil
    if not os.path.exists(path):
        return False
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
        return not os.path.exists(path)
    except OSError:
        return False


@idle.gated("清理更新残留", empty=list)
def cleanup_update_leftovers(app_dir=None):
    """清掉上次更新中断留下的残留; 返回清理掉的条目。

    ★ 正常情况下更新 bat 自己会清; 但杀软可能短暂锁定导致残留 —— 启动时再兜一道。
      `*.exe.bak` 与 `_update.bat` **不在这里**(见 _UPDATE_LEFTOVERS 的说明),
      它们由 cleanup_old_backup() 在跑稳之后收。任何失败都吞掉。
    """
    # ★ app_dir 必须单独判断 —— 写成 `app_dir or X if frozen else ""` 会被解析成
    #   `(app_dir or X) if frozen else ""`: 未打包时 app_dir 被整个丢掉, 清理落空
    root = _app_root(app_dir)
    if not root:
        return []
    cleaned = [name for name in _UPDATE_LEFTOVERS
               if _remove(os.path.join(root, name))]
    if cleaned:
        log.info(f"[初始化] 已清理上次更新的残留: {', '.join(cleaned)}")
    return cleaned


def bundled_root():
    """随包资源所在目录: 打包后是 `<_internal>`, 开发环境是项目根。"""
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", "") or os.path.dirname(
            os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_data_dirs(app_dir=None, data_dir=None):
    """铺出首次运行所需的程序资源; 返回铺出去的文件列表(便于日志/测试)。

    任何失败都只记日志、不抛 —— 初始化不该拦住程序启动。
    """
    from core.driver import DATA_DIR
    src_root = os.path.abspath(app_dir or bundled_root())
    dst_root = os.path.abspath(data_dir or DATA_DIR)
    seeded = []

    # ★ 补 update 段必须在"开发环境提前返回"**之前**做: 它针对的正是**老配置文件**,
    #   而开发环境的 config.yaml 往往就是那个老文件 —— 放到后面会被跳过
    #   (实测踩过: 日志说补了, 实际文件里还是没有, 检查更新仍报"未启用")。
    try:
        _ensure_update_section(os.path.join(dst_root, _CONFIG_TARGET))
    except Exception as e:
        log.warning(f"[初始化] 补 update 段失败(忽略): {e}")

    # 开发环境: 源就是目标所在目录, 没什么可铺的
    if os.path.normcase(src_root) == os.path.normcase(dst_root):
        return seeded

    for rel_src, rel_dst in _SEED_FILES:
        src = os.path.join(src_root, rel_src)
        dst = os.path.join(dst_root, rel_dst)
        if not os.path.isfile(src) or os.path.exists(dst):
            continue                     # 源没有 / 目标已存在(用户可能改过) -> 不动
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            seeded.append(rel_dst)
        except OSError as e:
            log.warning(f"[初始化] 复制 {rel_dst} 失败(忽略): {e}")

    # 默认用例/模板: 逐文件"缺才补" —— 用户删了某个默认用例, 下次启动会回来;
    # 用户改过的文件永不被覆盖
    for rel_dir in _SEED_DIRS:
        src_dir = os.path.join(src_root, rel_dir)
        if not os.path.isdir(src_dir):
            continue
        for base, _dirs, files in os.walk(src_dir):
            for fn in files:
                src = os.path.join(base, fn)
                # ★ 相对 src_dir(而不是 src_root): 否则目标会变成
                #   Test_cases/Test_cases/...(双重前缀), 铺错位置
                rel = os.path.relpath(src, src_dir)
                dst = os.path.join(dst_root, rel_dir, rel)
                if os.path.exists(dst):
                    continue
                try:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                    seeded.append(os.path.join(rel_dir, rel))
                except OSError as e:
                    log.warning(f"[初始化] 铺默认文件 {rel} 失败(忽略): {e}")

    # 没有用户配置时, 从模板生成一份, 免得程序一上来就读不到配置
    cfg_dst = os.path.join(dst_root, _CONFIG_TARGET)
    if not os.path.exists(cfg_dst):
        tpl = os.path.join(dst_root, _CONFIG_TEMPLATE)
        if not os.path.isfile(tpl):
            tpl = os.path.join(src_root, _CONFIG_TEMPLATE)
        if os.path.isfile(tpl):
            try:
                os.makedirs(os.path.dirname(cfg_dst), exist_ok=True)
                shutil.copy2(tpl, cfg_dst)
                seeded.append(_CONFIG_TARGET)
                log.info(f"[初始化] 已从模板生成 {_CONFIG_TARGET}(请在界面里填好设备与 APP)")
            except OSError as e:
                log.warning(f"[初始化] 生成 {_CONFIG_TARGET} 失败: {e}")

    if seeded:
        log.info(f"[初始化] 首次运行, 已铺出: {', '.join(seeded)}")
    return seeded


def _ensure_update_section(cfg_path):
    """config.yaml 里没有 update 段时, 追加一段默认的。

    ★ 为什么必须补: 自动更新的全部配置(仓库/分支/检查周期)都在这段里, 而它是后来
      才加进模板的 —— 老用户的 config.yaml 早就生成好了、没有这一段, 于是
      "检查更新"会静默返回"未启用自动更新", 用户以为功能坏了(实测遇到)。
      ★ 文本级追加而不是 yaml.safe_dump 重写: 保住用户自己的注释与排版。
      已有该段就一个字都不动(哪怕用户改过内容)。
    ★ 必须走 core.driver._atomic_write(先写临时文件再 os.replace): 直接 `open(w)`
      会**立刻截断**用户的 config.yaml, 写到一半进程被杀(或磁盘满)就只剩半截 ——
      而它含设备序列号等, 被 .gitignore 忽略、丢了只能手工重建。
    """
    if not os.path.isfile(cfg_path):
        return False
    try:
        with open(cfg_path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError:
        return False
    if re.search(r"^update:", text, re.M):
        return False                      # 已有 -> 不动
    eol = "\r\n" if "\r\n" in text else "\n"
    block = (
        f"{eol}# ── OTA 自动更新 ──{eol}"
        f"# repository 留空 = 不检查更新。填 GitHub 的 owner/repo 即启用:{eol}"
        f"# 启动后自动检查一次, 之后每 8 小时一次(执行用例期间不检测)。{eol}"
        f"update:{eol}"
        f"  enabled: true{eol}"
        f'  repository: "Liu-wang-cheng/APP_AutoTest"{eol}'
        f'  branch: "master"{eol}'
        f'  version_file: "version.json"{eol}'
        f"  check_interval_hours: 8{eol}"
    )
    from core.driver import _atomic_write
    _atomic_write(cfg_path, text.rstrip("\r\n") + eol + block)
    log.info("[初始化] config.yaml 缺少 update 段, 已补上默认配置(自动更新启用)")
    return True


#: 调试图片的封顶策略(用户要求 2026-10-11: "测试的调试图片也要限制一下")
#: 每次真机运行都会往 Test_img/debug/ 存模板点击标记、失败截图与失败前的时序证据 ——
#: 长期跑下来会慢慢堆满盘(打包版没有测试期那套清理)。
DEBUG_KEEP_PER_DIR = 200          # 每个子目录最多留最新的 200 个文件
DEBUG_MAX_TOTAL_MB = 300          # 整个 debug 目录最多占 300MB(超了按最旧优先删)
TRACE_KEEP = 50                   # 失败时序证据目录最多保留多少个(每个目录里还有 8 张 JPEG)


def _debug_entries(debug_dir):
    """[(mtime, size, path)] —— debug 目录下所有文件(含子目录)"""
    out = []
    for root, _dirs, files in os.walk(debug_dir):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, p))
    return out


@idle.gated("调试产物封顶", empty=(0, 0))
def prune_debug_artifacts(app_dir=None, keep_per_dir=DEBUG_KEEP_PER_DIR,
                          max_total_mb=DEBUG_MAX_TOTAL_MB):
    """给调试产物封顶: 每个子目录只留最新的 N 个, 整体超过上限时按最旧优先删。

    返回 (删掉的文件数, 释放的字节数)。任何异常都吞掉 —— 清理绝不能影响启动。

    ★ 为什么要它: 模板点击标记(每次模板点击一张)、失败截图、失败前时序证据都落在
      Test_img/debug/ 下; 跑几个月能堆到几个 GB, 而它们只是**排查用**的中间产物。
      测试期有 conftest 每轮清, 打包版没有 —— 这里补上。
    """
    root = _app_root(app_dir)
    if not root:
        return 0, 0
    debug_dir = os.path.join(root, "Test_img", "debug")
    if not os.path.isdir(debug_dir):
        return 0, 0
    try:
        entries = _debug_entries(debug_dir)
        doomed = []
        # ① 每个目录只留最新 keep_per_dir 个
        by_dir = {}
        for mt, size, p in entries:
            by_dir.setdefault(os.path.dirname(p), []).append((mt, size, p))
        for _d, items in by_dir.items():
            items.sort(reverse=True)                  # 新 → 旧
            doomed.extend(items[keep_per_dir:])
        # ② 整体体积超上限 → 按最旧优先继续删(不算已判死的)
        dead = {p for _mt, _s, p in doomed}
        total = sum(s for _mt, s, _p in entries if _p not in dead)
        if total > max_total_mb * 1024 * 1024:
            for mt, size, p in sorted(entries, key=lambda x: x[0]):   # 旧 → 新
                if total <= max_total_mb * 1024 * 1024:
                    break
                if p in dead:
                    continue
                dead.add(p)
                total -= size
        freed = n = 0
        for _mt, size, p in entries:
            if p in dead and _remove(p):
                n += 1
                freed += size
        # ★ 失败时序证据目录(<用例>_trace/)也按**个数**保留 —— 只按"每个目录里的文件数"限,
        #   目录本身会一直攒(用户 2026-10-12: "多了也无用")。
        trace_root = os.path.join(debug_dir, "failures")
        if os.path.isdir(trace_root):
            traces = [os.path.join(trace_root, n) for n in os.listdir(trace_root)
                      if os.path.isdir(os.path.join(trace_root, n))]
            traces.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            for old_t in traces[TRACE_KEEP:]:
                n2, b2 = _tree_size(old_t)
                if _rm_tree(old_t):
                    n += 1 + n2
                    freed += b2
        # 顺手收掉空目录
        for root2, dirs, files in os.walk(debug_dir, topdown=False):
            if root2 != debug_dir and not os.listdir(root2):
                try:
                    os.rmdir(root2)
                except OSError:
                    pass
        if n:
            log.info(f"[初始化] 调试产物封顶: 删除 {n} 个旧文件, 释放 {freed / 1048576:.0f}MB")
        return n, freed
    except Exception as e:          # 清理失败绝不能影响启动
        log.debug(f"[初始化] 调试产物清理跳过: {e}")
        return 0, 0


#: 「跑出来的产物」封顶策略(用户要求 2026-10-12: "如何确保不会越来越大")
SCREENSHOT_KEEP_RUNS = 10         # 每个用例最多保留最近多少"次执行"的截图目录
SCREENSHOT_MAX_TOTAL_MB = 1024    # 所有用例截图目录合计上限(超了按最旧优先删)
REPORT_KEEP = 20                  # 最多保留多少份报告
REPORT_FAIL_SHOTS_KEEP = 50       # 失败截图最多保留多少张(用户 2026-10-12: 50)


def _rm_tree(path):
    try:
        shutil.rmtree(path, ignore_errors=True)
        return not os.path.exists(path)
    except Exception:
        return False


@idle.gated("运行产物封顶", empty=(0, 0, 0))
def prune_run_artifacts(app_dir=None, keep_runs=None, max_total_mb=None,
                        report_keep=None, fail_keep=None):
    """给"跑出来的产物"封顶: 用例截图按执行次数保留 + 报告/失败截图按份数保留。

    返回 (删掉的目录数, 删掉的文件数, 释放字节数)。任何异常都吞掉(绝不影响启动)。
    ★ 这一趟要走遍 Test_cases 下所有组的 screenshots 树并逐个 stat, 是最重的清理 ——
      正在执行用例时整个推迟(装饰器)。

    ★ 为什么需要(2026-10-12 用户问"如何确保不会越来越大"): 每次点击都会留一张带标记的
      截图(1080x1920 PNG ≈ 340KB), 一个用例一轮十几张 —— 不封顶的话一个月能堆几十 GB。
      策略(只删**可以重跑出来的**东西, 绝不碰用例/模板/配置/备份):
        · Test_cases/<组>/screenshots/<每次执行一个目录> → 每个用例保留最近 N 次;
          所有用例合计超过上限时, 按目录时间最旧优先整目录删。
        · reports/ 下: 报告文件保留最近 N 份; reports/failures/ 的失败截图保留最近 M 张。
    """
    # ★ 阈值在**调用时**读取(不是写进默认参数): 默认参数在导入时就绑定了, 改不动,
    #   测试/调优都没法覆盖。
    keep_runs = SCREENSHOT_KEEP_RUNS if keep_runs is None else keep_runs
    max_total_mb = SCREENSHOT_MAX_TOTAL_MB if max_total_mb is None else max_total_mb
    report_keep = REPORT_KEEP if report_keep is None else report_keep
    fail_keep = REPORT_FAIL_SHOTS_KEEP if fail_keep is None else fail_keep
    root = _app_root(app_dir)
    if not root:
        return 0, 0, 0
    # ★ 顺带把 Test_img/debug(点击标记/失败截图/时序证据)也封顶 —— 同一个入口, 启动跑一次
    prune_debug_artifacts(app_dir)
    dirs_removed = files_removed = freed = 0
    try:
        # ── ① 用例截图: 每次执行一个目录(可能叫 "用例名-第N次", 也可能是老版的 "用例名") ──
        shot_dirs = []
        cases_root = os.path.join(root, "Test_cases")
        for group in (os.listdir(cases_root) if os.path.isdir(cases_root) else []):
            gdir = os.path.join(cases_root, group, "screenshots")
            if not os.path.isdir(gdir):
                continue
            # 目录名形如 "<用例名>-第N次"(2.0.6 起); 老版只有 "<用例名>" → 两者都按用例分组
            by_case = {}
            for name in os.listdir(gdir):
                p = os.path.join(gdir, name)
                if os.path.isdir(p):
                    case = _shot_case_of(name)
                    by_case.setdefault(case, []).append(p)
            for plist in by_case.values():
                plist.sort(key=os.path.getmtime, reverse=True)     # 新 → 旧
                for old_run in plist[keep_runs:]:                  # 每个用例只留最近 N 次
                    n, b = _tree_size(old_run)
                    if _rm_tree(old_run):
                        dirs_removed += 1
                        files_removed += n
                        freed += b
                shot_dirs.extend(p for p in plist[:keep_runs] if os.path.isdir(p))
        # ② 总容量兜底: 超上限 → 按目录时间最旧优先整目录删
        total = sum(_tree_size(p)[1] for p in shot_dirs)
        if total > max_total_mb * 1024 * 1024:
            for p in sorted(shot_dirs, key=lambda x: os.path.getmtime(x)):   # 旧→新
                if total <= max_total_mb * 1024 * 1024:
                    break
                n, b = _tree_size(p)
                if _rm_tree(p):
                    dirs_removed += 1
                    files_removed += n
                    freed += b
                    total -= b
        # ── ③ 报告文件: 保留最近 N 份 ──
        rep_dir = os.path.join(root, "reports")
        if os.path.isdir(rep_dir):
            reports = [os.path.join(rep_dir, f) for f in os.listdir(rep_dir)
                       if f.lower().endswith((".xlsx", ".xls"))]
            reports.sort(key=lambda x: os.path.getmtime(x), reverse=True)
            for old in reports[report_keep:]:
                if _remove(old):
                    files_removed += 1
                    freed += _file_size(old)
            # ── ④ 失败截图: 保留最近 M 张 ──
            fdir = os.path.join(rep_dir, "failures")
            if os.path.isdir(fdir):
                shots = [os.path.join(fdir, f) for f in os.listdir(fdir)
                         if os.path.isfile(os.path.join(fdir, f))]
                shots.sort(key=lambda x: os.path.getmtime(x), reverse=True)
                for old in shots[fail_keep:]:
                    if _remove(old):
                        files_removed += 1
                        freed += _file_size(old)
        if files_removed or dirs_removed:
            log.info(f"[清理] 运行产物封顶: 删 {dirs_removed} 个旧截图目录 / {files_removed} 个文件, "
                     f"释放 {freed / 1048576:.0f}MB")
        return dirs_removed, files_removed, freed
    except Exception as e:          # 清理失败绝不能影响启动
        log.debug(f"[清理] 运行产物封顶跳过: {e}")
        return dirs_removed, files_removed, freed


def _file_size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _tree_size(path):
    """(文件数, 总字节)"""
    n = size = 0
    for r, _d, files in os.walk(path):
        for f in files:
            n += 1
            size += _file_size(os.path.join(r, f))
    return n, size


def keep_newest(paths, keep):
    """只保留最新 keep 个(按 mtime), 其余删掉 —— 返回 (删了几个, 释放多少字节)。

    ★ "到上限就循环覆盖"是**写的时候**做的(见下面三个 trim_*): 启动时那一次只是兜底 ——
      否则一次长时间运行里连着失败几十次, 图片会先涨过上限、等到下次启动才被清掉。
    """
    removed = freed = 0
    for p in sorted(paths, key=lambda x: os.path.getmtime(x), reverse=True)[keep:]:
        size = _tree_size(p)[1] if os.path.isdir(p) else _file_size(p)
        if _rm_tree(p) if os.path.isdir(p) else _remove(p):
            removed += 1
            freed += size
    return removed, freed


@idle.gated("失败截图循环", empty=(0, 0))
def trim_failures(failures_dir, keep=None):
    """失败截图目录: 只留最新 N 张(写新图后立刻调用 → 循环覆盖)"""
    keep = REPORT_FAIL_SHOTS_KEEP if keep is None else keep
    if not failures_dir or not os.path.isdir(failures_dir):
        return 0, 0
    files = [os.path.join(failures_dir, f) for f in os.listdir(failures_dir)
             if os.path.isfile(os.path.join(failures_dir, f))]
    n, freed = keep_newest(files, keep)
    if n:
        log.info(f"[清理] 失败截图循环: 删最旧 {n} 张, 释放 {freed / 1048576:.1f}MB")
    return n, freed


@idle.gated("失败证据循环", empty=(0, 0))
def trim_traces(failures_dir, keep=None):
    """失败证据目录(Test_img/debug/failures/): **子目录(时序证据)与文件(逐步失败截图)**
    各留最新 N 个, 写一次就回收一次 → 到上限循环覆盖。

    ★ 两类都放这里(一个目录一种用途), 所以一起管 —— 只限目录会漏掉那些单张 PNG。
    """
    keep = TRACE_KEEP if keep is None else keep
    if not failures_dir or not os.path.isdir(failures_dir):
        return 0, 0
    entries = [os.path.join(failures_dir, x) for x in os.listdir(failures_dir)]
    dirs = [p for p in entries if os.path.isdir(p)]
    files = [p for p in entries if os.path.isfile(p)]
    n1, f1 = keep_newest(dirs, keep)
    n2, f2 = keep_newest(files, keep)
    n, freed = n1 + n2, f1 + f2
    if n:
        log.info(f"[清理] 失败证据循环: 删最旧 {n} 项(目录 {n1} / 文件 {n2}), "
                 f"释放 {freed / 1048576:.1f}MB")
    return n, freed


def _shot_case_of(name):
    """截图目录名 → 用例键。两种形态都认(审查 2026-10-12):
    「用例名-第N次」(现行) / 「用例名-N」(2.0.7 短暂生成过的) —— 后者若不认,
    已装 2.0.7 的机器上那些目录就永远没人收。"""
    stem, sep, tail = name.rpartition("-第")
    if sep and tail.endswith("次") and tail[:-1].isdigit():
        return stem
    stem, sep, tail = name.rpartition("-")
    if sep and tail.isdigit():
        return stem
    return name


@idle.gated(lambda shots_dir, case_name="", keep=None:
            f"截图循环({case_name or '全部'})", empty=(0, 0))
def trim_case_screenshots(shots_dir, case_name="", keep=None):
    """某个用例的截图: 只留最新 N 次执行的目录(每次执行结束后调用 → 循环覆盖)

    ★ defer 名里带用例键(审查 2026-10-12): 一轮跑 N 个用例就调 N 次, 写死"截图循环"
      会被去重成只剩第一个用例, 其余用例的封顶被静默丢弃。
    """
    keep = SCREENSHOT_KEEP_RUNS if keep is None else keep
    if not shots_dir or not os.path.isdir(shots_dir):
        return 0, 0
    dirs = []
    for name in os.listdir(shots_dir):
        p = os.path.join(shots_dir, name)
        if os.path.isdir(p) and (not case_name or _shot_case_of(name) == case_name):
            dirs.append(p)
    n, freed = keep_newest(dirs, keep)
    if n:
        log.info(f"[清理] 截图循环: {case_name or '全部用例'} 删最旧 {n} 次, "
                 f"释放 {freed / 1048576:.1f}MB")
    return n, freed
