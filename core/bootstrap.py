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
"""
import os
import re
import shutil
import sys

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
_SEED_DIRS = ("Test_cases", "Test_img/templates")
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
