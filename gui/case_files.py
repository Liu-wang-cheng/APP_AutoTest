# -*- coding: utf-8 -*-
"""用例文件级操作: 重命名、文件名 ↔ 用例名双向绑定、路径状态迁移。

从 `gui/main_window.py` 拆出(结构守护要求主窗口 <= 3000 行; 2026-10-19 的
双向绑定逻辑把主窗口顶超了)。这里的实现只依赖宿主窗口的属性/方法, 由
`MainWindow(CaseFileMixin, QMainWindow)` 混入, 对外仍可 `main_window.X` 访问。
"""
import os
import re

import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QInputDialog, QMessageBox

from gui.style import _safe_relpath


class CaseFileMixin:
    """用例文件重命名 / 名字绑定相关方法(MainWindow 的状态由宿主提供)。"""

    # ── 重命名(2026-10-19 用户反馈: 粘贴后的名字改不了) ───────────────────
    def _rename_case_dialog(self, path):
        """右键「重命名用例…」入口。"""
        if self.worker:
            QMessageBox.information(self, "重命名用例", "用例执行中, 结束后再重命名。")
            return
        old = os.path.splitext(os.path.basename(path))[0]
        new, ok = QInputDialog.getText(self, "重命名用例", "新的用例名:", text=old)
        if not ok:
            return
        try:
            self._rename_case(path, new)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "重命名失败", str(e))

    def _rename_case(self, path, new_name):
        """重命名用例文件(列表显示的就是文件名), 并同步 YAML 里的用例名。

        ★ 三处必须一起动, 否则看起来就是"名字改不了":
          文件名(列表) / YAML `cases[].name`(右侧「用例」框) / 编辑器当前路径。
          单 case 文件强制让 YAML 名字跟文件名一致; 多 case 文件只替换与旧文件名
          相同的那一条, 其它自定义用例名不动。
        """
        if self.worker:
            raise ValueError("用例执行中, 结束后再重命名")
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise ValueError("用例文件不存在")
        new_name = self._validate_case_file_name(new_name)
        old_stem, ext = os.path.splitext(os.path.basename(path))
        ext = ext or ".yaml"             # .yml 保持原扩展名, 不要偷偷改格式
        if new_name == old_stem:
            return path
        gdir = os.path.dirname(path)
        dst = os.path.join(gdir, new_name + ext)
        if os.path.exists(dst):
            raise ValueError(f"同组下已存在用例: {new_name}{ext}")

        # ★ 改名后旧路径就没了: 防抖中的编辑必须先落盘, 否则直接丢
        self._flush_pending_edit()
        is_open = bool(self.case_path and self._same_path(self.case_path, path))
        os.rename(path, dst)
        try:
            self._rename_case_name_in_yaml(dst, old_stem, new_name,
                                           force_single=True)
        except OSError:
            pass                         # 文件已改名, YAML 名字没同步不阻断

        # 勾选态/状态灯/复制源都跟着换 key, 否则重建列表后全部丢失
        self._migrate_case_path(path, dst)

        if is_open:
            self.case_path = dst
            # 从磁盘重新装填: YAML 名字可能刚被改写(含单 case 强制改名),
            # 内存里必须与磁盘一致, 否则下一次自动保存又把旧名字写回去。
            try:
                with open(dst, encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if isinstance(data, dict) and data.get("cases"):
                    self.data = data
                    self.case_idx = min(self.case_idx,
                                        len(data["cases"]) - 1)
            except Exception:
                pass
            self.file_label.setText(_safe_relpath(dst))
            self._load_case_into_ui()
            self.render_cards()
            self._refresh_case_selector()
            self._refresh_yaml_text()

        self._fill_case_list()
        target = os.path.abspath(dst)
        for i in range(self.case_list.count()):
            p = self.case_list.item(i).data(Qt.UserRole)
            if p and self._same_path(p, target):
                self.case_list.setCurrentRow(i)
                break
        self._set_status(f"已重命名用例: {old_stem} → {new_name}")
        return dst

    @staticmethod
    def _rename_case_name_in_yaml(path, old, new, force_single=False):
        """把 YAML 里值为 old 的 `- name:` 改成 new; 返回改动条数。

        ★ 文本级逐行替换: 保留注释、缩进、换行符、case_order 与其它字段。
          旧值用 yaml.safe_load 解析(单/双引号都认), 新值统一写成单引号标量
          (任意字符都合法, 不会把 `#`/`true` 之类解析成注释或布尔值)。
        ★ force_single: 单 case 文件即使名字与旧文件名不同也强制改成新名 ——
          重命名文件是明确动作, 单 case 的用例名应当跟文件名一致。
        """
        if old == new and not force_single:
            return 0
        with open(path, encoding="utf-8", newline="") as f:
            lines = f.readlines()
        entries = []            # (行号, 前缀, 解析出的旧值, 行尾注释/空格, 换行符)
        for i, line in enumerate(lines):
            body = line.rstrip("\r\n")
            eol = line[len(body):]
            m = re.match(r"^(\s*-\s*name:\s*)(.*?)([ \t]*(?:#.*)?)$", body)
            if not m:
                continue
            raw = m.group(2).strip()
            try:
                parsed = yaml.safe_load("v: " + raw)
                val = parsed.get("v") if isinstance(parsed, dict) else None
            except Exception:
                val = None
            entries.append((i, m.group(1), val, m.group(3), eol))
        targets = [e for e in entries if e[2] == old]
        if not targets and force_single and len(entries) == 1:
            targets = entries
        if not targets:
            return 0
        quoted = "'" + str(new).replace("'", "''") + "'"
        for i, prefix, _val, tail, eol in targets:
            lines[i] = f"{prefix}{quoted}{tail}{eol}"
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.writelines(lines)
        return len(targets)

    @staticmethod
    def _validate_case_file_name(new_name):
        """校验"用例名也是文件名"的合法性; 返回去空白后的名字, 非法抛 ValueError。"""
        name = (new_name or "").strip()
        if not name:
            raise ValueError("用例名不能为空")
        if any(ch in name for ch in '\\/:*?"<>|') or name in (".", ".."):
            raise ValueError('用例名不能包含 \\ / : * ? " < > |')
        if name != name.rstrip(" ."):
            raise ValueError("用例名不能以空格或点结尾")
        if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", name):
            raise ValueError(f"「{name}」是 Windows 保留名, 请换一个")
        return name

    def _bind_file_name_to_case_name(self, new_name):
        """把当前打开用例的文件名改成 new_name(单 case 文件专用)。

        ★ 只改文件名/路径引用; YAML 里的 name 已由 `_sync_header` 写进内存,
          随后的 `_do_auto_save()` 会立即落盘。两处任改一边, 另一边都在提交时同步。
        """
        if not self.case_path or len(self.data.get("cases") or []) != 1:
            return None
        new_name = self._validate_case_file_name(new_name)
        old_path = self.case_path
        old_stem, ext = os.path.splitext(os.path.basename(old_path))
        ext = ext or ".yaml"
        if new_name == old_stem:
            return old_path
        dst = os.path.join(os.path.dirname(old_path), new_name + ext)
        if os.path.exists(dst) and not self._same_path(dst, old_path):
            raise ValueError(f"同组下已存在用例: {new_name}{ext}")
        if self._same_path(dst, old_path):
            return old_path           # 仅大小写变化: NTFS 上不值得冒险
        os.rename(old_path, dst)
        self.case_path = dst
        self._migrate_case_path(old_path, dst)
        group = self._group_of(dst)
        self._collapsed_groups.discard(group)   # 改名后保证列表里能看见
        self._known_groups.add(group)
        self.file_label.setText(_safe_relpath(dst))
        self._fill_case_list()
        for i in range(self.case_list.count()):
            p = self.case_list.item(i).data(Qt.UserRole)
            if p and self._same_path(p, dst):
                self.case_list.setCurrentRow(i)
                break
        return dst

    def _migrate_case_path(self, old, new):
        """路径级状态(勾选/状态灯/复制源)从一个文件迁到另一个。"""
        was_checked = any(self._same_path(p, old) for p in self._checked)
        self._checked = {p for p in self._checked
                         if not self._same_path(p, old)}
        if was_checked:
            self._checked.add(new)
        state = next((s for p, s in self._case_states.items()
                      if self._same_path(p, old)), None)
        self._case_states = {p: s for p, s in self._case_states.items()
                             if not self._same_path(p, old)}
        if state is not None:
            self._case_states[new] = state
        if self._case_clipboard and self._same_path(self._case_clipboard, old):
            self._case_clipboard = new

    @staticmethod
    def _same_path(a, b):
        """Windows 路径比较: 统一绝对路径 + normcase(大小写/斜杠差异)。"""
        if not a or not b:
            return False
        return os.path.normcase(os.path.abspath(str(a))) == \
            os.path.normcase(os.path.abspath(str(b)))

    @staticmethod
    def _group_of(path):
        """用例文件所属 APP 组 = 所在目录名(未分组文件返回所在目录名本身)。"""
        return os.path.basename(os.path.dirname(os.path.abspath(str(path))))
