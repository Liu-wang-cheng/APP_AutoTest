"""用例编排器主窗口(卡片式简洁版)

布局: 顶部工具栏(文件/设备/前置/运行) + 快捷组件条 + 步骤卡片列表 + 底部结果页签
- 添加步骤: 点快捷组件或「＋添加步骤」分类菜单
- 编辑参数: 点卡片原位展开,高级参数折叠收起
- 数据模型: dict 列表,YAML 文件是唯一持久化格式
"""
import copy
import os
import shutil
import re
import time

import yaml
from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QIntValidator, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core import app_detect
from core.driver import BASE_DIR, load_config, update_config, is_case_file
from core.logger import get_logger
from core.version import __version__ as APP_VERSION   # 版本号单一真源: core/version.py

log = get_logger()
from gui import schema
from gui.runner_thread import RunWorker


CASES_DIR = os.path.join(BASE_DIR, "Test_cases")


class MainWindow(QMainWindow):
    log_signal = Signal(str)      # 后台线程/核心模块的日志 → 运行日志页签

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"APP 自动化测试平台 v{APP_VERSION}")
        # 应用图标(自绘 乐动品牌融合): ico 含 16~256 多尺寸(高分屏不发虚)
        _icon_path = os.path.join(_ASSETS, "app_icon.ico")
        if not os.path.isfile(_icon_path):
            _icon_path = os.path.join(_ASSETS, "app_icon.png")
        if os.path.isfile(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.worker = None
        self.expanded_key = None  # 展开的卡片: (顶层序号,) 或 (父序号, 子序号)
        self._preview_path = None  # 当前预览的截图路径

        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(8, 6, 8, 6)
        v.setSpacing(5)
        v.addWidget(self._build_toolbar())
        v.addWidget(self._build_status_line())      # ★ 运行状态独立一行(用户要求)
        v.addWidget(self._build_env_strip())
        v.addWidget(self._build_env_status_line())  # ★ 配置提示独立一行(用户要求)
        # ★ 用例列表(左,固定宽 200) | 右列 = 组/用例信息条(上)+ 步骤详情(下)
        #   同一垂直列 —— 步骤详情与组信息同列,不横跨在列表/信息条下方
        #   (2026-09-21 二次调整;列表仍在左侧,右侧是「组信息+步骤详情」)
        case_panel = self._build_case_list()
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(5)
        rv.addWidget(self._build_chip_strip())
        rv.addWidget(self._make_cards_area(), 1)
        mid = QWidget()
        h = QHBoxLayout(mid)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)
        h.addWidget(case_panel)
        h.addWidget(right, 1)
        v.addWidget(mid, 3)
        v.addWidget(self._make_bottom(), 2)

        self.setCentralWidget(page)
        self.setStyleSheet(STYLESHEET)

        # 快捷键: Ctrl+S 保存
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+S"), self, self.on_save)

        # 默认窗口大小:固定屏幕可用区域的 80%,居中显示。
        # 不保存/恢复上次窗口几何 —— 保证每次启动尺寸一致(用户要求)
        avail = QApplication.primaryScreen().availableGeometry()
        w, h = int(avail.width() * 0.8), int(avail.height() * 0.8)
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2,
                  avail.y() + (avail.height() - h) // 2)

        self._load_case_into_ui()
        self.render_cards()
        self._fill_env_from_config()
        self._attach_log_handler()      # core 日志 → 运行日志页签
        self._setup_update_check()      # OTA: 启动后延迟检查 + 每 8 小时自动检测

    # ── 顶部工具栏 ──
    # ── OTA 自动更新 ──

    def _update_message(self, heading, body="", kind="info", buttons=None,
                        default=0):
        """与界面主题一致的更新弹窗。

        ★ 裸 QMessageBox 是系统默认的灰色小盒子, 与蓝色主题的界面很违和; 这里统一
          套一套配色/圆角/中文按钮, 主按钮用主题蓝。
        kind: info / warn / error —— 只影响图标, 不改布局。
        返回被点击按钮的文本。
        """
        box = QMessageBox(self)
        box.setWindowTitle("检查更新")
        box.setIcon({"info": QMessageBox.Information, "warn": QMessageBox.Warning,
                     "error": QMessageBox.Critical}.get(kind, QMessageBox.Information))
        box.setText(f"<div style='font-size:14px;font-weight:bold;color:#1f2937'>{heading}</div>")
        if body:
            box.setInformativeText(body)
        box.setStyleSheet("""
            QMessageBox { background: #ffffff; }
            QMessageBox QLabel { color: #475569; font-family: "Microsoft YaHei UI";
                                 font-size: 12px; line-height: 160%; }
            QMessageBox QPushButton {
                background: #ffffff; border: 1px solid #d9dee6; border-radius: 6px;
                padding: 6px 20px; color: #333; min-width: 80px;
            }
            QMessageBox QPushButton:hover { border-color: #2563eb; color: #2563eb; }
            QMessageBox QPushButton#primaryBtn {
                background: #2563eb; color: white; border: none; font-weight: bold;
            }
            QMessageBox QPushButton#primaryBtn:hover { background: #1d4fd7; }
        """)
        texts = list(buttons or ["知道了"])
        made = []
        for i, t in enumerate(texts):
            b = box.addButton(t, QMessageBox.AcceptRole if i == default
                              else QMessageBox.RejectRole)
            if i == default:
                b.setObjectName("primaryBtn")     # 主按钮走主题蓝
            made.append(b)
        box.setDefaultButton(made[default])
        # ★ 抢前台(用户 2026-10-15: "弹窗优先显示"): 自动检查更新的弹窗多在用户
        #   切到别的应用时出现 —— 不主动 raise 会被压在别的窗口后面, 用户根本看不到
        self.raise_()
        self.activateWindow()
        box.raise_()
        box.activateWindow()
        box.exec()
        clicked = box.clickedButton()
        for b, t in zip(made, texts):
            if b is clicked:
                return t
        return ""

    def _setup_update_check(self):
        """启动后延迟检查一次, 之后每 N 小时自动检测(用户要求)。

        ★ 延迟几秒再查: 首次检查要并发测多个镜像, 放在窗口刚显示时做会拖慢启动感知。
        ★ 定时器周期取配置里的 check_interval_hours(默认 8) —— 程序挂在测试机上常年
          不关时靠它持续覆盖; 只靠"启动时检查"对这种情况只生效一次。
        """
        from core import updater
        try:
            conf = updater.load_update_config(load_config())
            hours = float(conf.get("check_interval_hours")
                          or updater.CHECK_INTERVAL_HOURS)
        except Exception:
            hours = float(updater.CHECK_INTERVAL_HOURS)
        self._update_thread = None
        self._update_manual = False      # 本次检查是用户手动点的?(手动必须有反馈)
        self._update_timer = QTimer(self)
        self._update_timer.setInterval(max(1, int(hours * 3600 * 1000)))
        self._update_timer.timeout.connect(self._check_update_now)
        self._update_timer.start()
        # 首次检查用**独立且可停止**的定时器, 不用 QTimer.singleShot:
        # singleShot 没有句柄、取消不掉, 而它 3 秒后会在测试运行期间触发一次真实
        # 网络检查(并可能走到模态弹窗), 污染后面的测试(实测: 与 test_preconditions
        # 同跑时后者卡死)。
        self._first_check_timer = QTimer(self)
        self._first_check_timer.setSingleShot(True)
        self._first_check_timer.setInterval(3000)      # 启动 3 秒后查一次
        self._first_check_timer.timeout.connect(self._check_update_now)
        self._first_check_timer.start()
        self._setup_backup_cleanup()

    #: 窗口稳定运行多久之后, 才认定"这次更新确实成功了"
    BACKUP_CLEANUP_DELAY_MS = 120_000

    def _setup_backup_cleanup(self):
        """延迟清理更新留下的旧程序备份(*.exe.bak)与残渣。

        ★ 为什么不在启动时清: .bak 是"新版本起不来就回滚"的唯一退路 —— 更新脚本
          要在新进程起来 8 秒后才判定存活, 启动那一瞬间把它删掉, 回滚脚本将无路可退
          (F2)。所以延到窗口稳定运行 2 分钟后, 以及正常关窗时(见 closeEvent)。
        """
        self._backup_cleanup_timer = QTimer(self)
        self._backup_cleanup_timer.setSingleShot(True)
        self._backup_cleanup_timer.setInterval(self.BACKUP_CLEANUP_DELAY_MS)
        self._backup_cleanup_timer.timeout.connect(self._cleanup_update_backups)
        self._backup_cleanup_timer.start()

    def _cleanup_update_backups(self):
        """真正干活的那个(幂等; 任何失败都只记日志)。

        ★ 故意**不传** app_dir: 由 bootstrap 自己只在打包环境推程序目录。开发环境
          (APP_DIR 就是仓库根)传进去等于允许"顺手删仓库里的文件", 与项目铁律
          「脚本绝不改动用户/仓库状态」冲突 —— 而开发环境根本不会产生这些残渣。
        """
        try:
            self._backup_cleanup_timer.stop()   # 已经清了, 别再排着(关窗路径也会走到这)
        except Exception:
            pass
        # ★ 已下好、但用户还没点「立即重启」的那次更新不能清: 包和替换脚本都还要用
        #   (下载完成的弹窗上点「稍后」就是这种状态 —— 定时器到点会把它们一并删掉,
        #    用户再点重启就变成"替换脚本不存在")
        pending = getattr(self, "_update_bat_path", "")
        if pending and os.path.exists(pending):
            log.debug("[更新] 有已就绪的更新未执行, 跳过本次清理")
            return
        try:
            from core.bootstrap import cleanup_old_backup
            cleanup_old_backup()
        except Exception as e:
            log.debug(f"[更新] 清理旧程序备份失败(忽略): {e}")

    def on_check_update(self):
        """「检查更新」按钮: 手动检查。

        与自动检查的区别是**必须有反馈** —— 自动检查每 8 小时静默跑一次, 只在发现
        新版时弹窗(平时不打扰); 手动点是用户的明确意图, 所以"已是最新""检查失败"
        "没配仓库"都要说一声, 否则点了没反应, 用户会以为按钮坏了。
        """
        if self.worker:
            self._update_message("现在不方便检查",
                                 "用例正在执行中。等这一轮跑完再点即可 —— "
                                 "反正更新也要重启程序才能生效。", kind="warn")
            return
        if self._update_thread is not None and self._update_thread.isRunning():
            self._set_status("正在检查更新，请稍候…", self.status_label)
            return
        self._update_manual = True
        self.update_btn.setEnabled(False)
        self._set_status("正在检查更新…", self.status_label)
        # ★ 起不来线程时必须**把按钮点亮回去**: 检查期间按钮是灰的, 而"恢复正常"
        #   只写在回调 _on_update_checked 里 —— 线程压根没起来就不会有回调, 按钮
        #   从此永久置灰(用户只能重启程序)。配置瞬时读不到(企业加密文件正在解密)
        #   在这个项目里是承认存在的场景, 不是假想。
        if not self._check_update_now():
            self.update_btn.setEnabled(not getattr(self, "_locked", False))
            self._update_manual = False
            self._set_status("检查更新没启动起来，请稍后再试", self.status_label)

    def _check_update_now(self):
        """发起一次检查; 返回是否真的起了后台线程。

        正在跑用例时**整轮跳过**(用户要求: 执行期间不检测新版本)。
        """
        if self.worker:
            log.info("[更新] 正在执行用例, 跳过本次检查")
            return False
        if self._update_thread is not None and self._update_thread.isRunning():
            return False                             # 上一次还没结束
        try:
            self._update_thread = UpdateCheckThread(load_config(), self)
            self._update_thread.done.connect(self._on_update_checked)
            self._update_thread.start()
            return True
        except Exception as e:
            log.warning(f"[更新] 检查失败(忽略): {e}")
            return False

    def _on_update_checked(self, result):
        """检查结果: 自动检查只在「有新版本」时打扰用户; 手动检查则任何结果都要回话"""
        manual = bool(getattr(self, "_update_manual", False))
        self._update_manual = False
        if hasattr(self, "update_btn"):
            # 检查期间禁用了按钮, 这里恢复 —— 但若此刻正在跑用例(锁定中)必须保持禁用,
            # 否则会把 _set_locked 刚置的灰又点亮
            self.update_btn.setEnabled(not getattr(self, "_locked", False))
        log.info(f"[更新] {result.message}")

        if result.status != "has_update":
            if manual:
                if result.status == "up_to_date":
                    self._update_message(
                        "已是最新版本",
                        f"当前版本 v{APP_VERSION} 就是最新的。\n\n"
                        "程序在启动时、以及之后每 8 小时会自动检查一次，"
                        "有新版本会第一时间提示你。")
                elif result.status == "error":
                    self._update_message(
                        "暂时无法检查更新",
                        f"{result.message}\n\n"
                        "多半是网络或代理限制，稍后会自动重试 —— 不影响程序正常使用。",
                        kind="warn")
                else:                      # skipped: 没配仓库
                    self._update_message(
                        "未启用自动更新",
                        "如需启用，请在 config/config.yaml 的 update.repository "
                        "里填入 GitHub 的 owner/repo。\n\n"
                        "（企业内网环境如果不方便外联，保持现状即可。）")
                self._set_status("就绪", self.status_label)
            return

        info = result.info
        notes = (info.release_notes or "").strip()
        if len(notes) > 600:
            notes = notes[:600] + "…"
        head = f"发现新版本 v{info.version}"
        if result.force:
            head += "（需更新）"
        body = f"当前版本：v{APP_VERSION}\n最新版本：v{info.version}"
        if info.release_date:
            body += f"（{info.release_date}）"
        if notes:
            body += f"\n\n更新内容\n{notes}"
        buttons = ["立即更新"] if result.force else ["立即更新", "稍后再说"]
        picked = self._update_message(head, body, buttons=buttons, default=0)
        if picked == "立即更新":
            self._start_update_download(result)

    def _start_update_download(self, result):
        """后台下载更新包(下载中再次触发直接忽略)"""
        if getattr(self, "_update_download_thread", None) and \
                self._update_download_thread.isRunning():
            self._set_status("更新包正在下载，请稍候…", self.status_label)
            return
        self._set_status(f"正在下载更新 v{result.info.version}…", self.status_label)
        self._dl_logged_key = None          # 进度日志台阶清零(见 _on_download_progress)
        self._update_download_thread = UpdateDownloadThread(
            load_config(), result.info, result.mirror, self,
            ranked=getattr(result, "ranked", None))
        self._update_download_thread.progress.connect(self._on_download_progress)
        self._update_download_thread.done.connect(self._on_download_done)
        self._update_download_thread.start()

    def _on_download_progress(self, got, total, speed):
        """下载进度: 界面每次都刷新, **日志只在跨过 5% 台阶时记一行**。

        ★ 进度回调原来每 64KB 一次、每次都经 _set_status 落一行日志 —— 208MB 的包
          会写 3000+ 行, 把运行日志和"这一轮到底发生了什么"全冲没了(updater.download
          那边也做了节流, 这里是第二道)。
        """
        if total > 0:
            key, step = got * 100 // total, 5     # 百分比台阶
            msg, done = f"正在下载更新 {key}%（{speed}）", key >= 100
        else:
            key, step = got // (10 << 20), 1      # 总长未知 -> 按 10MB 台阶
            msg, done = f"正在下载更新… {speed}", False
        last = getattr(self, "_dl_logged_key", None)
        if done or last is None or key >= last + step:
            self._dl_logged_key = key
            self._set_status(msg, self.status_label)     # 这个会写日志
        else:
            self.status_label.setText(msg)               # 只刷新界面

    def _on_download_done(self, bat_path, err):
        if err:
            self._set_status("更新失败", self.status_label)
            self._update_message(
                "更新没有成功",
                f"{err}\n\n程序仍是可用的，联网正常时再试一次即可。", kind="error")
            return
        self._update_bat_path = bat_path
        picked = self._update_message(
            "更新已就绪",
            "新版本下载完成并通过校验。\n\n"
            "点「立即重启」后程序会关闭、自动完成替换并重新启动"
            "（你的配置、用例、模板都不会动）。",
            buttons=["立即重启", "稍后"], default=0)
        if picked == "立即重启":
            self._apply_update_and_restart()

    def _apply_update_and_restart(self):
        """启动替换脚本并立刻退出本进程。

        ★ 必须 os._exit 而不是正常退出: 单文件 exe 内已释放的 Qt/opencv DLL 句柄若
          不立刻释放, bat 里的改名/复制会因文件被锁而失败(参考项目实战踩过)。
        ★ 下载期间用户可能又点了「运行」—— 带着正在执行的用例退出会毁掉这一轮,
          必须拦下(下次定时器到点再提示)。
        """
        if self.worker:
            self._update_message(
                "现在不能重启更新",
                "用例正在执行中。等这一轮跑完，再点工具栏的「检查更新」重新操作即可。",
                kind="warn")
            return
        bat = getattr(self, "_update_bat_path", "")
        if not bat or not os.path.exists(bat):
            self._update_message("更新失败", "替换脚本不存在，请重新检查更新。",
                                 kind="error")
            return
        # ★ 留痕: 这行之后本进程会 os._exit(0)(静默退出) —— 有这行才说明是**更新重启**,
        #   而不是崩溃(见 core/crashlog.py)。
        log.info("[退出] 应用更新: 已启动替换脚本, 本进程即将退出并自动重启")
        import subprocess
        subprocess.Popen(
            ["cmd", "/c", bat],
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
            cwd=os.path.dirname(bat),
        )
        os._exit(0)

    def _set_status(self, msg, label=None):
        """同时更新界面提示并写运行日志。

        ★ 用户反馈: 保存配置 / APP 检测这类提示原来只 setText 到界面, 不走
        logging → 运行日志里看不到。统一走这里, 界面与日志都有。
        """
        (label or self.env_status).setText(msg)
        if msg:
            log.info(msg)

    def _attach_log_handler(self):
        """把 core.logger 的日志转到 GUI「运行日志」页签。

        用户要求: GUI 的运行(设备检测/前置等待)与保存配置等动作也要在界面可见,
        不能只写进 reports/test.log。
        """
        import logging
        if getattr(self, "_log_handler", None) is not None:
            return
        outer = self

        class _GuiLogHandler(logging.Handler):
            def __init__(self):
                super().__init__(logging.INFO)
                self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                                     "%H:%M:%S"))

            def emit(self, record):
                try:
                    outer.log_signal.emit(self.format(record))
                except Exception:
                    pass

        self._log_handler = _GuiLogHandler()
        logging.getLogger("vacuum_test").addHandler(self._log_handler)
        self.log_signal.connect(self.log_view.appendPlainText)

    def _build_toolbar(self):
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        for text, fn, attr in [("新建", self.on_new, "new_btn"),
                               ("打开", self.on_open, "open_btn"),
                               ("保存", self.on_save, "save_btn")]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            setattr(self, attr, b)
            lay.addWidget(b)
        self.save_btn.setEnabled(False)   # 初始无修改,保存置灰(有修改才亮起)
        self.file_label = QLabel("未打开文件")
        self.file_label.setStyleSheet("color:#94a3b8;")
        lay.addWidget(self.file_label)
        lay.addStretch()
        # ★ 「故障检测」按钮(用户 2026-10-13): 在线改故障/空闲词表 —— 运行时即时生效
        #   不重启(写 config.yaml 并同步到 core.session 的全局词表), 加在前置条件左边。
        #   弹窗里两个文本框, 一并编辑, 词以逗号/顿号分隔。
        self.fault_btn = QPushButton("故障检测")
        self.fault_btn.setToolTip(
            "配置文本等待与断言轮询里的**故障词**与**空闲词**——\n"
            "· 故障词命中 → 立即报错, 报错和日志带整段原文\n"
            "· 空闲词命中 → 立即判不成立, 不再傻等超时\n"
            "修改后即时生效, 不重启。")
        self.fault_btn.clicked.connect(self.on_edit_status_words)
        lay.addWidget(self.fault_btn)

        pre_btn = QPushButton("前置条件")
        self.pre_menu = QMenu(pre_btn)
        pre_btn.setMenu(self.pre_menu)
        lay.addWidget(pre_btn)
        # 从配置加载前置项(无则用默认 4 项) → 动态生成菜单
        self.preconditions = self._load_preconditions()
        self._build_pre_menu()

        # ★ 启动时把 config 的故障/空闲词表同步到全局(2026-10-13):
        #   不调的话, 之前发的 2.0.12 用内置默认; 现在 GUI 显式弹窗可见默认值,
        #   与 config 注释保持一致。ActionRunner 每次启动也会再调一次, 这里双保险。
        self._load_status_words_from_config()

        # ── 执行次数 ──
        lay.addWidget(QLabel("次数"))
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 99)
        self.repeat_spin.setValue(1)
        self.repeat_spin.setToolTip("勾选用例队列重复的轮数")
        lay.addWidget(self.repeat_spin)

        self.run_btn = QPushButton("▶ 运行")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.clicked.connect(self.on_run)
        lay.addWidget(self.run_btn)
        self.pause_btn = QPushButton("⏸ 暂停")
        self.pause_btn.setObjectName("pauseBtn")
        self.pause_btn.setEnabled(False)
        self.pause_btn.clicked.connect(self.on_pause)
        self.pause_btn.setToolTip(
            "暂停执行: 当前步骤跑完后挂起(设备保持现状)\n"
            "再点一次(变成「▶ 继续」)接着跑; 暂停中超时不计时\n"
            "挂起中随时可以点「停止」")
        lay.addWidget(self.pause_btn)
        self.stop_btn = QPushButton("■ 停止")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.on_stop)
        lay.addWidget(self.stop_btn)

        # 手动检查更新: 平时启动后会自动检查(每 8 小时一次, 静默——只在有新版时弹窗);
        # 手动点这个则**必须有反馈**(已是最新/失败都要说一声), 否则用户以为按钮没反应
        self.update_btn = QPushButton("检查更新")
        self.update_btn.setToolTip("立即检查是否有新版本\n(程序启动后会自动检查一次, 之后每 8 小时一次)")
        self.update_btn.clicked.connect(self.on_check_update)
        lay.addWidget(self.update_btn)

        return bar

    def _hint_line(self, attr, text, color, align=Qt.AlignLeft):
        """提示行: 独占一整行(**组件下方**, 不挂在组件右边)。

        ★ 用户两次反馈: 「报告已生成(部分执行): ...」「配置已保存: ...」这类长文本
        挂在工具栏/环境条右侧时, 会把那一行整体撑宽 → 窗口跟着变大。所以所有提示都
        做成整行显示, 并且**不让它参与宽度决策**(水平策略 Ignored + 自动换行):
        文本再长也只在自己这行里排布(与截图预览 `preview.setSizePolicy` 同一套路)。
        align: 文本在整行内的对齐(运行状态行居中, 配置提示行左对齐)。
        """
        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color:{color};")
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        lbl.setAlignment(align)
        lay.addWidget(lbl)
        setattr(self, attr, lbl)
        return bar

    def _build_status_line(self):
        """运行状态行(工具栏下方): 执行中/报告已生成 等。★ 文本居中(用户要求)"""
        return self._hint_line("status_label", "就绪", "#64748b", Qt.AlignCenter)

    def _build_env_status_line(self):
        """环境配置提示行(环境条下方): 配置已保存/已删除历史记录/检测结果 等"""
        return self._hint_line("env_status", "", "#94a3b8")

    # ── 名称输入(自适应 + 历史下拉 + ×清除) ──

    def _make_name_combo(self, tip, cfg_key, hist_key, record_on_save=True):
        """名称输入框: 宽度随内容自适应, 历史值可下拉选择, 条目右侧 ✕ 删除。

        record_on_save=False(测试APP): 编辑不记历史, 只有「检测成功」才记
        (用户要求: 检测不到的 APP 不该进历史)
        """
        combo = _HistCombo()
        combo.setEditable(True)
        combo.setToolTip(tip + " · 可从历史记录下拉选择, 点条目右侧 ✕ 删除该条")
        combo.setProperty("cfg_key", cfg_key)
        combo.setProperty("hist_key", hist_key)
        combo.setProperty("base_tip", tip)
        combo.setProperty("record_on_save", record_on_save)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        combo.setMinimumContentsLength(6)
        combo.addItems(self._load_name_history(hist_key))
        # ★ 下拉每项右侧 ✕ 删除该条历史(用户要求: 删的是历史记忆, 不是输入框)
        combo.setItemDelegate(_HistComboDelegate(combo.view()))
        combo.removed.connect(lambda row, c=combo: self._remove_name_history(c, row))
        # ★ 宽度随内容自适应(editable combo 的 AdjustToContents 不随输入变化)
        combo.currentTextChanged.connect(lambda _t, c=combo: self._refresh_name_tip(c))
        self._refresh_name_tip(combo)
        combo.activated.connect(lambda _i, c=combo: self._save_env_field_of(c))
        combo.lineEdit().editingFinished.connect(lambda c=combo: self._save_env_field_of(c))
        return combo

    @staticmethod
    def _refresh_name_tip(combo):
        """名称框: 宽度自适应(基类 fit_width) + 悬停显示完整值 + popup 尺寸同步"""
        combo.fit_width()
        txt = combo.currentText()
        combo.setToolTip((combo.property("base_tip") or "") +
                         (("当前值: " + txt) if txt else ""))
        combo.fit_popup_now()

    def _remove_name_history(self, combo, row):
        """删除下拉中第 row 条历史(同步 QSettings + 刷新列表, 当前值被删则清空)"""
        hist_key = combo.property("hist_key")
        if not hist_key or row < 0 or row >= combo.count():
            return
        removed_text = combo.itemText(row)
        hist = self._load_name_history(hist_key)
        hist = [v for v in hist if v != removed_text]
        st = QSettings("vacuum_test", "case_studio")
        st.setValue(hist_key, hist)
        keep = combo.currentText()
        removed_current = bool(keep) and keep == removed_text
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(hist)
        if removed_current:
            combo.setCurrentText("")     # 删掉的正是当前值 → 清空
        elif keep:
            combo.setCurrentText(keep)
        combo.blockSignals(False)
        # ★ 信号屏蔽期间宽度不重算, 这里补一次(删除后内容可能变化)
        self._refresh_name_tip(combo)
        self._set_status(f"已删除历史记录: {removed_text}")
        if removed_current:
            # ★ 用户要求: 删的若是「当前生效的值」, 就把它连 config 一起清掉。
            #   否则输入框清空了、配置里还在 → 重启按配置回填回来, 用户看到
            #   的却是"删了下次启动又出现"(真机实测)。
            self._clear_env_field(combo)

    def _reload_name_combo(self, combo):
        """按最新历史重载下拉项(保留当前输入), 并同步宽度"""
        hist_key = combo.property("hist_key")
        if not hist_key:
            return
        keep = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(self._load_name_history(hist_key))
        combo.setCurrentText(keep)
        combo.blockSignals(False)
        # ★ 信号被屏蔽期间宽度不会自动重算, 这里显式补一次
        self._refresh_name_tip(combo)

    def _purge_name_history(self, hist_key, value):
        """把某个值从历史中移除(用于「检测不到的 APP 不该记历史」, 用户要求)"""
        if not hist_key or not value:
            return
        hist = self._load_name_history(hist_key)
        if value not in hist:
            return
        hist = [v for v in hist if v != value]
        st = QSettings("vacuum_test", "case_studio")
        st.setValue(hist_key, hist)
        combo = getattr(self, "app_name_edit", None)
        if combo is not None and combo.property("hist_key") == hist_key:
            keep = combo.currentText()
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(hist)
            combo.setCurrentText(keep)     # 输入框保留用户所写, 只是不再进历史
            combo.blockSignals(False)

    @staticmethod
    def _load_name_history(hist_key):
        st = QSettings("vacuum_test", "case_studio")
        val = st.value(hist_key, [])
        if isinstance(val, str):
            val = [val] if val else []
        return list(val or [])[:10]

    @staticmethod
    def _push_name_history(hist_key, value):
        """把值记入历史(去重, 最近在前, 最多 10 条)"""
        if not value:
            return
        st = QSettings("vacuum_test", "case_studio")
        cur = st.value(hist_key, [])
        if isinstance(cur, str):
            cur = [cur] if cur else []
        cur = [v for v in (cur or []) if v != value]
        cur.insert(0, value)
        st.setValue(hist_key, cur[:10])

    def _save_env_field_of(self, combo):
        """combo 版环境字段保存: 写 config + 记历史 + 刷新下拉项"""
        from PySide6.QtWidgets import QComboBox as _C
        w = combo if isinstance(combo, _C) else self.sender()
        if w is None:
            return
        key = w.property("cfg_key")
        value = w.currentText().strip()
        if not key or not value:
            return
        # ★ 同值不重复保存: 从下拉点选一项时 activated 与 editingFinished 会各发一次
        #   (见 _make_name_combo 的接线), 不去重会写两次盘、运行日志出现两行相同提示
        #   (用户真机实测: 同一秒两条「配置已保存: target_device = ...」)
        if w.property("last_saved_value") == value:
            return
        try:
            update_config({key: value})
            self._set_status(f"配置已保存: {key} = {value}")
        except Exception as e:
            QMessageBox.critical(self, "保存配置失败", str(e))
            return
        w.setProperty("last_saved_value", value)
        hist_key = w.property("hist_key")
        if hist_key and w.property("record_on_save") is not False:
            self._push_name_history(hist_key, value)
            self._reload_name_combo(w)

    def _clear_env_field(self, combo):
        """把该字段对应的配置值清空(值写为 "")

        ★ 用户要求: 点 ✕ 删掉的若是「当前生效的值」, 连 config 一起清 ——
        否则输入框清空了、配置里还留着, 重启又回填回来。

        注意: 这只由「✕ 删除当前值」这个明确动作触发; 输入框手动清空后失焦
        不会清配置(_save_env_field_of 对空值直接 return), 避免误清。
        """
        key = combo.property("cfg_key")
        if not key:
            return
        try:
            update_config({key: ""})
        except Exception as e:
            QMessageBox.critical(self, "清空配置失败", str(e))
            return
        combo.setProperty("last_saved_value", "")   # 空值如今也是"已写盘"
        self._set_status(f"已清空配置: {key}")

    def _build_env_strip(self):
        """环境配置条(流式布局,窗口窄时自动换行): 设备优先,其次测试APP"""
        strip = QFrame()
        strip.setObjectName("chipStrip")
        lay = FlowLayout(strip, margin=5, spacing=6)
        self.env_strip = strip
        self.env_strip_lay = lay

        # ── 设备(最前) ──
        lay.addWidget(QLabel("设备"))
        self.device_combo = QComboBox()
        # 按最长项自适应宽度:设备标签「MuMu模拟器 (127.0.0.1:7555)」较长,
        # 默认策略会截断显示不全
        self.device_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.device_combo.setMinimumContentsLength(18)
        self.device_combo.setToolTip("自动检测 adb 在线的真机/模拟器,选择执行设备")
        lay.addWidget(self.device_combo)

        refresh_btn = QPushButton("↻")
        refresh_btn.setToolTip("重新检测在线设备")
        refresh_btn.clicked.connect(self._refresh_devices)
        lay.addWidget(refresh_btn)

        sep = QLabel("|")
        sep.setStyleSheet("color:#e2e8f0;")
        lay.addWidget(sep)

        # ── 测试APP ──
        lay.addWidget(QLabel("测试APP"))
        self.app_name_edit = self._make_name_combo(
            "测试APP名称(中文/英文均可),作为包名自动检测依据", "app.name",
            "hist/app_name", record_on_save=False)   # 仅检测成功才记历史
        lay.addWidget(self.app_name_edit)

        self.detect_btn = QPushButton("🔍 检测")
        self.detect_btn.setToolTip("按APP名称在设备已装应用中匹配包名,并自动解析启动页")
        self.detect_btn.clicked.connect(self.on_detect_app)
        lay.addWidget(self.detect_btn)

        # 包名/启动页不常驻界面: 点「检测」后结果直接显示在右侧状态文字,并写入配置

        lay.addWidget(QLabel("设备名称"))
        self.device_name_edit = self._make_name_combo(
            "APP 内的扫地机设备名称(如 SE3L),前置阶段自动点击进入该设备页",
            "target_device", "hist/device_name")
        lay.addWidget(self.device_name_edit)
        # ★ env_status 不放在这条流式布局里(会被长文本撑宽) —— 见 _build_env_status_line
        return strip

    def _fill_env_from_config(self):
        cfg = {}
        try:
            cfg = load_config()
        except Exception:
            pass
        app = cfg.get("app", {})
        # ★ YAML 会把纯数字值解析成 int(如 target_device: 111), 必须转字符串
        #   —— 否则 setCurrentText(int) 抛 TypeError, 窗口构造直接失败(启动"卡住")
        self.app_name_edit.setCurrentText(str(app.get("name") or ""))
        self.device_name_edit.setCurrentText(str(cfg.get("target_device") or ""))
        # ★ 登记"当前已写盘的值": 启动回填的值本来就是 config 里的值, 不算一次修改。
        #   否则用户只是点一下输入框再点到别处(editingFinished, 值没变)就会触发一次
        #   "保存" —— 不但重复写盘, 还会把这个值重新记回历史, 等于把用户刚用 ✕
        #   删掉的历史项又撤销回来(用户真机实测: 删了下次启动又出现)。
        for c in (self.app_name_edit, self.device_name_edit):
            c.setProperty("last_saved_value", c.currentText().strip())
        self._refresh_devices()

    # ── 设备检测 ──
    def _refresh_devices(self):
        """后台线程扫描设备并回填下拉框(检测在 GUI 线程跑会卡死界面)"""
        if getattr(self, "_scan_thread", None) and self._scan_thread.isRunning():
            return                       # 上一次扫描还没完,避免堆积
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        self.device_combo.addItem("正在检测设备...", None)
        self.device_combo.blockSignals(False)
        self._set_status("正在检测设备...")
        try:
            cfg = load_config()
        except Exception:
            cfg = {}
        self._scan_thread = DeviceScanThread(cfg, self)
        self._scan_thread.done.connect(self._fill_devices)
        self._scan_thread.start()

    def _fill_devices(self, devices, err):
        """设备扫描结果回填(GUI 线程,由 DeviceScanThread.done 触发)"""
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        if err:
            self._set_status(f"adb 检测失败: {err}")
        elif devices:
            self.env_status.setText("")
        if not devices:
            self.device_combo.addItem("未检测到设备(检查 adb)", None)
        for dev in devices:
            self.device_combo.addItem(dev["label"], dev["id"])
        self.device_combo.blockSignals(False)

    def _build_case_list(self):
        """用例列表面板:Test_cases/ 下全部用例,复选框勾选参与批量执行。
        每行右侧 ↑/↓ 箭头调整执行顺序,顺序写回各用例 YAML 顶层 case_order 字段。
        位于环境条(设备)下方,与组/用例信息条左右并排。返回面板容器。"""
        panel = QWidget()
        v = QVBoxLayout(panel)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(2)
        head = QHBoxLayout()
        list_title = QLabel("用例列表")   # 与「测试步骤详情」同一标题样式(用户要求)
        list_title.setStyleSheet("color:#2563eb; font-weight:bold; font-size:13px;")
        head.addWidget(list_title)
        head.addStretch(1)
        all_btn = QPushButton("全选")
        all_btn.setToolTip("勾选当前所选 APP 组的全部用例(没选用例时才勾选全部)")
        all_btn.clicked.connect(self.on_select_all_cases)
        head.addWidget(all_btn)
        none_btn = QPushButton("清空")
        none_btn.setToolTip("取消当前所选 APP 组的勾选(没选用例时才清全部)")
        none_btn.clicked.connect(self.on_clear_cases)
        head.addWidget(none_btn)
        del_btn = QPushButton("删除")
        del_btn.setToolTip("删除当前选中的用例文件, 或整组目录(含组内全部用例)")
        del_btn.clicked.connect(self.on_delete_selected)
        head.addWidget(del_btn)
        v.addLayout(head)

        # ★ self.case_list 必须指向真正的 QListWidget(曾被 panel 覆盖,
        #   导致勾选/运行时 AttributeError)
        self.case_list = CaseListWidget()
        self.case_list.setObjectName("caseList")
        self.case_list.set_owner(self)
        self.case_list.setFixedWidth(200)    # 宽度与设备下拉框视觉对齐;高度随步骤详情区填满右列
        self._case_states = {}               # path → 'running'/'passed'/'failed'(状态灯)
        self._collapsed_groups = set()       # 折叠的组目录名
        #: 见过哪些组 —— 只对"第一次见到"的组做默认折叠, 用户手动展开过的不再动它
        self._known_groups = set()
        # ★ 折叠状态要**跨启动记住**(用户要求: 下次打开保持原状)。
        #   存"展开过的组"(而不是折叠的): 新出现的组天然不在里面 -> 默认折叠,
        #   正好也满足"默认收回 APP 组"。
        self._expanded_saved = self._load_expanded_groups()
        # ★ 勾选态的**模型层**表示(路径集合)。不能只依赖 QListWidget 的 checkState:
        #   折叠组的用例行根本不生成, 每次重建列表都会把它们的勾选丢掉(用户实测:
        #   勾好用例 → 收起该组整理视图 → 点运行, 那些用例不会跑, 而折叠态下连
        #   "没勾上"都看不见)。勾选是"将要执行什么"的唯一依据, 必须独立于视图。
        self._checked = set()
        self._all_case_paths = []            # 最近一次填充的用例顺序(含折叠组)
        self._fill_case_list()
        self.case_list.itemClicked.connect(self._on_case_item_clicked)
        self.case_list.itemChanged.connect(self._on_case_check_changed)
        self.case_list.moved.connect(self.on_case_rows_dropped)
        v.addWidget(self.case_list)
        return panel

    # ── 用例排序(case_order 写入用例 YAML) ──

    @staticmethod
    def _read_case_order(path):
        """读用例 YAML 顶层 case_order;缺失/非法返回 None"""
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            v = data.get("case_order")
            return int(v) if isinstance(v, (int, float)) else None
        except Exception:
            return None

    @staticmethod
    def _write_case_order(path, n):
        """文本级写入顶层 case_order 行(不重排 YAML、不丢注释、不翻转换行符;
        update_config 的教训:newline='' 读写,原 CRLF/LF 原样保留)"""
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        new_text, cnt = re.subn(r"(?m)^case_order:.*$", f"case_order: {n}", text, count=1)
        if cnt == 0:
            # 无该行则插入:跳过文件头注释块,插到第一个实质行之前
            eol = "\r\n" if "\r\n" in text else "\n"
            lines = text.splitlines(keepends=True)
            idx = 0
            for ln in lines:
                s = ln.strip()
                if s and not s.startswith("#"):
                    break
                idx += 1
            lines.insert(idx, f"case_order: {n}{eol}")
            new_text = "".join(lines)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)

    def _load_expanded_groups(self):
        """读回上次**展开**过的组名(用户要求: 折叠状态跨启动保持原状)。

        存"展开的"而不是"折叠的": 新出现的组天然不在里面 -> 默认折叠, 正好满足
        "APP 组默认收回"。读失败/没记录过都返回空集(即全部按默认折叠)。
        """
        try:
            v = QSettings("vacuum_test", "case_studio").value("ui/expanded_groups")
            return set(v) if isinstance(v, (list, tuple)) else set()
        except Exception:
            return set()

    def _save_expanded_groups(self):
        """把"已展开的组"写回 QSettings(关窗口/切组时都调, 保证下次还原)。

        ★ 列表**读不到就一个字都不写**(F11): 原来无条件覆盖, 而 CASES_DIR 读不到
          (目录被挪走/临时不可访问)时 _list_group_dirs() 返回空 → 写进去一个空集合,
          用户攒下来的展开状态被一次静默清空。空集合也一律不写 —— 那同样是
          "这次没读到组", 而不是"用户把组都删了"。残留的旧组名无害: 还原时只对
          当前存在的组查表。
        """
        try:
            groups = set(self._list_group_dirs())
            if not groups:
                return
            QSettings("vacuum_test", "case_studio").setValue(
                "ui/expanded_groups", sorted(groups - self._collapsed_groups))
        except Exception as e:
            log.debug(f"[ui] 保存组展开状态失败(忽略): {e}")

    def _list_group_dirs(self):
        """Test_cases/ 下的组目录(APP 分组)列表,按名称排序"""
        if not os.path.isdir(CASES_DIR):
            return []
        return sorted(d for d in os.listdir(CASES_DIR)
                      if os.path.isdir(os.path.join(CASES_DIR, d))
                      and not d.startswith((".", "_")))

    def _fill_case_list(self, order_paths=None):
        """(重)填充用例列表:**按组目录分节显示**(Test_cases/<APP名>/ = 一组),
        组内按 case_order 升序(缺失排末尾)。组头行不可选/不可勾/不可拖,
        点击组头可折叠/展开该组。order_paths 给定时按它排;
        Test_cases/ 外的当前打开文件也追加显示。"""
        import glob
        # ★ 新出现的组**默认收起**(用户要求: 用例列表默认收回 APP 组, 不要展开)。
        #   只对"第一次见到的组"这样做: 用户手动展开过的组记在 _known_groups 里,
        #   之后重建列表不会又把它收回去(否则每次刷新都白展开一次)。
        for g in self._list_group_dirs():
            if g not in self._known_groups:
                self._known_groups.add(g)
                # 上次展开过的组保持展开; 其余(含新出现的组)默认收起
                if g not in self._expanded_saved:
                    self._collapsed_groups.add(g)
        if order_paths is None:
            infos = []
            for g in self._list_group_dirs():
                gdir = os.path.join(CASES_DIR, g)
                for p in sorted(glob.glob(os.path.join(gdir, "*.yaml"))):
                    if not is_case_file(p):
                        continue        # 前置条件等非用例文件不进列表
                    infos.append((p, g, self._read_case_order(p)))
            extra = self.case_path
            if (extra and os.path.isfile(extra)
                    and all(os.path.abspath(extra) != os.path.abspath(p) for p, *_ in infos)
                    and extra.lower().endswith((".yaml", ".yml"))
                    and CASES_DIR.lower() not in os.path.abspath(extra).lower()):
                infos.append((extra, "未分组", self._read_case_order(extra)))
            infos.sort(key=lambda it: (it[1].lower(), it[2] is None, it[2] or 0,
                                       os.path.basename(it[0])))
            paths = [p for p, *_ in infos]
            groups = {p: g for p, g, _ in infos}
        else:
            paths = list(order_paths)
            # ★ 折叠组的用例行不在可见列表中, 数据必须补全 ——
            #   否则折叠组会被误判为空组, 错插「暂无用例」占位(用户实测)
            import glob as _glob
            for g in self._list_group_dirs():
                if g in self._collapsed_groups:
                    for p in _glob.glob(os.path.join(CASES_DIR, g, '*.yaml')):
                        if not is_case_file(p):
                            continue
                        if p not in paths:
                            paths.append(p)
            groups = {p: os.path.basename(os.path.dirname(p)) for p in paths}
        # ★ 勾选态从**模型层**取, 不是从可见行采集 —— 折叠组的用例行不存在,
        #   从行采集会把它们的勾选丢掉(见 __init__ 里 self._checked 的说明)。
        checked = set(self._checked)
        self._all_case_paths = list(paths)   # 供 _checked_case_paths 使用(含折叠组)
        cur_item = self.case_list.currentItem()
        cur = cur_item.data(Qt.UserRole) if cur_item else None
        self.case_list.clear()
        last_group = object()
        for path in paths:
            group = groups.get(path) or "未分组"
            collapsed = group in self._collapsed_groups
            # ── 组头行(组变化时插入;不可选/不可勾/不可拖;点击折叠/展开) ──
            if group != last_group:
                head = QListWidgetItem(("▸ " if collapsed else "▾ ") + group)
                head.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                hf = head.font()
                hf.setBold(True)
                head.setFont(hf)
                head.setForeground(QColor("#0284c7"))
                head.setData(Qt.UserRole, None)          # 标记: 非用例行(组头)
                head.setData(CASE_STATE_ROLE, group)     # 组名存此供折叠切换
                head.setToolTip(f"{group} 的用例组,点击折叠/展开")
                self.case_list.addItem(head)
                last_group = group
            if collapsed:
                continue               # ★ 折叠组: 该组所有用例行都不填充
            name = os.path.splitext(os.path.basename(path))[0]
            # ★ item 完全原生:checkState 勾选 + text(拖拽快照的名称来源)。
            #   不要用 setItemWidget 做行内控件 —— 会盖 indicator/拦事件/断拖拽
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # ★ 先设路径再设勾选: setCheckState 会触发 itemChanged, 槽要靠 UserRole
            #   认出这是哪条用例(置反了槽就只能忽略)
            item.setData(Qt.UserRole, path)
            item.setCheckState(Qt.Checked if path in checked else Qt.Unchecked)
            item.setToolTip(f"{path} | 勾选参与批量执行;点右侧箭头或拖拽调整执行顺序")
            item.setSizeHint(QSize(0, 26))   # 略高于文字行,箭头绘制/命中更从容
            state = self._case_states.get(path, "idle")
            item.setData(CASE_STATE_ROLE, state)
            item.setIcon(lamp_icon(state))   # 灯在勾选框后、用例名前(icon 位,原生布局永不重叠)
            self.case_list.addItem(item)
        # ★ 空组目录(还没建任何用例)也显示组头,提示"暂无用例"(用户要求可见所有 APP 组)
        rendered = {g for g in groups.values()}
        for g in self._list_group_dirs():
            if g in rendered:
                continue
            collapsed_g = g in self._collapsed_groups
            head = QListWidgetItem(("▸ " if collapsed_g else "▾ ") + g)
            head.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            hf = head.font()
            hf.setBold(True)
            head.setFont(hf)
            head.setForeground(QColor("#0284c7"))
            head.setData(Qt.UserRole, None)
            head.setData(CASE_STATE_ROLE, g)
            head.setToolTip(f"{g} 的用例组,点击折叠/展开")
            self.case_list.addItem(head)
            if not collapsed_g:      # ★ 折叠的空组只显示组头, 不显示占位(用户实测 bug)
                hint = QListWidgetItem("　　(暂无用例)")   # 全角缩进: 显示在组头内部层级
                hint.setFlags(Qt.ItemIsEnabled)
                hint.setForeground(QColor("#94a3b8"))
                hint.setData(Qt.UserRole, None)
                self.case_list.addItem(hint)
        for i in range(self.case_list.count()):
            # cur=None(点击组头折叠)时不得匹配组头行(UserRole 同为 None),
            # 否则高亮跳到第一个组 —— 由调用方单独保持被点击组头的选中
            if cur is not None and self.case_list.item(i).data(Qt.UserRole) == cur:
                self.case_list.setCurrentRow(i)
                break

    def on_precondition_failed(self, detail):
        """前置检查未通过(本轮已阻断):弹窗提醒 + 用例灯重置灰(未真正执行)"""
        if self.worker:
            for fp in self.worker.case_files:
                self.set_case_state(fp, "idle")
        QMessageBox.warning(self, "前置检查未通过",
                            f"已阻断本轮执行,后续用例不再运行:\n{detail}\n\n"
                            "请处理设备状态(充电/网络/APP)后重新运行。")

    def _reset_case_lamps_to_running(self, paths):
        """点运行的瞬间:全部勾选用例灯变黄(重新执行时先覆盖上次的绿/红)"""
        for p in paths:
            self.set_case_state(p, "running")

    def on_delete_selected(self):
        """删除当前选中的行: 用例行 = 删除该用例文件;组头行 = 删除整组目录。
        均需确认;当前正在编辑的用例被删时同步清空编辑区"""
        item = self.case_list.currentItem()
        if item is None or self.worker:
            return
        path = item.data(Qt.UserRole)
        if not path:
            # ── 删除整组 ──
            group = item.data(CASE_STATE_ROLE)
            if not group:
                return
            gdir = os.path.join(CASES_DIR, group)
            if not os.path.isdir(gdir):
                return
            n = len([f for f in os.listdir(gdir)
                     if is_case_file(os.path.join(gdir, f))])
            if QMessageBox.question(
                    self, "删除 APP 组",
                    f"确定删除组「{group}」及其全部 {n} 条用例文件?\n此操作不可恢复!"
            ) != QMessageBox.Yes:
                return
            shutil.rmtree(gdir)
            self._collapsed_groups.discard(group)
            for p in list(self._case_states):
                if os.path.basename(os.path.dirname(p)) == group:
                    self._case_states.pop(p, None)
            if (self.case_path and os.path.basename(os.path.dirname(
                    os.path.abspath(self.case_path))) == group):
                self._unload_case()
            self._fill_case_list()
            self._set_status(f"已删除组: {group}")
            return
        # ── 删除单个用例 ──
        if not os.path.isfile(path):
            return
        name = os.path.splitext(os.path.basename(path))[0]
        if QMessageBox.question(
                self, "删除用例", f"确定删除用例「{name}」?\n此操作不可恢复!"
        ) != QMessageBox.Yes:
            return
        os.remove(path)
        self._case_states.pop(path, None)
        if self.case_path == path:
            self._unload_case()
        self._fill_case_list()
        self._set_status(f"已删除用例: {name}")

    def set_case_state(self, path, state):
        """设置用例执行状态灯:未执行置灰,执行中黄,通过绿,失败红。
        状态存在 item 的 CASE_STATE_ROLE 上,灯用 icon 位(勾选框后、名字前)"""
        self._case_states[path] = state or "idle"
        for i in range(self.case_list.count()):
            item = self.case_list.item(i)
            if item.data(Qt.UserRole) == path:
                state = self._case_states[path]
                item.setData(CASE_STATE_ROLE, state)
                item.setIcon(lamp_icon(state))
                break

    def _move_case(self, path, delta):
        """用例行上移/下移一行,并把新顺序 1..N 写回各用例 YAML 的 case_order"""
        if self.worker:
            return      # 运行中禁止排序: 它会把 case_order 重写进每个用例 YAML,
                        # 而 worker 是逐条 open() 读盘的 —— 时间窗内有概率读到半截文件
        paths = [self.case_list.item(i).data(Qt.UserRole)
                 for i in range(self.case_list.count())
                 if self.case_list.item(i).data(Qt.UserRole)]
        if path not in paths:
            return
        i = paths.index(path)
        j = i + delta
        if not (0 <= j < len(paths)):
            return
        paths[i], paths[j] = paths[j], paths[i]
        self._fill_case_list(order_paths=paths)
        self.case_list.setCurrentRow(j)
        self._persist_case_order(paths)

    def on_case_rows_dropped(self):
        """拖拽排序松手后:行控件已被 InternalMove 甩掉,重建并写回顺序"""
        if self.worker:
            self._fill_case_list()   # 运行中不允许改顺序: 把列表重建回原样
            return
        paths = [self.case_list.item(i).data(Qt.UserRole)
                 for i in range(self.case_list.count())
                 if self.case_list.item(i).data(Qt.UserRole)]
        if len(paths) < 2:
            return
        self._fill_case_list(order_paths=paths)
        self._persist_case_order(paths)

    def _persist_case_order(self, paths):
        """按给定顺序把 case_order=1..N 写回各用例 YAML"""
        try:
            for n, p in enumerate(paths, 1):
                self._write_case_order(p, n)
            self._set_status("已保存执行顺序到用例文件(case_order)")
            self.log_view.appendPlainText(
                "[排序] " + " → ".join(os.path.splitext(os.path.basename(p))[0]
                                       for p in paths))
        except Exception as e:
            QMessageBox.warning(self, "保存顺序失败", str(e))


    def _on_case_item_clicked(self, item):
        """单击用例行 → 右侧加载该用例编辑;单击组头行 → 折叠/展开该组(用户要求)"""
        if self.worker:
            return
        path = item.data(Qt.UserRole)
        if not path:
            group = item.data(CASE_STATE_ROLE)
            if group:
                if group in self._collapsed_groups:
                    self._collapsed_groups.discard(group)
                else:
                    self._collapsed_groups.add(group)
                    # ★ 收起组时,若当前编辑的用例属于该组 → 详情恢复未选中
                    #   (用例行已隐藏,继续显示其步骤会误导)
                    if (self.case_path and
                            os.path.basename(os.path.dirname(os.path.abspath(self.case_path))) == group):
                        self._unload_case()
                self._save_expanded_groups()   # 记住展开/收起(下次打开保持原状)
                self._fill_case_list()
                # ★ 选中框保持在被点击的组头上(否则重建后 cur=None 会错误
                #   匹配到第一个组头, 高亮跳到别的 APP 组 —— 用户实测)
                for i in range(self.case_list.count()):
                    it = self.case_list.item(i)
                    if (it.data(Qt.UserRole) is None
                            and it.data(CASE_STATE_ROLE) == group):
                        self.case_list.setCurrentRow(i)
                        break
            return
        if not os.path.isfile(path) or path == self.case_path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            QMessageBox.critical(self, "打开失败", f"YAML 解析失败:\n{e}")
            return
        if not isinstance(data, dict) or not data.get("cases"):
            QMessageBox.warning(self, "格式不符", "文件需包含 module 与 cases 字段")
            return
        self.case_path = path
        self.data = data
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText(_safe_relpath(path))
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()
        self._fill_case_list()   # 打开的用例必须在左侧列表可见

    def _target_group_for_bulk_check(self):
        """批量勾选(全选)的作用组: 优先"当前所选", 判不出来返回 None(=全部)。

        优先级(用户要求: **优先服务于所选的 APP 组**, 不要一次把各组全勾上):
          1. 列表里当前选中的行 —— 选中组头 → 该组; 选中用例行 → 它所在组
          2. 否则看正在编辑的用例属于哪组
          3. 都没有(空列表/未选用例) → None, 退回"全部"
        """
        cur = self.case_list.currentItem()
        if cur is not None:
            path = cur.data(Qt.UserRole)
            if path:
                return os.path.basename(os.path.dirname(os.path.abspath(path)))
            group = cur.data(CASE_STATE_ROLE)     # 组头行: 组名存在这里
            if group:
                return group
        return self._current_app_group() or None

    def _set_cases_checked(self, state, scope_group=None):
        """勾选/取消勾选用例行。

        scope_group: 只作用于该 APP 组(None=全部)。全选按钮传当前所选组 ——
        用户要求"优先服务于所选的 APP 组, 不要全部生效"。
        """
        def _group_of(p):
            return os.path.basename(os.path.dirname(os.path.abspath(p)))

        # ★ 以模型层为准更新 —— 必须覆盖**折叠组**的用例(它们的行根本不存在,
        #   只存在 self._checked 里)。原先只遍历可见行, 于是折叠的组既全选不上、
        #   也清空不掉。
        hit = 0
        for path in self._all_case_paths or []:
            if scope_group and _group_of(path) != scope_group:
                continue
            if state == Qt.Checked:
                self._checked.add(path)
            else:
                self._checked.discard(path)
            hit += 1
        # 反射到可见行(折叠组的行不存在, 靠 self._checked 在下次填充列表时恢复)
        for i in range(self.case_list.count()):
            item = self.case_list.item(i)
            p = item.data(Qt.UserRole)
            if p:
                item.setCheckState(Qt.Checked if p in self._checked else Qt.Unchecked)
        return hit

    def _checked_case_paths(self):
        """勾选的用例文件路径列表(勾选才参与批量执行;按列表顺序=执行顺序)

        ★ 以模型层 `self._checked` 为准, 并**包含折叠组**里已勾选的用例 ——
          折叠只是视图整理, 不该改变"将要执行什么"。原先遍历可见行, 折叠组的勾选
          既看不见也不执行(用户实测: 收起组后点运行, 那些用例被静默跳过)。
        """
        out, seen = [], set()
        for path in self._all_case_paths:          # 顺序 = 列表顺序(含折叠组在组内位置)
            if path in self._checked and path not in seen:
                out.append(path)
                seen.add(path)
        return out

    def _on_case_check_changed(self, item):
        """用户勾选/取消 → 同步到模型层(唯一真源)"""
        path = item.data(Qt.UserRole)
        if not path:                    # 组头行/占位行
            return
        if item.checkState() == Qt.Checked:
            self._checked.add(path)
        else:
            self._checked.discard(path)

    def on_select_all_cases(self):
        """「全选」: **优先只勾选当前所选 APP 组**(用户要求), 并在状态栏说明范围"""
        group = self._target_group_for_bulk_check()
        n = self._set_cases_checked(Qt.Checked, scope_group=group)
        if group:
            self._set_status(f"已全选「{group}」的 {n} 条用例", self.status_label)
        else:
            self._set_status(f"已全选全部 {n} 条用例(未选中具体组)", self.status_label)

    def on_clear_cases(self):
        """「清空」: 与「全选」**同一范围语义** —— 优先只作用于当前所选 APP 组。

        ★ 用户要求(2026-10-09): 清空也按组限定。原先是无差别清掉所有组的勾选 ——
          想清掉某一组时会把别的组辛苦勾好的也一起清掉, 与"全选优先服务本组"
          的语义也不对称。
        """
        group = self._target_group_for_bulk_check()
        n = self._set_cases_checked(Qt.Unchecked, scope_group=group)
        if group:
            self._set_status(f"已清空「{group}」的 {n} 条勾选", self.status_label)
        else:
            self._set_status(f"已清空全部 {n} 条勾选(未选中具体组)", self.status_label)

    def _current_device_id(self):
        return self.device_combo.currentData()

    # ── APP/设备配置 ──
    def _save_env_field(self):
        """测试APP/包名/启动页/设备名称 编辑 → 写回 config.yaml"""
        self._save_cfg_field(self.sender())

    def _save_cfg_field(self, w):
        if w is None:
            return
        key = w.property("cfg_key")
        value = w.text().strip()
        if not key or not value:
            return
        try:
            update_config({key: value})
            self._set_status(f"配置已保存: {key} = {value}")
        except Exception as e:
            QMessageBox.critical(self, "保存配置失败", str(e))

    def on_detect_app(self):
        """按 APP 名称在设备已装应用中匹配包名,并解析启动页"""
        app_name = self.app_name_edit.currentText().strip()
        if not app_name:
            QMessageBox.warning(self, "提示", "请先填写被测APP名称")
            return
        device_id = self._current_device_id()
        if not device_id:
            QMessageBox.warning(self, "提示", "未检测到在线设备,无法检测包名")
            return
        # ★ 不用全局转圈光标: 模态弹窗(选择/告警)叠加 override 栈不平衡时
        #   会残留转圈(用户实测: APP 未安装时鼠标一直转); 改为按钮禁用+状态提示
        self._set_status("检测中...")
        self.detect_btn.setEnabled(False)
        self.detect_btn.setText("检测中…")
        try:
            packages = app_detect.list_packages(device_id)
            matched = app_detect.match_packages(app_name, packages)
            if matched and (len(matched) == 1 or matched[0][1] > matched[1][1]):
                package = matched[0][0]
            elif matched:
                package = self._pick_app(
                    "匹配到多个应用,请选择:", [p for p, _ in matched[:8]])
            else:
                # 名称与包名对不上(如 SmartThings → com.samsung.android.oneconnect):
                # 列出已装应用让人直接选,别把路堵死
                package = self._pick_app(
                    f"按名称「{app_name}」未匹配到已装应用。\n"
                    "请从设备已装应用中选择(或在 common/app_detect.py 的 "
                    "_APP_ALIASES 里登记别名):",
                    packages)
            if package is None:
                self.env_status.setText("")
                self._purge_name_history("hist/app_name", app_name)   # 未检测到 → 不记历史
                return
            activity = app_detect.detect_main_activity(device_id, package) or ""
            update_config({"app.package": package, "app.main_activity": activity})
            # ★ 检测成功才记历史(用户要求), 并刷新下拉
            self._push_name_history("hist/app_name", app_name)
            self._reload_name_combo(self.app_name_edit)
            self._set_status(
                f"检测结果: {package} → {activity or '启动页未识别'}(已写入配置)")
        except Exception as e:
            QMessageBox.critical(self, "检测失败", f"{type(e).__name__}: {e}")
            self.env_status.setText("")
            self._purge_name_history("hist/app_name", app_name)       # 检测失败 → 不记历史
        finally:
            self.detect_btn.setEnabled(True)
            self.detect_btn.setText("🔍 检测")
            # 兜底: 清空可能残留的全局光标覆盖栈(任何路径都不留转圈)
            while QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def _pick_app(self, prompt, options):
        """弹选择框,返回选中的包名;取消或无候选返回 None"""
        if not options:
            QMessageBox.warning(self, "未找到应用", "设备上没有可选的第三方应用。")
            return None
        from PySide6.QtWidgets import QInputDialog
        sel, ok = QInputDialog.getItem(self, "选择APP", prompt, options, 0, False)
        return sel if ok else None

    # ── 前置条件(可编辑/可新增) ──

    def _load_preconditions(self, app_group=None):
        """当前 APP 组的前置项; 同时记下来源(供菜单显示)。

        ★ 用户要求(2026-09-24): 前置条件与 APP 组绑定 —— 切到某组用例就用那组的。
          优先级(见 core.session.preconditions_for_group):
            组目录 preconditions.yaml → config.yaml 全局 → 内置默认
        """
        from core.session import preconditions_for_group
        group = self._current_app_group() if app_group is None else app_group
        items, source = preconditions_for_group(group)
        if source == "default":
            # 全新环境: 把默认项落到 config(保持旧版行为, 用户能直接编辑)
            try:
                from core.driver import save_preconditions
                save_preconditions(items)
            except Exception:
                pass
        self._pre_source, self._pre_group = source, group
        return items

    def _refresh_preconditions_for_case(self):
        """切换用例后让前置条件跟随该用例的 APP 组(用户要求)"""
        if not hasattr(self, "pre_menu"):
            return
        group = self._current_app_group()
        if group == getattr(self, "_pre_group", None):
            return                      # 同组不重载, 免得覆盖还没保存的勾选改动
        self.preconditions = self._load_preconditions(group)
        self._build_pre_menu()

    def _save_preconditions(self):
        """保存前置条件: 有当前组 → 写组目录; 没选用例 → 写 config.yaml"""
        group = self._current_app_group()
        try:
            if group:
                from core.driver import save_group_preconditions
                save_group_preconditions(group, self.preconditions)
            else:
                from core.driver import save_preconditions
                save_preconditions(self.preconditions)
            return True
        except Exception as e:
            QMessageBox.critical(self, "保存失败", str(e))
            return False

    def _current_app_group(self):
        """当前 APP 组 = 正在编辑的用例所在目录名(供模板下拉用)"""
        if not self.case_path:
            return ""
        return os.path.basename(os.path.dirname(os.path.abspath(self.case_path)))

    def _pre_label(self, item):
        from core.session import item_label
        return item_label(item)

    def _build_pre_menu(self):
        """按当前前置项列表重建菜单(勾选=启用; 底部「设置…」增删改)

        ★ 菜单顶部显示"这些前置条件属于哪个 APP 组 / 来源", 避免误以为全局共用。
        """
        self.pre_menu.clear()
        src = {"group": "本组配置", "global": "全局兜底(config.yaml)",
               "default": "默认项"}.get(getattr(self, "_pre_source", ""), "")
        group = self._current_app_group() or "(未选用例)"
        head = self.pre_menu.addAction(f"当前组: {group} · {src}")
        head.setEnabled(False)
        self.pre_menu.addSeparator()
        self.pre_actions = {}
        for i, item in enumerate(self.preconditions):
            a = QAction(self._pre_label(item), self.pre_menu)
            a.setCheckable(True)
            a.setChecked(bool(item.get("enabled", True)))
            a.setToolTip("勾选=执行该前置条件; 需要改参数或增删请点下方「设置前置条件…」")
            a.toggled.connect(lambda on, k=i: self._toggle_precondition(k, on))
            self.pre_menu.addAction(a)
            self.pre_actions[i] = a
        self.pre_menu.addSeparator()
        self.pre_menu.addAction("设置前置条件…", self.on_edit_preconditions)

    def _toggle_precondition(self, idx, on):
        """勾选=启用; 立即落盘(按组存), 免得切组/重启后勾选状态丢失"""
        if self.worker:
            return      # 运行中不让改前置: 本轮用的是启动时导出的那份, 改了只会让
                        # 界面显示与实际执行对不上
        if 0 <= idx < len(self.preconditions):
            self.preconditions[idx]["enabled"] = bool(on)
            self._save_preconditions()

    def on_precondition_retry(self, detail):
        """前置检测失败 → 暂停等待用户处理(用户 2026-10-15): 在设备上排除故障后
        点「重试前置」从失败项继续; 点「取消运行」结束本次执行。
        ★ 弹窗期间执行线程阻塞在应答事件上(设备保持现状, 即"暂停排除故障")。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("前置检测未通过")
        box.setText("前置检测有失败项, 执行已暂停")
        box.setInformativeText(
            f"{detail}\n\n"
            "请先在设备/APP 上排除故障(保持当前界面), 然后选择:")
        retry = box.addButton("重试前置", QMessageBox.AcceptRole)
        box.addButton("取消运行", QMessageBox.RejectRole)
        box.setDefaultButton(retry)
        box.exec()                     # 模态: 居中于主窗口并置顶
        if self.worker:
            self.worker.resolve_precondition_retry(box.clickedButton() is retry)

    def preview_compare_area(self, region_text):
        """预览对比范围(用户 2026-10-14): 连当前设备截屏 → 按范围画标注图 → 弹窗。
        范围非法/设备未连接都转警告框, 不打断编辑。"""
        from core.actions.asserts import preview_region
        try:
            if not str(region_text or "").strip():
                QMessageBox.warning(
                    self, "预览对比范围",
                    "对比范围是空的 —— 会用默认中部区域\n"
                    "(10%,30%-90%,80%); 填上后可预览自定义范围")
                region_text = ""
            import uiautomator2 as u2
            dev_id = self.device_combo.currentData()
            d = u2.connect(dev_id)
            frame = d.screenshot(format="opencv")
            path, desc = preview_region(frame, region_text)
        except ValueError as e:
            QMessageBox.warning(self, "对比范围有误", str(e))
            return
        except Exception as e:
            QMessageBox.warning(self, "预览失败",
                                f"{type(e).__name__}: {e}\n请确认设备已连接")
            return
        dlg = ImageViewDialog(path, parent=self)
        dlg.setWindowTitle(f"对比范围预览 - {desc}")
        dlg.exec()

    def on_edit_status_words(self):
        """故障/空闲词配置弹窗(用户 2026-10-13): 写 config.yaml + 同步全局词表,
        当轮后续步骤立即用上新词表(不重启)。"""
        fault, idle, fault_n, idle_n = self._load_status_words_from_config()
        dlg = StatusWordsDialog(fault, idle,
                                fault_streak=fault_n, idle_streak=idle_n,
                                parent=self)
        if dlg.exec():
            log.info(f"[故障检测] 已更新(运行时即时生效): "
                     f"故障={dlg.fault_texts!r}(连续{dlg.fault_streak_value}次), "
                     f"空闲={dlg.idle_texts!r}(连续{dlg.idle_streak_value}次)")

    def _load_status_words_from_config(self):
        """从 config.yaml 读 runner.fault_texts / runner.idle_texts / *streak, 同步到全局。
        返回当前值便于弹窗预填。无 config/无键 → 用内置默认。"""
        from core import driver, session
        try:
            cfg = driver.load_config() or {}
        except Exception:
            cfg = {}
        r = cfg.get("runner", {}) if isinstance(cfg, dict) else {}
        fault = str(r.get("fault_texts") or "")
        idle = str(r.get("idle_texts") or "")
        # 读 streak: 非法值/_clamp_streak 兜底(空字符串/字母/0/-1 都会回默认 3)
        fault_n = session._clamp_streak(r.get("fault_streak"))
        idle_n = session._clamp_streak(r.get("idle_streak"))
        try:
            session.set_status_words(fault, idle,
                                     fault_streak=fault_n, idle_streak=idle_n)
        except Exception:
            pass
        return fault, idle, fault_n, idle_n
        return fault, idle

    def on_edit_preconditions(self):
        """打开设置对话框: 增删改/排序前置条件, 保存到当前 APP 组"""
        if self.worker:
            return      # 运行中不让改前置(本轮已用启动时导出的那份)。另外对话框里的
                        # 步骤编辑器会改 vision 的模块级模板上下文, 运行中打开会把
                        # 正在跑的用例的模板解析指向别的 APP 组。
        dlg = PreconditionsDialog(self.preconditions, self,
                                  app_group=self._current_app_group())
        if dlg.exec() != QDialog.Accepted:
            return
        self.preconditions = dlg.values()
        if self._save_preconditions():
            group = self._current_app_group()
            self._pre_source = "group" if group else "global"
            self._set_status(f"前置条件已保存({group or '全局'})", self.status_label)
        self._build_pre_menu()

    def _selected_preconditions(self):
        """当前启用中的前置项(执行时传给 RunWorker)"""
        return [dict(x) for x in self.preconditions if x.get("enabled", True)]

    def _pre_items_by_group(self, case_files):
        """执行前: 按要跑的用例收集每个 APP 组的前置项(用户要求: 前置随组)。

        当前编辑的组直接用界面上这份(可能刚勾/去勾过); 其它组从磁盘读该组配置。
        """
        from core.session import preconditions_for_group
        cur = self._current_app_group()
        out = {}
        for fp in case_files:
            group = os.path.basename(os.path.dirname(os.path.abspath(fp)))
            if group in out:
                continue
            items = self.preconditions if group == cur else preconditions_for_group(group)[0]
            out[group] = [dict(x) for x in items if x.get("enabled", True)]
        return out

    def _build_chip_strip(self):
        """测试步骤详情区标题 + 用例组/用例/优先级/步骤间隔/添加步骤(流式布局)"""
        strip = QFrame()
        strip.setObjectName("chipStrip")
        lay = FlowLayout(strip, margin=5, spacing=6)

        # ★ 区块标题: 测试步骤详情(用户要求;之后依次是 用例组/用例/优先级/间隔/添加步骤)
        title = QLabel("测试步骤详情")
        title.setStyleSheet("color:#2563eb; font-weight:bold; font-size:13px;")
        lay.addWidget(title)
        lay.addWidget(QLabel("用例组"))
        self.module_edit = QLineEdit()
        self.module_edit.setFixedWidth(96)
        self.module_edit.setToolTip("用例组名,也是 Excel 报告的 sheet 名")
        self.module_edit.editingFinished.connect(self._sync_header)
        lay.addWidget(self.module_edit)
        lay.addWidget(QLabel("用例"))
        self.case_name_edit = QLineEdit()
        self.case_name_edit.setObjectName("caseName")
        self.case_name_edit.setFixedWidth(120)
        self.case_name_edit.editingFinished.connect(self._sync_header)
        lay.addWidget(self.case_name_edit)

        # 多 case 文件的用例切换器(单 case 文件自动隐藏)
        self.case_combo = QComboBox()
        self.case_combo.setToolTip("该文件包含多个用例,选择当前编辑的用例")
        self.case_combo.currentIndexChanged.connect(self._on_case_switched)
        self.case_combo.setVisible(False)
        lay.addWidget(self.case_combo)
        lay.addWidget(QLabel("优先级"))
        self.priority_combo = QComboBox()
        self.priority_combo.addItems(["P0", "P1", "P2"])
        self.priority_combo.setFixedWidth(64)
        self.priority_combo.currentTextChanged.connect(self._sync_header)
        lay.addWidget(self.priority_combo)
        lay.addWidget(QLabel("间隔s"))
        self.case_wait_edit = QLineEdit()
        self.case_wait_edit.setFixedWidth(46)
        self.case_wait_edit.setValidator(QIntValidator(0, 9999))
        self.case_wait_edit.editingFinished.connect(self._sync_header)
        lay.addWidget(self.case_wait_edit)
        # ★ 「＋添加步骤」排在间隔之后(用户要求的字段顺序)
        self.add_btn = QPushButton("＋ 添加步骤")
        self.add_btn.setObjectName("chipBtn")
        self.add_btn.setToolTip("向当前用例添加步骤")
        self.add_btn.setMenu(make_action_menu(self.add_btn, self.add_step))
        lay.addWidget(self.add_btn)

        line = QLabel("|")
        line.setStyleSheet("color:#e2e8f0;")
        lay.addWidget(line)

        self._quick_btns = []
        for key in QUICK_ACTIONS:
            if key == "sleep":
                label, cat = "延时", "时间"     # 快捷条上用短名
            else:
                a = schema.ACTION_BY_KEY[key]
                label, cat = a["label"], a["category"]
            b = QPushButton(label)
            b.setObjectName("chipBtn")
            b.setToolTip(f"添加「{label}」步骤(分类: {cat})")
            b.clicked.connect(lambda _, k=key: self.add_step(k))
            lay.addWidget(b)
            self._quick_btns.append(b)
        tip = QLabel("更多动作见右上「＋添加步骤」;点卡片展开编辑参数")
        tip.setStyleSheet("color:#b6c0cd;")
        lay.addWidget(tip)
        self.step_count_label = QLabel("")
        self.step_count_label.setStyleSheet("color:#2563eb; font-weight:bold;")
        lay.addWidget(self.step_count_label)
        collapse_btn = QPushButton("收起全部")
        collapse_btn.setObjectName("chipBtn")
        collapse_btn.setToolTip("折叠全部展开中的步骤卡片")
        collapse_btn.clicked.connect(self._collapse_all)
        lay.addWidget(collapse_btn)
        return strip

    def _collapse_all(self):
        self.expanded_key = None
        self.render_cards()

    # ── 步骤卡片列表 ──
    def _make_cards_area(self):
        self.cards_scroll = QScrollArea()
        self.cards_scroll.setWidgetResizable(True)
        host = QWidget()
        self.cards_lay = QVBoxLayout(host)
        self.cards_lay.setContentsMargins(2, 2, 6, 2)
        self.cards_lay.setSpacing(6)
        self.cards_lay.addStretch()
        self.cards_scroll.setWidget(host)
        return self.cards_scroll

    def render_cards(self):
        while self.cards_lay.count() > 1:  # 末尾 stretch 保留
            item = self.cards_lay.takeAt(0)
            if item.widget():
                item.widget().hide()  # 先隐藏再延迟销毁,杜绝重渲染瞬间残留
                item.widget().deleteLater()
        for i in range(len(self.steps)):
            step = self.steps[i]
            self.cards_lay.insertWidget(self.cards_lay.count() - 1, StepCard(self, i))
            # 父卡片(或其子步骤)展开时, then/else 两个分支的子步骤都以缩进子卡片紧随其后
            ek = self.expanded_key
            if ek and ek[0] == i:
                for branch in ("then", "else"):
                    subs = step.get(branch)
                    if not isinstance(subs, list):
                        continue
                    for j in range(len(subs)):
                        self.cards_lay.insertWidget(
                            self.cards_lay.count() - 1,
                            StepCard(self, i, parent_index=i, sub_index=j, branch=branch))
        self.step_count_label.setText(f"共 {len(self.steps)} 步")
        self._refresh_yaml_text()

    def expand_card(self, key):
        # 收起焦点控件,触发 editingFinished 把未提交的编辑写回步骤
        focused = self.focusWidget()
        if focused is not None:
            focused.clearFocus()
        self.expanded_key = key
        self.render_cards()

    def _auto_save(self):
        """编辑自动保存(防抖 300ms): 停止编辑后写盘一次,
        避免勾选切换时的连续写盘抖动;「保存」按钮为手动强制保存"""
        if self.worker or not self.case_path:
            return
        if getattr(self, "_autosave_timer", None) is None:
            from PySide6.QtCore import QTimer as _QTimer
            self._autosave_timer = _QTimer(self)
            self._autosave_timer.setSingleShot(True)
            self._autosave_timer.setInterval(300)
            self._autosave_timer.timeout.connect(self._do_auto_save)
        self._autosave_timer.start()

    def _do_auto_save(self):
        if self.worker or not self.case_path:
            return
        if getattr(self, "_edit_timer", None) is not None and self._edit_timer.isActive():
            self._edit_timer.stop()
            self._refresh_yaml_text()   # 写盘前保证 YAML 视图一致
        try:
            prev_order = self._read_case_order(self.case_path)
            with open(self.case_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
            if prev_order is not None:
                self._write_case_order(self.case_path, prev_order)
            self._dirty = False
            self.status_label.setText(
                f"已自动保存 {os.path.basename(self.case_path)} "
                f"{time.strftime('%H:%M:%S')}")
        except Exception as e:
            self.log_view.appendPlainText(f"[自动保存失败] {e}")

    def on_card_edited(self):
        """卡片编辑 → 防抖刷新 YAML 文本 + 自动保存(勾选连续切换只触发一次,
        避免每次 toggle 都全量重绘导致勾选框闪动)"""
        if getattr(self, "_edit_timer", None) is None:
            self._edit_timer = QTimer(self)
            self._edit_timer.setSingleShot(True)
            self._edit_timer.setInterval(300)
            self._edit_timer.timeout.connect(self._flush_card_edit)
        self._edit_timer.start()

    def _flush_card_edit(self):
        self._refresh_yaml_text()
        self._auto_save()

    # ── 步骤操作 ──
    def add_step(self, key):
        if self.worker:
            return
        step = schema.new_step(key)
        self.steps.append(step)
        self.expanded_key = (len(self.steps) - 1,)
        self.render_cards()
        self._auto_save()
        self._scroll_to_end()

    def _scroll_to_end(self):
        bar = self.cards_scroll.verticalScrollBar()
        QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))

    def move_step(self, index, delta):
        if self.worker:
            return
        new = index + delta
        if not (0 <= new < len(self.steps)):
            return
        self.steps[index], self.steps[new] = self.steps[new], self.steps[index]
        if self.expanded_key == (index,):
            self.expanded_key = (new,)
        self.render_cards()
        self._auto_save()

    def dup_step(self, index):
        if self.worker:
            return
        self.steps.insert(index + 1, copy.deepcopy(self.steps[index]))
        self.expanded_key = (index + 1,)
        self.render_cards()
        self._auto_save()

    def del_step(self, index):
        if self.worker:
            return
        del self.steps[index]
        if self.expanded_key and self.expanded_key[0] >= len(self.steps):
            self.expanded_key = (len(self.steps) - 1,) if self.steps else None
        self.render_cards()
        self._auto_save()

    # ── then / else 子步骤操作(2026-10-11: then 原来没有子步骤, 只能靠"取反条件+else"绕) ──
    def _sub_list(self, parent_index, branch="else"):
        return self.steps[parent_index].setdefault(branch, [])

    def add_sub(self, parent_index, action_key, branch="else"):
        if self.worker:
            return
        step = schema.new_step(action_key)
        subs = self._sub_list(parent_index, branch)
        subs.append(step)
        self.expanded_key = (parent_index, branch, len(subs) - 1)
        self.render_cards()
        self._auto_save()
        self._scroll_to_end()

    def move_sub(self, parent_index, sub_index, delta, branch="else"):
        if self.worker:
            return
        subs = self.steps[parent_index].get(branch) or []
        new = sub_index + delta
        if not (0 <= new < len(subs)):
            return
        subs[sub_index], subs[new] = subs[new], subs[sub_index]
        if self.expanded_key == (parent_index, branch, sub_index):
            self.expanded_key = (parent_index, branch, new)
        self.render_cards()
        self._auto_save()

    def dup_sub(self, parent_index, sub_index, branch="else"):
        if self.worker:
            return
        subs = self._sub_list(parent_index, branch)
        subs.insert(sub_index + 1, copy.deepcopy(subs[sub_index]))
        self.expanded_key = (parent_index, branch, sub_index + 1)
        self.render_cards()
        self._auto_save()

    def del_sub(self, parent_index, sub_index, branch="else"):
        if self.worker:
            return
        subs = self.steps[parent_index].get(branch) or []
        if 0 <= sub_index < len(subs):
            del subs[sub_index]
        if self.expanded_key and len(self.expanded_key) >= 2 and self.expanded_key[0] == parent_index:
            self.expanded_key = (parent_index,)  # 收回到父卡片
        self.render_cards()
        self._auto_save()

    # ── 数据模型 ──
    def _empty_data(self):
        return {"module": "新用例组", "cases": [{"name": "新用例", "priority": "P1", "steps": []}]}

    @property
    def current_case(self):
        cases = self.data.setdefault("cases", [])
        if not cases:
            cases.append({"name": "新用例", "priority": "P1", "steps": []})
        self.case_idx = min(self.case_idx, len(cases) - 1)
        return cases[self.case_idx]

    @property
    def steps(self):
        return self.current_case.setdefault("steps", [])

    def _load_case_into_ui(self):
        # ★ 模板上下文跟随当前用例的 APP 组(点击模板下拉自动列出该组模板)
        if self.case_path:
            from core import vision as _vision
            _vision.set_template_app_group(
                os.path.basename(os.path.dirname(os.path.abspath(self.case_path))))
        # ★ 前置条件也跟随 APP 组(用户要求: 切到某组用例就用那组的前置条件)
        self._refresh_preconditions_for_case()
        c = self.current_case
        # ★ 装填期间必须屏蔽 _sync_header —— 它把控件的**当前值**回写进 self.data,
        #   而此刻控件里还是上一个用例的残留文本。四个控件里 module/用例名/间隔用
        #   setText(不发 editingFinished), 但 priority_combo.setCurrentIndex 在优先级
        #   不同时会**立刻发 currentTextChanged** → _sync_header 读到上一个用例的"间隔"
        #   文本, 把刚加载用例的 wait 覆盖掉。
        #   实测: 用例甲 P1/wait=5 → 切到用例乙 P0/wait=3, 乙的 wait 变成 5, 而且间隔框
        #   随后也显示 5(用户完全看不出异常); 300ms 后 _autosave_timer 直接写进磁盘。
        self._loading = True
        try:
            self.module_edit.setText(str(self.data.get("module") or ""))
            self.case_name_edit.setText(str(c.get("name") or ""))
            pri = str(c.get("priority") or "P1")
            self.priority_combo.setCurrentIndex(max(0, self.priority_combo.findText(pri)))
            self.case_wait_edit.setText("" if c.get("wait") is None else str(c["wait"]))
        finally:
            self._loading = False
        # 这两步原本是 _sync_header 的副作用, 屏蔽之后要显式补上, 否则切用例后
        # YAML 视图不刷新(回归)
        self._refresh_case_item_text()
        self._refresh_yaml_text()

    def _sync_header(self):
        if getattr(self, "_loading", False):
            # 正在把数据装填进控件, 此时控件里还是上一个用例的残留文本 ——
            # 回写会把刚加载的数据覆盖掉(见 _load_case_into_ui 的说明)
            return
        self.data["module"] = self.module_edit.text().strip() or "未命名"
        c = self.current_case
        c["name"] = self.case_name_edit.text().strip() or "未命名用例"
        c["priority"] = self.priority_combo.currentText()
        txt = self.case_wait_edit.text().strip()
        c["wait"] = int(txt) if txt.isdigit() else None
        self._refresh_case_item_text()
        self._refresh_yaml_text()

    # ── 多 case 切换器 ──
    def _refresh_case_selector(self):
        cases = self.data.get("cases", [])
        self.case_combo.blockSignals(True)
        self.case_combo.clear()
        for i, c in enumerate(cases):
            self.case_combo.addItem(f"{i + 1}. {c.get('name', '未命名')}")
        self.case_combo.setCurrentIndex(self.case_idx)
        self.case_combo.blockSignals(False)
        self.case_combo.setVisible(len(cases) > 1)

    def _refresh_case_item_text(self):
        if self.case_combo.count() > self.case_idx:
            self.case_combo.blockSignals(True)
            self.case_combo.setItemText(
                self.case_idx, f"{self.case_idx + 1}. {self.current_case.get('name', '未命名')}")
            self.case_combo.blockSignals(False)

    def _on_case_switched(self, index):
        if index < 0 or index == self.case_idx:
            return
        self.case_idx = index
        self.expanded_key = None
        self._load_case_into_ui()
        self.render_cards()

    # ── 执行期间锁定编排区 ──
    def _set_locked(self, locked):
        self._locked = locked
        widgets = [self.new_btn, self.open_btn, self.save_btn, self.add_btn,
                   self.case_name_edit, self.module_edit, self.priority_combo,
                   self.case_wait_edit, self.case_combo,
                   self.yaml_refresh_btn, self.yaml_apply_btn,
                   # 用例执行期间「检查更新」置灰: 变了也没法立刻重启安装,
                   # 点它只会得到一个"请等本轮跑完"的提示 —— 不如直接禁用
                   self.update_btn]
        widgets += self._quick_btns
        for w in widgets:
            w.setEnabled(not locked)
        self.cards_scroll.widget().setEnabled(not locked)  # 卡片区只读(仍可滚动)

    def _find_action(self, step):
        # ★ 用 schema.display_action_key(先问引擎的判据, 再按 schema 兜底): 卡片显示的动作必须
        #   就是会被执行的那个 —— 原来按 dict 键序取, {assert, click} 会显示"断言"却
        #   执行"点击"(2026-10-11 实测)。
        key = schema.display_action_key(step)
        return schema.ACTION_BY_KEY.get(key) if key else None

    # ── 底部标签页 ──
    def _make_bottom(self):
        splitter = QSplitter(Qt.Vertical)
        self.tabs = QTabWidget()

        # 执行结果
        result_split = QSplitter(Qt.Horizontal)
        # ★ 列里要有"用例名(第几次执行)"(用户要求 2026-10-12): 同一个用例重复跑多轮时,
        #   以前结果表里看不出现在/这条属于第几轮。
        self.result_table = QTableWidget(0, 6)
        self.result_table.setHorizontalHeaderLabels(
            ["#", "用例(第几次)", "步骤", "结果", "耗时", "错误信息"])
        # ★ 步骤/错误信息两列拉伸占满;结果列随内容自适应(中文总结行放得下);
        #   此前 Stretch 挂在结果列上,列序调整后步骤/错误信息被挤窄显示不全
        self.result_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.result_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.result_table.setColumnWidth(0, 40)
        self.result_table.setColumnWidth(1, 150)
        self.result_table.setColumnWidth(4, 64)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.result_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        # ★ 步骤/错误信息**自动换行**(用户 2026-10-15: "一行显示不全") —— 长步骤名
        #   与多行错误在列宽内折行, 行高随内容(resizeRowsToContents 在每次填行后调)
        self.result_table.setWordWrap(True)
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.cellDoubleClicked.connect(self.on_result_row)
        self.result_table.itemSelectionChanged.connect(self._on_result_selection)
        result_split.addWidget(self.result_table)
        self.preview = ClickableLabel("单击结果行显示对应截图\n点击图片可放大查看")
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumWidth(240)
        # ★ Ignored:pixmap 不参与 sizeHint —— 否则每次点击行,预览图按上次的
        #   放大尺寸再缩放一轮,splitter 越撑越大(正反馈,用户实测)
        self.preview.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.preview.clicked.connect(self._zoom_preview)
        self.preview.setCursor(Qt.PointingHandCursor)
        result_split.addWidget(self.preview)
        result_split.setSizes([620, 300])
        self.tabs.addTab(result_split, "执行结果")

        # YAML 源码
        src = QWidget()
        sv = QVBoxLayout(src)
        self.yaml_edit = QPlainTextEdit()
        sv.addWidget(self.yaml_edit)
        srow = QHBoxLayout()
        for text, fn, attr in [("← 从卡片刷新", self._refresh_yaml_text, "yaml_refresh_btn"),
                               ("应用到卡片 →", self._apply_yaml_text, "yaml_apply_btn")]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            setattr(self, attr, b)
            srow.addWidget(b)
        srow.addStretch()
        sv.addLayout(srow)
        self.tabs.addTab(src, "YAML 源码")

        # 运行日志
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        # ★ 自动换行: 默认 NoWrap 会把长日志行在右侧截断(用户反馈"显示不完整")
        self.log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_view.setMaximumBlockCount(5000)   # 限制条数, 防止长时间运行涨爆内存
        self.tabs.addTab(self.log_view, "运行日志")

        splitter.addWidget(self.tabs)
        splitter.setSizes([300])
        return splitter

    # ── 文件操作 ──
    def on_new(self):
        """新建用例:弹名称输入框 → 在 Test_cases/ 创建空白用例文件 →
        列表刷新并选中新用例,右侧进入编辑(后续编辑自动保存)"""
        if self.worker:
            return
        groups = sorted(set(self._list_group_dirs()))
        # ★ 默认组 = 当前编辑用例所在的 APP 组(顺着当前 APP 新建);
        #   否则按名称取第一个组 —— 曾因此把新用例默认建进「测试」组(用户实测)
        default_group = None
        if self.case_path:
            g = os.path.basename(os.path.dirname(os.path.abspath(self.case_path)))
            default_group = g if g in groups else None
        if default_group is None:
            default_group = next((g for g in groups
                                  if g == (load_config().get("app") or {}).get("name")), None)
        if default_group is None and groups:
            default_group = groups[0]
        dlg = NewCaseDialog(groups, self, default_group=default_group)
        if dlg.exec() != QDialog.Accepted:
            return
        name, group = dlg.values()
        if not name:
            QMessageBox.warning(self, "新建用例", "用例名称不能为空")
            return
        self.create_case(name, group=group)

    def _unload_case(self):
        """清空右侧编辑区,恢复未选中状态(收起包含当前用例的组时调用)"""
        self.case_path = None
        self.data = self._empty_data()
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText("未选中用例")
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()

    def create_case(self, name, group):
        """按名称在组目录 Test_cases/<group>/ 下创建空白用例文件
        (已存在则提示)并加载到编辑区/列表"""
        gdir = os.path.join(CASES_DIR, group)
        os.makedirs(gdir, exist_ok=True)
        path = os.path.join(gdir, f"{name}.yaml")
        if os.path.exists(path):
            QMessageBox.warning(self, "新建用例", f"用例已存在: {group}/{name}.yaml")
            return
        self.case_path = path
        self.data = self._empty_data()
        self.data["module"] = name
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText(_safe_relpath(path))
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()
        self._fill_case_list()      # 新用例出现在列表
        for i in range(self.case_list.count()):
            if self.case_list.item(i).data(Qt.UserRole) == path:
                self.case_list.setCurrentRow(i)   # 选中新用例
                break
        self.status_label.setText(f"已新建用例: {group}/{name}.yaml(编辑自动保存)")
        self.log_view.appendPlainText(f"[新建] {path}")

    def on_open(self):
        if self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "打开用例", CASES_DIR, "YAML 用例 (*.yaml *.yml)")
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            QMessageBox.critical(self, "打开失败", f"YAML 解析失败:\n{e}")
            return
        if not isinstance(data, dict) or not data.get("cases"):
            QMessageBox.warning(self, "格式不符", "文件需包含 module 与 cases 字段")
            return
        self.case_path = path
        self.data = data
        self.case_idx = 0
        self.expanded_key = None
        self.file_label.setText(_safe_relpath(path))
        self._load_case_into_ui()
        self.render_cards()
        self._refresh_case_selector()
        self._fill_case_list()   # ★ 打开的用例必须在左侧列表可见(Test_cases/ 外的也追加)

    def _dump_data(self):
        return {"module": self.data.get("module", "未命名"),
                "cases": [dict(c, steps=[schema.serialize_step(s) for s in c.get("steps", [])])
                          for c in self.data.get("cases", [])]}

    def on_save(self):
        if self.worker:
            return
        if not self.case_path:
            os.makedirs(CASES_DIR, exist_ok=True)
            path, _ = QFileDialog.getSaveFileName(
                self, "保存用例", os.path.join(CASES_DIR, "新用例.yaml"), "YAML 用例 (*.yaml)")
            if not path:
                return
            self.case_path = path
        self._sync_header()
        # 校验全部用例(多 case 文件逐一检查)
        problems = []
        for ci, case in enumerate(self.data.get("cases", [])):
            label = case.get("name") or f"第{ci + 1}个用例"
            for i, s in enumerate(case.get("steps", [])):
                for err in schema.validate_step(s):
                    problems.append(f"用例[{label}] 步骤{i + 1}: {err}")
        if problems and QMessageBox.question(
                self, "存在参数问题,仍要保存?", "\n".join(problems[:10])) != QMessageBox.Yes:
            return
        # 保存重写整个文件会丢掉顶层 case_order(执行顺序)—— 先记后补
        prev_order = (self._read_case_order(self.case_path)
                      if os.path.isfile(self.case_path) else None)
        with open(self.case_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._dump_data(), f, allow_unicode=True, sort_keys=False)
        if prev_order is not None:
            self._write_case_order(self.case_path, prev_order)
        self._fill_case_list()   # 新保存的用例进入列表(勾选/选中状态保留)
        self.file_label.setText(_safe_relpath(self.case_path))
        self.status_label.setText(f"已保存: {os.path.basename(self.case_path)}")
        self.log_view.appendPlainText(f"[保存] {self.case_path}")

    # ── YAML 源码同步 ──
    def _refresh_yaml_text(self):
        if not hasattr(self, "yaml_edit"):
            return
        self.yaml_edit.setPlainText(
            yaml.safe_dump(self._dump_data(), allow_unicode=True, sort_keys=False))

    def _apply_yaml_text(self):
        if self.worker:
            return
        try:
            data = yaml.safe_load(self.yaml_edit.toPlainText()) or {}
            if not isinstance(data, dict):
                raise ValueError("顶层必须是键值结构")
        except Exception as e:
            QMessageBox.critical(self, "解析失败", f"YAML 有误:\n{e}")
            return
        self.data = data
        self.case_idx = 0
        self.expanded_key = None
        self._load_case_into_ui()
        self.render_cards()
        self._auto_save()
        self._refresh_case_selector()

    # ── 执行 ──
    def on_run(self):
        if self.worker:
            return
        self.on_save()
        case_files = self._checked_case_paths()
        if not case_files:
            QMessageBox.warning(self, "提示", "请先在左侧勾选要执行的用例")
            return
        device_id = self.device_combo.currentData()
        pre_items = self._selected_preconditions()   # 当前组的前置(兜底用)
        pre_by_group = self._pre_items_by_group(case_files)   # ★ 每个 APP 组各自的前置
        repeat = self.repeat_spin.value()
        self.result_table.setRowCount(0)
        self._running_row = None
        self._sub_rows = {}          # 子步骤行索引(sub_id → 行号), 开跑时清空
        self._set_preview_placeholder("单击结果行显示对应截图\n点击图片可放大查看")

        # ★ Qt 标准所有权(审查 2026-10-12 #23): worker 挂到窗口下 + run() 返回后
        #   deleteLater。这样 on_run_finished 里的 self.worker=None 只是丢一个无害的
        #   Python 包装 —— C++ 对象归窗口所有, 析构永远在 run() 返回之后, "线程还
        #   活着就被析构"的 qFatal 闪退从根上不再可能(退休机制因此整体删除)。
        self.worker = RunWorker(device_id, case_files, pre_items, repeat,
                                parent=self, pre_by_group=pre_by_group)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker.step_done.connect(self.on_step_done)
        # ★ 步骤开始就插一行「执行中」, 跑完再就地改成通过/失败(用户要求 2026-10-12)
        self.worker.step_started.connect(self.on_step_started)
        # 注: 不再连接 log_line → 运行日志(执行期间的 core 日志已由全局
        #     handler 转发, 再连一次会每条显示两遍)
        self.worker.status.connect(self.on_worker_status)
        self.worker.finished_run.connect(self.on_run_finished)
        # 状态灯:执行中黄 / 通过绿 / 失败红(画在用例名后面)
        self.worker.case_started.connect(
            lambda fp, name: self.set_case_state(fp, "running"))
        self.worker.case_finished.connect(
            lambda fp, name, ok: self.set_case_state(fp, "passed" if ok else "failed"))
        self.worker.precondition_failed.connect(self.on_precondition_failed)
        # ★ 前置失败弹窗(用户 2026-10-15): 重试/取消 —— 用户在设备上排除故障后选择
        self.worker.precondition_retry.connect(self.on_precondition_retry)
        # ★ 暂停 → 当前步骤行显示「⏸ 暂停中」(用户 2026-10-15)
        self.worker.step_paused.connect(self.on_step_paused)
        # ★ 开始执行即在结果表里留痕(浅蓝开始行)—— 以前第一条步骤完成前
        #   表格一片空白,用户不知道跑没跑起来(用户反馈)
        self._append_result_row(
            "—", "▶ 开始执行",
            f"{len(case_files)} 个用例 × {repeat} 轮 · 设备 {device_id or '未选'}", "#e8f0fe")
        # ★ 点运行的瞬间全部勾选用例灯变黄(含上次跑过的,重新执行先重置)
        self._reset_case_lamps_to_running(case_files)
        self.worker.start()
        self._set_locked(True)  # 执行期间锁定编排区
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.pause_btn.setEnabled(True)
        self.pause_btn.setText("⏸ 暂停")
        self.tabs.setCurrentIndex(0)
        self.log_view.appendPlainText(
            f"[运行] {len(case_files)} 个文件 × {repeat} 轮 设备={device_id}")

    def on_pause(self):
        """暂停/继续切换(用户 2026-10-13): 同一个按钮, 按当前状态翻转。
        暂停语义见 gui.runner_thread.RunWorker.request_pause —— 步骤间挂起,
        超时顺延, 设备保持现状; 挂起中「停止」随时可用。"""
        w = self.worker
        if not w:
            return
        if w._pause_requested:
            w.request_resume()
            self.pause_btn.setText("⏸ 暂停")
        else:
            w.request_pause()
            self.pause_btn.setText("▶ 继续")

    def on_stop(self):
        w = self.worker
        if w:
            # ★ 防误点: 停止前先确认(用户要求 2026-10-12)。与"关窗"那条确认同一套写法,
            #   说明里写清楚会发生什么 —— 停止不是失败, 已跑出的结果都会保留。
            if QMessageBox.question(
                    self, "停止执行",
                    "确定停止本次执行吗?\n\n当前步骤跑完就停下, 已经跑出的结果会保留。",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self._set_status("停止中(当前步骤结束后退出)...", self.status_label)
            try:
                # ★ 用 w 而非 self.worker(审查 2026-10-12): 确认框的模态循环会泵事件,
                #   队列里的 finished_run 可能恰好在框内被分发 → on_run_finished 已把
                #   self.worker 清掉; 对 None 调 request_stop 是 AttributeError。
                w.request_stop()
            except RuntimeError:
                pass            # C++ 对象已删(执行恰已结束): 无需再停

    def on_step_started(self, result):
        """步骤开始 → 先插一行「执行中」, 跑完由 on_step_done 就地改成通过/失败。

        用户要求(2026-10-12): "要把当前执行的步骤也写出来, 执行时显示为执行中, 执行结束
        再显示通过还是失败" —— 以前整步跑完才出现一行, 长步骤(几十秒的断言)期间界面
        像卡住, 不知道在跑什么。
        """
        prev = getattr(self, "_running_row", None)
        same = (prev is not None and prev < self.result_table.rowCount()
                and self.result_table.item(prev, 2) is not None
                and self.result_table.item(prev, 2).text() == result.get("desc", ""))
        if same:
            row = prev                     # ★ 重试: 复用同一行(显示"执行中(第N次)")
        else:
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
        self._fill_result_row(row, result, running=True)
        self._running_row = row
        self.result_table.resizeRowsToContents()
        self.result_table.scrollToBottom()
        self.tabs.setCurrentIndex(0)

    def on_step_paused(self, paused):
        """暂停/恢复 → 结果表把**当前步骤行**标「⏸ 暂停中」(用户 2026-10-15)。

        · 步骤执行中暂停: 就地改「执行中」那一行(记下原文, 恢复时还原);
        · 步骤**之间**暂停(上一行已完成, 没有"执行中"行): 追加一条中性「已暂停」行,
          恢复时删掉 —— 删行后若下方已有新行(_running_row 指向它), 行号要跟着上移。
        """
        row = getattr(self, "_running_row", None)
        if paused:
            if row is not None and row < self.result_table.rowCount():
                it = self.result_table.item(row, 3)
                if it is not None:
                    self._pause_prev = (row, it.text(),
                                        it.foreground().color().name())
                    it.setText("⏸ 暂停中")
                    it.setForeground(QColor("#64748b"))
                    return
            r = self.result_table.rowCount()
            self.result_table.insertRow(r)
            for col, text in enumerate(("—", "", "（步骤间暂停）", "⏸ 暂停中", "", "")):
                it = QTableWidgetItem(text)
                it.setBackground(QColor("#eef2f7"))
                if col in (0, 3, 4):
                    it.setTextAlignment(Qt.AlignCenter)
                self.result_table.setItem(r, col, it)
            self._running_row = None
            self._pause_row = r
            self.result_table.resizeRowsToContents()
            self.result_table.scrollToBottom()
            return
        # 恢复
        prev = getattr(self, "_pause_prev", None)
        if prev:
            r, text, color = prev
            if r < self.result_table.rowCount():
                it = self.result_table.item(r, 3)
                if it is not None and it.text() == "⏸ 暂停中":
                    it.setText(text)
                    it.setForeground(QColor(color))
            self._pause_prev = None
        r = getattr(self, "_pause_row", None)
        if r is not None:
            self.result_table.removeRow(r)
            self._pause_row = None
            # ★ 删行后下方的行号上移: _running_row 若指向它, 必须跟着减一 ——
            #   否则后续 step_done 会写错行(暂停期间恰好跨到下一步时会踩到)
            if self._running_row is not None and self._running_row > r:
                self._running_row -= 1

    def on_step_done(self, result):
        # ★ if 子步骤(sub=True)一律**追加新行**, 不占用父 if 的"执行中"占位 ——
        #   否则父行会被第一个子步骤顶掉、最后追加到子步骤后面(用户 2026-10-15)
        if result.get("sub"):
            # ★ 按 sub_id 就地更新(用户 2026-10-15): 嵌套 if 这类**容器型子步骤**会先
            #   推一行「执行中」(保证它的子步骤排在它后面), 跑完再推一次 —— 必须原地
            #   改写而不是再插一行。没有 sub_id 的(旧路径)照旧追加。
            sid = result.get("sub_id")
            rows = getattr(self, "_sub_rows", None)
            if rows is None:
                rows = self._sub_rows = {}
            row = rows.get(sid) if sid is not None else None
            if row is None or row >= self.result_table.rowCount():
                row = self.result_table.rowCount()
                self.result_table.insertRow(row)
                if sid is not None:
                    rows[sid] = row
            self._fill_result_row(row, result, running=bool(result.get("running")))
            self.result_table.resizeRowsToContents()
            self.result_table.scrollToBottom()
            return
        row = getattr(self, "_running_row", None)
        if row is None or row >= self.result_table.rowCount():
            row = self.result_table.rowCount()   # 没有对应的"开始"事件(如前置项) → 直接追加
            self.result_table.insertRow(row)
        self._fill_result_row(row, result)       # ★ 有"执行中"行时就地改成 PASS/FAIL
        self._running_row = None
        self.result_table.resizeRowsToContents()
        self.result_table.scrollToBottom()

    def _fill_result_row(self, row, result, running=False):
        """写一行结果(执行中 / PASS / FAIL 共用)"""
        passed = result.get("passed")
        elapsed = result.get("elapsed")
        attempt = result.get("attempt", 1) or 1
        if running:
            state = "执行中" if attempt <= 1 else f"执行中(第{attempt}次)"
            color = QColor("#d97706")            # 执行中: 橙色
        elif result.get("stopped"):
            # ★ 用户停止打断的步骤显示中性「⏹ 已停止」(用户 2026-10-15: 停止不是失败,
            #   红色 FAIL 会让人以为这一步跑砸了)
            state = "⏹ 已停止"
            color = QColor("#64748b")
        elif passed is None:
            # ★ 中性态(用户 2026-10-15): 用户停止时"还没轮到的前置项"没执行过,
            #   显示成 FAIL 会让人以为它们失败了
            state = "⏹ 未执行"
            color = QColor("#64748b")
        else:
            state = "PASS" if passed else "FAIL"
            color = QColor("#16a34a") if passed else QColor("#dc2626")
        values = [str(row + 1), result.get("case", ""), result.get("desc", ""), state,
                  f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "",
                  result.get("error", "")]
        for col, val in enumerate(values):
            item = QTableWidgetItem(val)
            if col == 3:
                item.setForeground(color)
            if col in (3, 4):        # ★ 结果 / 耗时 居中显示(用户要求 2026-10-12)
                item.setTextAlignment(Qt.AlignCenter)
            item.setData(Qt.UserRole, result.get("screenshot", ""))
            if val:
                item.setToolTip(val)  # 长文本(步骤/错误信息)悬停看全文
            self.result_table.setItem(row, col, item)
        shot = result.get("screenshot", "")
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)

    def _on_result_selection(self):
        """单击/键盘选中结果行 → 预览该步骤截图(无截图显示占位)"""
        row = self.result_table.currentRow()
        if row < 0:
            return
        item = self.result_table.item(row, 0)
        shot = item.data(Qt.UserRole) if item else ""
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)
        else:
            self._set_preview_placeholder("该步骤无截图")

    def on_result_row(self, row, _col):
        shot = self.result_table.item(row, 0).data(Qt.UserRole)
        if shot and os.path.exists(shot):
            self._show_screenshot(shot)
        else:
            self._set_preview_placeholder("该步骤无截图")

    def _set_preview_placeholder(self, text):
        self._preview_path = None
        self.preview.setPixmap(QPixmap())
        self.preview.setText(text)

    def _show_screenshot(self, path):
        self._preview_path = path
        pix = QPixmap(path)
        if not pix.isNull():
            self.preview.setPixmap(
                pix.scaled(self.preview.width(), self.preview.height(),
                           Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _zoom_preview(self):
        """点击预览图 → 独立窗口放大查看"""
        if self._preview_path and os.path.exists(self._preview_path):
            dlg = ImageViewDialog(self._preview_path, self)
            dlg.exec()

    def on_worker_status(self, message):
        """阶段状态 → 状态栏;用例开始时在执行结果表插分隔行,让用户看到在跑"""
        self.status_label.setText(message)
        if message.startswith("执行用例"):
            # 分隔行文字放「步骤」列(结果列留给 PASS/FAIL 这类判定)
            self._append_result_row("—", "", "▶ " + message, "#f0f4fa")

    def _append_result_row(self, num, result, desc, bg):
        """向执行结果表追加一行彩色标记行(开始行/用例分隔行/总结行)。
        列序与表格一致:#、用例(第几次)、步骤(desc)、结果(result)、耗时、错误信息"""
        row = self.result_table.rowCount()
        self.result_table.insertRow(row)
        for col, text in enumerate((num, "", desc, result, "", "")):
            it = QTableWidgetItem(text)
            f = it.font()
            f.setBold(True)
            it.setFont(f)
            it.setBackground(QColor(bg))
            if col in (0, 3, 4):        # 序号/结果/耗时居中(与数据行对齐)
                it.setTextAlignment(Qt.AlignCenter)
            self.result_table.setItem(row, col, it)
        self._running_row = None        # 分隔/总结行之后不该再往旧行里写
        self._pause_row = None           # 步骤间暂停时追加的中性行(2026-10-15)
        self._pause_prev = None          # 暂停时被改写的「执行中」行原文, 恢复时还原
        self.result_table.scrollToBottom()
        self.tabs.setCurrentIndex(0)

    def on_run_finished(self, passed, message):
        """执行结束。★ 解锁与 worker 清理必须无条件执行(try/finally)——
        显示逻辑一旦抛异常,界面会永久锁死、所有编辑入口被 worker 守卫拦住
        (用户实测:执行后无法编辑/展开步骤)"""
        try:
            # ★ 手动停止**不是失败**(用户要求 2026-10-12: "同时按你的建议修改手动停止的显示"):
            #   以前停止也走 passed=False 这条路 → 状态栏与总结行都渲染成红色"未通过",
            #   容易让人以为这轮跑砸了。停止是中性结果, 单独给个"⏹ 已停止"(灰蓝底)。
            #   ★ 精确匹配而不是子串(审查 2026-10-12): 前置等待的失败消息里带着用户文案
            #     (如「设备已停止充电」), 子串命中会把真失败渲染成中性"已停止"
            stopped = message == "已手动停止"
            if stopped:
                self.status_label.setText("⏹ " + message)
                self._append_result_row("—", "⏹ 已停止", message, "#eef2f7")
            else:
                self.status_label.setText(("✔ " if passed else "✘ ") + message)
                # ★ 最终结果写入执行结果表格(总结行,绿/红底粗体)——
                #   只显示在右上角状态栏太不明显(用户反馈)
                self._append_result_row(
                    "—", "✔ 全部通过" if passed else "✘ 未通过", message,
                    "#e6f7e9" if passed else "#fdeaea")
            self.log_view.appendPlainText(f"[结束] {message}")
        except Exception as e:
            self.log_view.appendPlainText(f"[内部] 结束处理异常: {e}")
        finally:
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self.pause_btn.setEnabled(False)
            self.pause_btn.setText("⏸ 暂停")
            self._set_locked(False)
            # ★ parent 所有权下(见 on_run)这里丢的只是 Python 包装, 线程还活着也安全;
            #   C++ 对象由 finished→deleteLater 在 run() 返回后销毁
            self.worker = None

    def closeEvent(self, event):
        # ★ 留痕: 日志里出现这行 = **用户正常关窗**(不是崩溃/被杀)。下次排查"闪退"时,
        #   没有这行又有其它退出迹象, 就说明进程是被硬结束的(见 core/crashlog.py)。
        log.info("[退出] 收到关窗请求")
        self._save_expanded_groups()      # 关窗前记住组展开状态(下次打开还原)
        self._cleanup_update_backups()    # 正常关窗 = 本次更新确实可用, 可以扔掉旧版本
        if self.worker:
            if QMessageBox.question(self, "正在执行", "用例正在执行,停止并退出?") == QMessageBox.Yes:
                try:
                    self.worker.request_stop()
                    self.worker.wait(5000)
                except RuntimeError:
                    pass                       # 包装可能已被 GC(执行刚结束): 无需再停
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()


# ── 从拆分出去的模块 re-export(结构整理 2026-10-15) ──
# 这些类/常量原先就在本模块, 现在按职责搬到了 gui/style|widgets|dialogs|step_card|threads。
# 这里显式 re-export, 好让界面/测试/工具里的 `main_window.X` 用法保持不变。
from gui.style import (  # noqa: F401
    CASE_STATE_COLORS, CASE_STATE_ROLE, _LAMP_SIZE, _ARROW_W, _FIELD_LABEL_W,
    _CHECK_MIN_H, make_lamp_pixmap, _LAMP_PIX, lamp_pixmap, lamp_icon,
    _safe_relpath, CATEGORY_COLORS, QUICK_ACTIONS, STYLESHEET, _ASSETS,
)
from gui.widgets import (  # noqa: F401
    ClickableLabel, ClickableFrame, FlowLayout, make_action_menu, _FormCheckBox,
    _HistComboDelegate, _FitCombo, _FitListView, _HistCombo, CaseListWidget,
    CaseItemDelegate,
)
from gui.dialogs import (  # noqa: F401
    ImageViewDialog, StatusWordsDialog, PreconditionEditDialog,
    PreconditionsDialog, NewCaseDialog,
)
from gui.step_card import (  # noqa: F401
    _make_field_widget, _int_of, _float_of, _int4_of, StepCard, _StepsHost,
    _StepsEditor,
)
from gui.threads import (  # noqa: F401
    DeviceScanThread, UpdateCheckThread, UpdateDownloadThread,
)
