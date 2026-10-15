# -*- coding: utf-8 -*-
# 由 gui/main_window.py 拆分而来(结构整理 2026-10-15):
# 后台线程: 设备扫描、更新检查、更新下载。
# ★ 拆分只为可读性 —— 这批类原先就在同一个模块里, 行为未改动。

# 只导入本模块用到的名字(结构整理 2026-10-15)
import os
import sys
import time
from PySide6.QtCore import QThread, Signal
from core import app_detect
from core.logger import get_logger

log = get_logger()


class DeviceScanThread(QThread):
    """设备扫描放后台线程:adb 补连/去重要跑多个子进程(最长达 30s),
    放 GUI 线程会把整个窗口卡成"无响应"(实测)。"""
    done = Signal(list, str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        try:
            devices = app_detect.resolve_devices(self._cfg)
        except Exception:
            time.sleep(1.5)              # adb server 冷启动等瞬态:重试一次
            try:
                devices = app_detect.resolve_devices(self._cfg)
            except Exception as e2:
                self.done.emit([], str(e2))
                return
        self.done.emit(devices, "")


class UpdateCheckThread(QThread):
    """后台检查更新: 多镜像并发测速最坏要十几秒, 放 GUI 线程会把窗口卡住。

    ★ 只检查、不下载不安装 —— 下载交给后续带进度条的流程。
    """
    done = Signal(object)          # 参数是 updater.CheckResult

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self._cfg = cfg

    def run(self):
        from core import updater
        try:
            result = updater.check_for_update(self._cfg)
        except Exception as e:     # 兜底: 检查更新绝不能把线程跑崩
            result = updater.CheckResult("error", str(e))
        self.done.emit(result)


class UpdateDownloadThread(QThread):
    """后台下载更新包 -> 校验 sha256 -> 解包 -> 生成替换脚本。

    全程不碰界面; 进度经 progress 信号推送。
    """
    progress = Signal(int, int, str)   # (已下载字节, 总字节, 速度文本; 总未知=0)
    done = Signal(str, str)            # (bat 路径, ""=成功 / 失败原因)

    def __init__(self, cfg, info, mirror, parent=None, ranked=None):
        super().__init__(parent)
        self._cfg = cfg
        self._info = info
        self._mirror = mirror
        self._ranked = ranked      # 全部镜像测速结果: 下载时另选最快的(见 run)

    def run(self):
        from core import updater
        from core.driver import DATA_DIR
        try:
            # 下载走镜像加速: 版本清单所在的镜像若配了 download_prefix 就用它
            # ★ 但**下载要独立选源**, 不能跟着"取版本清单"那个走 —— 清单优先 GitHub
            #   直连(求最新、避开 CDN 缓存), 而直连拉大文件实测几乎不可用(3MB 只得到
            #   0 字节); 跟着走就等于下载走直连, 永远下不完。这里挑带加速前缀、
            #   延迟最低的那个。
            dmirror = updater.pick_download_mirror(self._ranked)
            prefix = getattr(dmirror, "download_prefix", "") or ""
            if dmirror is not None:
                log.info(f"[更新] 下载走镜像: {dmirror.name}"
                         f"(加速前缀 {prefix or '无, 直连'})")
            else:
                # 没测速结果就退回直连 —— 直连拉大文件实测会卡住, 至少要留个痕迹,
                # 别让"下载不动"变成一个没有线索的现象
                log.warning("[更新] 没有可用的加速镜像, 本次下载走直连(可能很慢)")
            url = updater.apply_download_prefix(self._info.download_url, prefix)
            if not url:
                self.done.emit("", "版本清单里没有 download_url")
                return
            # 目录模式: 下载的是分发包 zip(内含 AutoTest.exe + _internal\)
            # ★ 分块并发下载(2026-10-13): 连接数由 config update.download_connections
            #   控制(默认 4) —— 加速镜像按单连接限速, 多连接常能翻几倍
            pkg = os.path.join(DATA_DIR, "_update_download.zip")
            updater.download(url, pkg,
                             connections=updater.download_connections_from_cfg(
                                 (self._cfg or {}).get("update")),
                             progress_cb=lambda d, t, s: self.progress.emit(d, t, s))
            if not updater.verify_sha256(pkg, self._info.sha256):
                try:
                    os.remove(pkg)
                except OSError:
                    pass
                self.done.emit("", "下载包校验失败(sha256 不符), 已删除")
                return
            # ★ 校验 + 解压都在**程序还活着**的时候做: 失败就报错收场, 一个文件都没动;
            #   等 bat 那边只剩两次瞬时改名, 被中途打断的窗口最小
            updater.validate_update_zip(pkg, self._info.version)
            self.progress.emit(0, 0, "正在解压更新包…")
            extracted = os.path.join(DATA_DIR, "_update_extracted")
            updater.extract_update(pkg, extracted)
            log.info(f"[更新] 更新包已解压并校验: v{self._info.version}")
            # ★ 把**正在运行的 exe 名**交给替换脚本(F3): 用户可能把 exe 改过名,
            #   bat 不该靠"目录里字母序第一个 .exe"猜
            bat = updater.generate_update_bat(
                DATA_DIR, os.getpid(), os.path.basename(sys.executable))
            self.done.emit(bat, "")
        except Exception as e:
            self.done.emit("", f"下载/准备更新失败: {e}")
