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
_UPDATE_LEFTOVERS = ("_update_download.exe",)
#: 旧程序备份(onefile 更新把当前 exe 改名成 <名>.exe.bak, 杀软锁定时可能残留);
#: 名字不固定(用户可能重命名过 exe), 用 glob 找
_LEFTOVER_GLOB = "*.exe.bak"


def cleanup_update_leftovers(app_dir=None):
    """清掉上次更新中断留下的残留; 返回清理掉的条目。

    ★ 正常情况下更新 bat 自己会清; 但杀软可能短暂锁定导致残留 —— 启动时再兜一道
      (此刻新版本已在运行, 旧 .bak 必然没用了)。任何失败都吞掉。
    """
    import glob as _glob
    import shutil
    # ★ app_dir 必须单独判断 —— 写成 `app_dir or X if frozen else ""` 会被解析成
    #   `(app_dir or X) if frozen else ""`: 未打包时 app_dir 被整个丢掉, 清理落空
    if app_dir:
        root = os.path.abspath(app_dir)
    elif getattr(sys, "frozen", False):
        root = os.path.dirname(os.path.abspath(sys.executable))
    else:
        return []
    if not root or not os.path.isdir(root):
        return []
    cleaned = []
    for name in _UPDATE_LEFTOVERS:
        p = os.path.join(root, name)
        if not os.path.exists(p):
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                os.remove(p)
            if not os.path.exists(p):
                cleaned.append(name)
        except OSError:
            pass
    for p in _glob.glob(os.path.join(root, _LEFTOVER_GLOB)):
        try:
            os.remove(p)
            cleaned.append(os.path.basename(p))
        except OSError:
            pass
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
