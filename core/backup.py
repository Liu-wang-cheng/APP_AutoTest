# -*- coding: utf-8 -*-
"""用户数据自动备份: 每次启动把关键数据打包成一份带时间戳的 zip。

★ 为什么要它(用户要求 2026-09-24)
  打包发布后用户数据与程序同目录(便携形态), 整个文件夹被误删、或被一次失败的
  OTA 更新牵连就全没了 —— 而 `config/config.yaml` 不在 git 里(被 gitignore),
  丢了只能手工重建。备份是这条链路上唯一的兜底。

★ 备份什么
  `config/config.yaml` + `Test_cases/` + `Test_preconditions/` + `Test_img/templates/`
  —— 都是"人做出来的、丢了要重做"的东西。
  不备份截图/调试图/报告: 那是可再生的运行产物, 体积还大(一屏就几 MB)。

★ 同一天只备一次(除非 force)
  用户一天开十次程序不该产生十份备份, 否则保留策略会把真正有价值的旧备份
  快速挤掉。判断只看文件名前缀, 不打开 zip。
"""
import os
import time
import zipfile

from core.logger import get_logger

log = get_logger()

BACKUP_KEEP = 10          # 最多保留多少份(超出则删最旧的)
BACKUP_DIRNAME = "backups"

#: (相对 DATA_DIR 的路径, 是否目录) —— 备份范围, 见模块注释
_BACKUP_ITEMS = (
    ("config/config.yaml", False),
    ("Test_cases", True),
    ("Test_preconditions", True),
    ("Test_img/templates", True),
)


def _stamp(now=None, with_time=True):
    return time.strftime("%Y%m%d-%H%M%S" if with_time else "%Y%m%d",
                         time.localtime(now))


def has_today_backup(backup_dir, now=None):
    """今天是否已经备过(只按文件名前缀判断, 不打开 zip)"""
    if not os.path.isdir(backup_dir):
        return False
    prefix = _stamp(now, with_time=False) + "-"
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return False
    return any(n.startswith(prefix) and n.endswith(".zip") for n in names)


def prune(backup_dir, keep=BACKUP_KEEP):
    """只保留最近的 keep 份; 返回删掉的数量(文件名时间戳前缀 ⇒ 字典序即时间序)"""
    if keep <= 0:
        return 0
    try:
        files = sorted(f for f in os.listdir(backup_dir) if f.endswith(".zip"))
    except OSError:
        return 0
    removed = 0
    for f in files[:-keep]:
        try:
            os.remove(os.path.join(backup_dir, f))
            removed += 1
        except OSError:
            pass
    return removed


def make_backup(data_dir=None, keep=BACKUP_KEEP, force=False, now=None):
    """执行一次备份; 返回 zip 路径(当天已备过 / 无内容 / 失败时返回 "")。

    data_dir 缺省用 core.driver.DATA_DIR; now 可注入(测试用固定时间)。
    ★ 任何失败都只记日志、不抛 —— 备份不该拖垮启动。
    """
    from core.driver import DATA_DIR
    base = data_dir or DATA_DIR
    backup_dir = os.path.join(base, BACKUP_DIRNAME)

    if not force and has_today_backup(backup_dir, now):
        log.info("[备份] 今天已经备份过, 跳过")
        return ""

    items = []
    for rel, is_dir in _BACKUP_ITEMS:
        full = os.path.join(base, rel)
        if os.path.exists(full):
            items.append((full, is_dir))
    if not items:
        log.info("[备份] 没有可备份的数据(新环境?), 跳过")
        return ""

    try:
        os.makedirs(backup_dir, exist_ok=True)
    except OSError as e:
        log.warning(f"[备份] 建目录失败(忽略): {e}")
        return ""

    path = os.path.join(backup_dir, f"{_stamp(now)}.zip")
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for full, is_dir in items:
                if is_dir:
                    for root, _dirs, files in os.walk(full):
                        for fn in files:
                            f = os.path.join(root, fn)
                            zf.write(f, os.path.relpath(f, base))
                else:
                    zf.write(full, os.path.relpath(full, base))
    except Exception as e:
        log.warning(f"[备份] 写入失败(不影响使用): {e}")
        try:
            os.remove(path)          # 别留半截 zip 骗人
        except OSError:
            pass
        return ""

    removed = prune(backup_dir, keep)
    log.info(f"[备份] 已备份到 {path}" + (f"; 清理了 {removed} 份旧备份" if removed else ""))
    return path
