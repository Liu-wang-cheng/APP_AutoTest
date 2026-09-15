import logging
import math
import operator
import os
import re
import subprocess
import time

import cv2
import numpy as np
from PIL import Image
import uiautomator2 as u2

from common import vision
from common.driver import BASE_DIR

log = logging.getLogger("vacuum_test")

# ── 可调参数集中定义(校准依据见各使用处) ──
SWITCH_BLUE_RATIO = 0.34   # 开关区域蓝色通道占比: 打开>0.34, 关闭(灰)≈0.33
SWITCH_SATURATION = 30     # 开关区域 HSV 平均饱和度: 有色(开)>30, 灰(关)<30
SWITCH_TMPL_THRESHOLD = 0.7  # 开关 SIFT 失败时,归一化模板匹配的置信度下限
POLL_SHORT_N = 3           # 前 N 次轮询用 1s 短间隔(元素可能马上出现)
POLL_MID_N = 10            # 到第 N 次用 2s 中间隔
POLL_SHORT = 1
POLL_MID = 2
POLL_MAX = 5               # 轮询间隔封顶 5s(原 30s 会让刚出现的元素白等半小时级)
TRACE_LIMIT = 8            # 失败时回溯保留的步骤数(截图留内存,失败才落盘)
TRACE_JPEG_QUALITY = 70    # 时序截图质量:够看清页面状态,单张约 100KB

# 数值断言算子映射(顺序即匹配优先级,>= 必须在 > 之前)
_NUM_OPS = {
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
    "==": operator.eq,
}


def d_serial(d):
    """获取设备序列号"""
    try:
        return d.serial
    except Exception:
        return ""


class UserStopped(RuntimeError):
    """用户请求停止执行(GUI 停止按钮触发)"""


class ActionRunner:
    """YAML 步骤执行引擎"""

    IMAGE_DIR = vision.TEMPLATE_DIR  # 模板图片固定目录(绝对路径)

    def __init__(self, device: u2.Device, config: dict, case_wait=None, case_name=""):
        self.d = device
        self._device_id = getattr(device, 'serial', None)
        self.case_name = case_name  # 用于失败截图命名,避免跨用例覆盖
        # 用例级 wait 优先，否则用全局 step_interval
        self.interval = case_wait if case_wait is not None else config.get("step_interval", 3)
        self.timeout = config.get("default_timeout", 30)
        self.click_timeout = config.get("click_timeout", 10)
        self.results = []
        self.store = {}  # grab/match 数据暂存
        self._trace = []  # 最近若干步的时序证据(内存,失败时才落盘)
        self._trace_limit = config.get("trace_limit", TRACE_LIMIT)
        self.stopped = False  # 停止标志,GUI 停止按钮置位
        self.on_result = None  # 结果回调(GUI 实时推送用),签名 fn(result_dict)
        # 执行过程中动态写入的状态,集中在此初始化
        self._locators = {}
        self._last_compare_msg = None
        self._pending_sub_results = None
        self._debug_map_path = None
        self._stored_zones = []
        self._stored_zone_boxes = []  # 与 _stored_zones 同序的分区外框,供合并排序
        self._next_room_idx = 0
        self._ocr = None  # ddddocr 懒加载(加载慢且非所有用例用到)
        # 加载定位器配置（用于 ${section.key} 引用，换 APP 时集中修改）
        try:
            import yaml
            loc_path = os.path.join(BASE_DIR, "config", "locators.yaml")
            if os.path.exists(loc_path):
                with open(loc_path, encoding="utf-8") as f:
                    self._locators = yaml.safe_load(f) or {}
        except Exception as e:
            log.warning(f"[locators] 加载失败: {e}")

    def _ensure_ocr(self):
        """OCR 引擎懒加载"""
        if self._ocr is None:
            import ddddocr
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        return self._ocr

    def _append_result(self, entry):
        """记录步骤结果;设置了 on_result 时同步回调(GUI 实时刷新)"""
        self.results.append(entry)
        if callable(self.on_result):
            try:
                self.on_result(entry)
            except Exception:
                pass

    def stop(self):
        """请求停止执行: 等待点在 _sleep/_poll_sleep/步骤边界"""
        self.stopped = True

    def _sleep(self, seconds):
        """可中断等待: 收到停止请求后快速退出并抛 UserStopped"""
        end = time.time() + max(0.0, seconds)
        while not self.stopped:
            remain = end - time.time()
            if remain <= 0:
                return
            time.sleep(min(0.2, remain))
        raise UserStopped("用户手动停止")

    def _poll_sleep(self, check_count):
        """轮询间隔: 1s → 2s → 5s 封顶(保证刚出现的元素尽快被发现)"""
        if check_count <= POLL_SHORT_N:
            self._sleep(POLL_SHORT)
        elif check_count <= POLL_MID_N:
            self._sleep(POLL_MID)
        else:
            self._sleep(POLL_MAX)

    def _trace_capture(self, desc, passed):
        """记录一步的时序证据:描述 + 截图(JPEG 留在内存,失败时才落盘)

        截图存 JPEG 而不是原始位图:一屏 BGR 约 6MB,留 8 步就是 50MB;
        JPEG 编码后单张约 100KB。截图失败不影响执行,只丢这一张图。
        """
        if not hasattr(self, '_trace'):
            self._trace = []          # 兼容 __new__ 构造的调用方(如 GUI 测试)
        limit = getattr(self, '_trace_limit', TRACE_LIMIT)
        if limit <= 0:
            return
        shot = None
        try:
            screen = self.d.screenshot(format="opencv")
            ok, buf = cv2.imencode(
                ".jpg", screen, [int(cv2.IMWRITE_JPEG_QUALITY), TRACE_JPEG_QUALITY])
            if ok:
                shot = buf.tobytes()
        except Exception as e:
            log.debug(f"[trace] 截图失败(不影响执行): {e}")
        self._trace.append({"desc": desc, "passed": passed, "shot": shot})
        if len(self._trace) > limit:
            del self._trace[:-limit]

    def _dump_trace(self, step_index, out_root=None):
        """失败时把时序证据落盘,返回目录(无内容返回 "")

        产出 steps.txt + 每步一张 jpg。steps.txt 每行:
            序号 <TAB> PASS/FAIL <TAB> 步骤描述 <TAB> 截图文件名
        这张表就是失败诊断(以及后续接模型做归因)的输入格式。
        """
        trace = getattr(self, '_trace', None)
        if not trace:
            return ""
        prefix = f"{self.case_name}_" if self.case_name else ""
        root = out_root or os.path.join(BASE_DIR, "reports", "failures")
        out_dir = os.path.join(root, f"{prefix}trace")
        try:
            os.makedirs(out_dir, exist_ok=True)
            lines = []
            for n, item in enumerate(trace):
                shot_name = ""
                if item.get("shot"):
                    shot_name = f"{n:02d}_{'ok' if item['passed'] else 'fail'}.jpg"
                    with open(os.path.join(out_dir, shot_name), "wb") as f:
                        f.write(item["shot"])
                lines.append("\t".join([
                    f"{n:02d}",
                    "PASS" if item["passed"] else "FAIL",
                    item["desc"],
                    shot_name,
                ]))
            with open(os.path.join(out_dir, "steps.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            log.error(f"[诊断] 失败前 {len(lines)} 步的时序证据已保存: {out_dir}")
            return out_dir
        except Exception as e:
            log.warning(f"[trace] 落盘失败: {e}")
            return ""

    def _failure_screenshot(self, step_index):
        """失败截图存到 reports/failures/,文件名带用例名避免跨用例覆盖"""
        prefix = f"{self.case_name}_" if self.case_name else ""
        fail_path = os.path.join(BASE_DIR, "reports", "failures", f"{prefix}step_{step_index:02d}_fail.png")
        try:
            os.makedirs(os.path.dirname(fail_path), exist_ok=True)
            self.d.screenshot(fail_path)
            return fail_path
        except Exception:
            return ""

    def _ensure_device(self):
        """操作前心跳检测，掉线自动重连（最多3次）"""
        for attempt in range(3):
            try:
                self.d.info  # 轻量调用，失败即掉线
                return
            except Exception as e:
                log.warning(f"[device] 连接异常({e})，尝试重连 {attempt+1}/3")
                if self._device_id:
                    try:
                        time.sleep(3)
                        self.d = u2.connect(self._device_id)
                        log.info(f"[device] 重连成功: {self._device_id}")
                    except Exception as e2:
                        log.error(f"[device] 重连失败: {e2}")
                else:
                    break

    def _resolve_step(self, step):
        """递归解析 step 中的 ${section.key} 定位器引用"""
        if isinstance(step, dict):
            return {k: self._resolve_step(v) for k, v in step.items()}
        if isinstance(step, list):
            return [self._resolve_step(v) for v in step]
        if isinstance(step, str):
            return self._resolve_str(step)
        return step

    def _resolve_str(self, s):
        """解析字符串中的 ${section.key} → locators.yaml 中的值；未定义则原样保留"""
        for ref in re.findall(r'\$\{([^}]+)\}', s):
            if '.' in ref:
                section, key = ref.split('.', 1)
                val = self._locators.get(section, {}).get(key)
                if val is not None:
                    s = s.replace(f'${{{ref}}}', str(val))
        return s

    def run_steps(self, steps: list) -> bool:
        """依次执行所有步骤，任一失败则中断。步骤支持 retry:N 自动重试;支持中途停止"""
        for i, step in enumerate(steps):
            if self.stopped:
                break
            step = self._resolve_step(step)  # 解析 ${section.key} 定位器引用
            desc = self._step_desc(step)
            # retry: 失败自动重试次数（默认0=不重试）
            retry = step.get("retry", 0) if isinstance(step, dict) else 0
            max_attempts = retry + 1
            succeeded = False
            last_err = None
            screenshot = ""
            for attempt in range(max_attempts):
                if self.stopped:
                    break
                self._ensure_device()
                try:
                    screenshot = self._execute(step) or ""
                    if getattr(self, '_last_compare_msg', None):
                        desc = f"{desc} ({self._last_compare_msg})"
                        self._last_compare_msg = None
                    self._append_result({"desc": desc, "passed": True, "error": "", "screenshot": screenshot})
                    self._trace_capture(desc, True)
                    if getattr(self, '_pending_sub_results', None):
                        for sub in self._pending_sub_results:
                            self._append_result(sub)
                        self._pending_sub_results = None
                    succeeded = True
                    break
                except UserStopped:
                    last_err = UserStopped("用户手动停止")
                    break  # 不重试,直接走失败收尾
                except Exception as e:
                    last_err = e
                    if attempt < max_attempts - 1:
                        log.warning(f"[retry] 步骤「{desc}」第{attempt+1}次失败: {e}，重试...")
                        self._sleep(2)
                    else:
                        # 最终失败：截图 + 诊断
                        screenshot = self._failure_screenshot(i)
                        try:
                            xml = self.d.dump_hierarchy()
                            texts = [t for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]
                            log.error(f"[FAIL诊断] 步骤「{desc}」失败: {e}")
                            log.error(f"[FAIL诊断] 当前页面文本(前30): {texts[:30]}")
                            log.error(f"[FAIL诊断] 截图: {screenshot}")
                        except Exception:
                            pass
            if not succeeded:
                err_text = "用户手动停止执行" if (self.stopped or isinstance(last_err, UserStopped)) else str(last_err)
                self._append_result({"desc": desc, "passed": False, "error": err_text, "screenshot": screenshot})
                # 真实失败才留时序证据;用户主动停止没有诊断价值
                if not (self.stopped or isinstance(last_err, UserStopped)):
                    self._trace_capture(desc, False)
                    self._dump_trace(i)
                if getattr(self, '_pending_sub_results', None):
                    for sub in self._pending_sub_results:
                        self._append_result(sub)
                    self._pending_sub_results = None
                return False
            # 步骤间等待(可中断)
            if i < len(steps) - 1:
                wait_time = step.get("wait") if isinstance(step, dict) else None
                try:
                    self._sleep(wait_time if wait_time is not None else self.interval)
                except UserStopped:
                    self._append_result({"desc": "用户手动停止", "passed": False, "error": "用户手动停止执行", "screenshot": ""})
                    return False
        if self.stopped:
            self._append_result({"desc": "用户手动停止", "passed": False, "error": "用户手动停止执行", "screenshot": ""})
            return False
        return True

    def _execute(self, step: dict):
        screenshot = ""
        # 1. 主操作：按映射表顺序找第一个匹配的 key 执行（新增动作只需在表里加一条）
        for key, handler in self._MAIN_DISPATCH:
            if key in step:
                handler(self, step)
                break
        # 2. 数据抓取/对比
        if "grab" in step:
            self._do_grab(step["grab"])
        elif "match" in step:
            self._do_match(step["match"])
        # 4. 截图
        if "screenshot" in step:
            path = step["screenshot"]
            # screenshots/ 开头的路径补全 Test_img/ 前缀（reports/ 等其他路径原样）
            if path.startswith("screenshots/"):
                path = os.path.join(BASE_DIR, "Test_img", path)
            elif not os.path.isabs(path):
                path = os.path.join(BASE_DIR, path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.d.screenshot(path)
            screenshot = path
        # room_zones 的标注图优先
        if getattr(self, '_debug_map_path', None):
            screenshot = self._debug_map_path
            self._debug_map_path = None
        return screenshot

    # 主操作分发表：(step_key, handler) 有序列表，第一个匹配的 key 执行
    # 新增动作：实现 _do_xxx(step) 方法，然后在此表追加一条 ("动作名", lambda s, st: s._do_xxx(st))
    _MAIN_DISPATCH = [
        ("click", lambda s, st: s._do_click(st)),
        ("assert", lambda s, st: s._do_assert(st)),
        ("input", lambda s, st: s._do_input(st["input"])),
        ("latest_record", lambda s, st: s._do_latest_record()),
        ("find_click", lambda s, st: s._do_find_click(st["find_click"])),
        ("if", lambda s, st: s._do_if(st)),
        ("if not", lambda s, st: s._do_if(st, negate=True)),
        ("back", lambda s, st: s.d.press("back")),
        ("long_click", lambda s, st: s._do_long_click(st)),
        ("switch_to", lambda s, st: s._do_switch_to(
            st["switch_to"], area=st.get("switch_area"),
            switch_tpl=st.get("switch_tpl"), switch_label=st.get("switch_label"))),
        ("assert_switch", lambda s, st: s._do_assert_switch(st["assert_switch"], st.get("switch_area"))),
        ("add_timer", lambda s, st: (s._do_add_timer(**st["add_timer"]) if isinstance(st["add_timer"], dict)
                                     else s._do_add_timer())),
        ("swipe", lambda s, st: s._do_swipe(st["swipe"])),
        ("room_zones", lambda s, st: s._do_room_zones(
            st["room_zones"] if isinstance(st["room_zones"], list) else None, st.get("desc", "zones"))),
        ("room_click", lambda s, st: s._do_room_click(st["room_click"] if isinstance(st["room_click"], int) else 1)),
        ("merge_zones", lambda s, st: s._do_merge_zones()),
        ("split_zone", lambda s, st: s._do_split_zone()),
        ("if_click", lambda s, st: s._do_if_click(st)),
        ("compare", lambda s, st: s._do_compare(st["compare"], st.get("threshold", 0.6))),
        ("set_time", lambda s, st: s._do_set_time(st["set_time"], st.get("circular", False))),
        ("wait_loading", lambda s, st: s._wait_loading(st.get("timeout", 30))),
        ("wait_for", lambda s, st: s._do_wait_for(st["wait_for"], st.get("timeout", 15))),
        ("diff", lambda s, st: s._do_compare(st["diff"], st.get("threshold", 0.99), inverse=True)),
    ]

    def _do_long_click(self, step):
        """长按：坐标/模板/文本，duration 控制时长"""
        target = step["long_click"]
        duration = step.get("duration", 2)
        if isinstance(target, list):
            x, y = target[0], target[1]
            duration = target[2] if len(target) > 2 else duration
            self.d.long_click(x, y, duration=duration)
        elif self._is_image(str(target)):
            pos = self._match_template(target, timeout=10)
            if pos:
                self.d.long_click(pos[0], pos[1], duration=duration)
        else:
            el = self._find_element(str(target))
            if el.exists(timeout=5):
                b = el.info["bounds"]
                self.d.long_click((b["left"] + b["right"]) // 2,
                                  (b["top"] + b["bottom"]) // 2, duration=duration)

    def _do_if_click(self, step):
        """条件点击：存在则点（支持逗号分隔多选）"""
        target = step["if_click"]
        opts = [o.strip() for o in target.split(",")] if isinstance(target, str) else target
        for opt in opts:
            if self.d(textContains=opt).exists(timeout=3):
                self.d(textContains=opt).click()
                break

    # ── 点击 ──
    def _do_click(self, step: dict):
        timeout = step.get("timeout", self.click_timeout)
        value = step["click"]
        if self._is_image(value):
            self._click_by_template(value, timeout)
        else:
            self._click_by_locator(value, timeout)

    # ── 断言 ──
    def _do_assert(self, step: dict):
        timeout = step.get("timeout", self.timeout)
        val = step["assert"]
        # 兼容旧格式: assert: {value: xxx, timeout: 0}
        if isinstance(val, dict):
            value = val.get("value", str(val))
            timeout = val.get("timeout", timeout)
        else:
            value = val

        if self._is_image(value):
            self._assert_template(value, timeout)
        else:
            self._assert_locator(value, timeout)

    # ── 输入 ──
    def _do_input(self, value):
        el = self.d(focused=True)
        if el.exists(timeout=3):
            el.set_text(value)
        else:
            raise RuntimeError("未找到输入框，请先 click 输入框再 input")

    # ── 底层：文本/ID 定位 ──
    def _click_by_locator(self, value, timeout=None):
        timeout = timeout if timeout is not None else self.click_timeout
        end_time = time.time() + timeout if timeout > 0 else None
        check_count = 0
        while True:
            if end_time and time.time() >= end_time:
                raise TimeoutError(f"click 超时({timeout}s)未找到: {value}")
            el = self._find_element(value)
            if el.exists(timeout=1):
                try:
                    info = el.info
                    if not info.get("clickable"):
                        # 非 clickable 时尝试点击所在的 *_Row 整行（RN 列表行）
                        if self._click_row_fallback(value, info.get("text", "")):
                            return
                except Exception as e:
                    log.debug(f"[click] 行点击回退异常: {e}")
                el.click()
                return
            check_count += 1
            self._poll_sleep(check_count)

    def _click_row_fallback(self, value, text_val):
        """在层级 XML 中查找包含目标文本的 *_Row 子树并点击其中心

        Row 内的标签文本与搜索文本(或元素自身文本)匹配即命中,
        优先用 content-desc 定位,失败则按 bounds 中心点坐标点击。
        """
        xml = self.d.dump_hierarchy()
        for rm in re.finditer(
                r'content-desc="(\w+_Row)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            row_end = xml.find('content-desc="', rm.end())
            if row_end < 0:
                row_end = len(xml)
            subtree = xml[rm.start():row_end]
            # 搜索文本或元素文本在这个 Row 的子树中才算命中
            if value in subtree or (text_val and text_val in subtree):
                if self.d(description=rm.group(1)).exists(timeout=1):
                    self.d(description=rm.group(1)).click()
                else:
                    self.d.click((int(rm.group(2)) + int(rm.group(4))) // 2,
                                 (int(rm.group(3)) + int(rm.group(5))) // 2)
                return True
        return False

    def _assert_locator(self, value, timeout=None):
        timeout = timeout if timeout is not None else self.timeout
        end_time = time.time() + timeout if timeout > 0 else None
        # 支持逗号分隔的多值断言，任一匹配即通过
        values = [v.strip() for v in value.split(",")] if "," in value else [value]
        check_count = 0
        while True:
            if end_time and time.time() >= end_time:
                raise AssertionError(f"超时({timeout}s)未找到: {value}")
            for v in values:
                if self._find_element(v).exists(timeout=1):
                    return
            check_count += 1
            self._poll_sleep(check_count)

    def _find_element(self, value):
        # #N后缀 → 第N个匹配（如 "虚拟墙#2" 点第2个虚拟墙）
        index = None
        if '#' in value and value.split('#')[-1].isdigit():
            value, index = value.rsplit('#', 1)
            index = int(index) - 1
        if ":id/" in value:
            return self.d(resourceId=value)
        # =前缀 → 精确文本匹配
        if value.startswith("="):
            el = self.d(text=value[1:])
        else:
            el = self.d(textContains=value)
            if not el.exists(timeout=1):
                el = self.d(description=value)
        if (not el.exists(timeout=1)) and value == "确认":
            # 时间选择器确认按钮回退
            el = self.d(description="Popup_Confirm")
        if index is not None and el.exists(timeout=1):
            return self.d(textContains=value if not value.startswith('=') else value[1:])[index]
        return el

    # ── 底层：模板匹配 ──
    def _click_by_template(self, img_name, timeout=None):
        timeout = timeout if timeout is not None else self.click_timeout
        pos = self._match_template(img_name, timeout=timeout)
        if pos is None:
            raise TimeoutError(f"click 超时({timeout}s)模板匹配失败: {img_name}")
        self.d.click(pos[0], pos[1])

    def _assert_template(self, img_name, timeout=None):
        timeout = timeout if timeout is not None else self.timeout
        pos = self._match_template(img_name, timeout)
        if pos is None:
            raise AssertionError(f"超时({timeout}s)图片未找到: {img_name}")

    def _match_template(self, img_name, timeout=None, min_matches=4):
        """SIFT 特征点匹配定位图标(匹配算法见 common/vision.py,模板特征已缓存)"""
        timeout = timeout if timeout is not None else self.timeout
        img_path = os.path.join(self.IMAGE_DIR, img_name)
        end_time = time.time() + timeout if timeout > 0 else None
        check_count = 0

        while True:
            if end_time and time.time() >= end_time:
                return None

            screen = self.d.screenshot(format="opencv")
            screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
            pos = vision.find_in_gray(screen_gray, img_path, min_matches)
            if pos is not None:
                return pos

            check_count += 1
            self._poll_sleep(check_count)

    # ── 抓取/对比数据 ──
    def _do_grab(self, keywords):
        """抓取页面上包含关键字的文本及相邻数字，存入 self.store"""
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]
        nodes = self._get_all_nodes()   # 带 bounds,供空间邻近匹配
        results = []
        for kw in keywords:
            val = self._extract_value(nodes, kw)
            if val is None:
                raise RuntimeError(f"grab 未找到 '{kw}' 的数据")
            self.store[kw] = val
            results.append(f"{kw}={val}")
        self._last_compare_msg = "主页: " + ", ".join(results)

    def _do_match(self, keywords):
        """对比当前页面数据与 grab 存储的数据（数值允许 ±1 浮动）"""
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]
        nodes = self._get_all_nodes()   # 带 bounds,供空间邻近匹配
        mismatches = []
        match_results = []
        for kw in keywords:
            stored = self.store.get(kw, "")
            found = self._extract_value(nodes, kw)
            # 预约时间特殊处理：从层级中取第一个非状态栏(y>80)的 HH:MM
            if found is None and kw == "预约时间":
                xml = self.d.dump_hierarchy()
                items = re.findall(r'text="(?:\d{4}-\d{2}-\d{2} )?(\d{1,2}:\d{2})"[^>]*bounds="\[\d+,(\d+)\]', xml)
                times = [t for t, y in items if int(y) > 80]
                if times:
                    found = times[0]
            match_results.append(f"{kw}={found}")
            if found is None:
                # grab 在主页抓到过、记录页却找不到 → 判失败,防止数据缺失假通过
                mismatches.append(f"主页={stored}, 记录页未找到'{kw}'")
            elif found != stored:
                try:
                    if abs(float(stored) - float(found)) <= 1:
                        continue
                except ValueError:
                    pass
                mismatches.append(f"主页={stored}, 记录={found}")
        if mismatches:
            self._last_compare_msg = "记录: " + ", ".join(match_results) + " | " + "; ".join(mismatches)
            raise AssertionError(f"数据不一致: {'; '.join(mismatches)}")
        self._last_compare_msg = "记录: " + ", ".join(match_results) + " | 一致"

    def _do_switch_to(self, state, area=None, switch_tpl=None, switch_label=None):
        """切换开关到指定状态：颜色判断当前→不对则点击→验证
        switch_tpl: 可选，用于定位开关的模板图片名
        area: 可选 [x1,y1,x2,y2] 直接指定开关区域
        switch_label: 可选，通过同行文本标签（如"定制模式"）自动定位开关
        """
        expect_on = state in ("on", "打开", "开", True)
        is_on, pos = self._get_switch_state(area, switch_tpl, switch_label)
        if is_on == expect_on:
            log.info(f"[switch] 已是{'打开' if expect_on else '关闭'}状态，跳过")
            self._last_compare_msg = f"开关={'打开' if expect_on else '关闭'}(无需切换)"
            return
        if pos is None:
            raise RuntimeError("无法定位开关位置")
        self.d.click(pos[0], pos[1])
        log.info(f"[switch] 点击开关({pos[0]},{pos[1]})")
        time.sleep(3)
        # 验证
        is_on, _ = self._get_switch_state(area, switch_tpl, switch_label)
        if is_on == expect_on:
            log.info(f"[switch] 切换成功→{'打开' if expect_on else '关闭'}")
            self._last_compare_msg = f"开关={'打开' if expect_on else '关闭'}"
        else:
            raise AssertionError(f"开关切换失败，当前{'打开' if is_on else '关闭'}")

    def _get_switch_state(self, area=None, switch_tpl=None, switch_label=None):
        """获取开关状态及位置，返回 (is_on, pos)：优先颜色检测
        switch_tpl: 用于定位开关的模板图片名（默认 勿扰打开.png）
        area: 可选 [x1,y1,x2,y2] 直接指定开关区域
        switch_label: 可选，通过同行文本标签（如"定制模式"）自动定位开关
        """
        screen = self.d.screenshot(format="opencv")
        if area:
            x1, y1, x2, y2 = area
            pos = ((x1 + x2) // 2, (y1 + y2) // 2)
            crop = screen[y1:y2, x1:x2]
        elif switch_label:
            el = self.d(textContains=switch_label)
            if not el.exists(timeout=3):
                el = self.d(description=switch_label)
            if not el.exists(timeout=1):
                raise AssertionError(f"未找到开关标签: {switch_label}")
            b = el.info["bounds"]
            scr_w = screen.shape[1]
            # 开关在标签同行最右侧，只取右端窄区域
            x1 = scr_w - 200
            y1 = max(0, b["top"] - 15)
            x2 = scr_w - 30
            y2 = min(screen.shape[0], b["bottom"] + 15)
            pos = ((x1 + x2) // 2, (y1 + y2) // 2)
            crop = screen[y1:y2, x1:x2]
        else:
            tpl_name = switch_tpl or "勿扰打开.png"
            tpl_path = os.path.join(self.IMAGE_DIR, tpl_name)
            h, w = np.array(Image.open(tpl_path).convert("L")).shape[:2]
            pos = None
            right_gray = None
            # 只在右半屏搜索（开关始终在右侧，避免左侧UI误匹配）
            half = screen.shape[1] // 2
            for _ in range(2):
                right_gray = cv2.cvtColor(screen[:, half:, :], cv2.COLOR_BGR2GRAY)
                found = vision.find_in_gray(right_gray, tpl_path, min_matches=3)
                if found:
                    pos = (found[0] + half, found[1])
                    break
                time.sleep(0.3)
                screen = self.d.screenshot(format="opencv")
            if not pos:
                # SIFT 失败时回退归一化模板匹配
                tpl = np.array(Image.open(tpl_path).convert("L"))
                r = cv2.matchTemplate(right_gray, tpl, cv2.TM_CCORR_NORMED)
                _, v, _, loc = cv2.minMaxLoc(r)
                if v >= SWITCH_TMPL_THRESHOLD:
                    pos = (loc[0] + half + w // 2, loc[1] + h // 2)
            if not pos:
                raise AssertionError(f"未定位到开关模板: {tpl_name}")
            # 以匹配位为中心扩散 2x 采样颜色（3x会稀释蓝色信号导致ON误判为OFF）
            x1, y1 = max(0, pos[0] - w), max(0, pos[1] - h)
            x2, y2 = min(screen.shape[1], pos[0] + w), min(screen.shape[0], pos[1] + h)
            crop = screen[y1:y2, x1:x2]
        # 蓝色通道占比：打开=蓝色>阈值，关闭=灰色≈0.33
        bgr_mean = np.mean(crop, axis=(0, 1))
        blue_ratio = bgr_mean[0] / (bgr_mean[0] + bgr_mean[1] + bgr_mean[2] + 1)
        return blue_ratio > SWITCH_BLUE_RATIO, pos

    def _do_assert_switch(self, state, area=None):
        """判断开关状态：打开=高饱和度颜色(蓝/绿)，关闭=灰色(低饱和度)
        state: 'on'/'打开' = 期望开，'off'/'关闭' = 期望关
        area: [x1,y1,x2,y2] 可选，指定开关精确区域（避免其他颜色干扰）
        """
        screen = self.d.screenshot(format="opencv")
        if area:
            # 用户指定区域
            x1, y1, x2, y2 = area
            crop = screen[y1:y2, x1:x2]
        else:
            # 自动定位：找开关文字右侧
            el = self.d(textContains="勿扰开关")
            if not el.exists(timeout=3):
                el = self.d(textContains="开关")
            if not el.exists(timeout=3):
                raise AssertionError("未找到开关标签")
            b = el.info["bounds"]
            x1 = min(b["right"] + 5, screen.shape[1] - 180)
            y1 = max(0, b["top"] - 20)
            x2 = min(x1 + 180, screen.shape[1])
            y2 = min(b["bottom"] + 70, screen.shape[0])
            crop = screen[y1:y2, x1:x2]
        # HSV 饱和度判断：开关有色=打开，灰色=关闭
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        mean_sat = hsv[:, :, 1].mean()
        is_on = mean_sat > SWITCH_SATURATION  # 均值饱和度超阈值=有色=打开
        # 期望状态
        expect_on = state in ("on", "打开", "开", True)
        expect_off = state in ("off", "关闭", "关", False)
        if expect_on and is_on:
            log.info(f"[switch] 开关已打开(饱和度{mean_sat:.0f})")
            self._last_compare_msg = f"开关=打开(饱和度{mean_sat:.0f})"
        elif expect_off and not is_on:
            log.info(f"[switch] 开关已关闭(饱和度{mean_sat:.0f})")
            self._last_compare_msg = f"开关=关闭(饱和度{mean_sat:.0f})"
        elif expect_on and not is_on:
            raise AssertionError(f"开关未打开(饱和度{mean_sat:.0f})")
        elif expect_off and is_on:
            raise AssertionError(f"开关未关闭(饱和度{mean_sat:.0f})")
        else:
            raise AssertionError(f"无法识别期望状态: {state}，用 on/off 或 打开/关闭")

    def _do_add_timer(self, add_text=None, add_tpl=None, max_tasks=2):
        """添加定时任务，自动处理任务上限、删除、弹窗确认
        add_text: YAML指定的添加按钮文本，如 "添加" / "新增预约"
        add_tpl:  YAML指定的添加按钮模板图，如 "添加预约.png"
        max_tasks: 任务数达此值时先删旧任务再添加，默认 2
        """
        # ── 1. 自动统计任务数 ──
        xml = self.d.dump_hierarchy()
        cells = re.findall(r'content-desc="(Timer_TimerCell\d+)"', xml)
        switches = re.findall(r'class="[^"]*Switch[^"]*"', xml)
        cell_count = len(set(cells)) if cells else len(switches)
        log.info(f"[add_timer] 现有任务数: {cell_count}")

        # ── 2. 任务数达上限 → 自动删除一个 ──
        if cell_count >= max_tasks:
            target_desc = f"Timer_TimerCell{max_tasks - 1}"
            el = self.d(description=target_desc)
            if not el.exists(timeout=2):
                switch_els = [e for e in self.d(className="android.widget.Switch")]
                if len(switch_els) >= max_tasks:
                    el = switch_els[max_tasks - 1]
            if hasattr(el, 'info') and el.exists(timeout=1):
                b = el.info["bounds"]
                self.d.long_click((b["left"] + b["right"]) // 2,
                                  (b["top"] + b["bottom"]) // 2, duration=4)
                time.sleep(2)

                # ── 3. 自动检测弹窗确认按钮 ──
                new_xml = self.d.dump_hierarchy()
                clicked = False
                for pat in [r'text="([^"]*确认[^"]*)"', r'text="([^"]*确定[^"]*)"',
                            r'text="([^"]*删除[^"]*)"', r'text="([^"]*Delete[^"]*)"',
                            r'text="([^"]*OK[^"]*)"', r'text="([^"]*Yes[^"]*)"']:
                    m = re.search(pat, new_xml)
                    if m and self.d(text=m.group(1)).exists(timeout=2):
                        self.d(text=m.group(1)).click()
                        time.sleep(2)
                        log.info(f"[add_timer] 已删除任务(弹窗:{m.group(1)})")
                        clicked = True
                        break
                if not clicked:
                    for dp in [r'content-desc="(Popup_Confirm)"', r'content-desc="(Button_Confirm)"']:
                        m = re.search(dp, new_xml)
                        if m and self.d(description=m.group(1)).exists(timeout=2):
                            self.d(description=m.group(1)).click()
                            time.sleep(2)
                            log.info(f"[add_timer] 已删除任务(弹窗:{m.group(1)})")
                            clicked = True
                            break
                if not clicked:
                    log.info("[add_timer] 无确认弹窗，假定已直接删除")
            else:
                log.info("[add_timer] 无法定位任务行，跳过删除")

        # ── 4. 找添加按钮（YAML指定优先，否则自动） ──
        xml = self.d.dump_hierarchy()
        # a) YAML指定的文本
        if add_text:
            if self.d(text=add_text).exists(timeout=3):
                self.d(text=add_text).click()
                log.info(f"[add_timer] 点击添加按钮(文本:{add_text})")
                return
        # b) YAML指定的模板
        if add_tpl:
            pos = self._match_template(add_tpl, timeout=5)
            if pos:
                self.d.click(pos[0], pos[1])
                log.info(f"[add_timer] 点击添加按钮(模板:{add_tpl})")
                return
        # c) 自动回退
        for t in ["添加", "新增", "添加预约"]:
            if self.d(text=t).exists(timeout=2):
                self.d(text=t).click()
                log.info(f"[add_timer] 点击添加按钮(自动:{t})")
                return
        pos = self._match_template("添加预约.png", timeout=5)
        if pos:
            self.d.click(pos[0], pos[1])
            log.info("[add_timer] 点击添加按钮(自动:添加预约.png)")
            return
        raise RuntimeError("未找到添加定时任务按钮")

    def _do_latest_record(self):
        """在清扫记录列表中自动点击最新一条记录"""
        texts = self._get_all_texts()
        for i, t in enumerate(texts):
            if t.strip() and t.startswith('面积') and re.search(r'\d', t):
                el = self.d(text=t)
                if el.exists(timeout=3):
                    el.click()
                    return
        for i, t in enumerate(texts):
            if re.match(r'\d{4}-\d{2}-\d{2}', t.strip()) or re.match(r'\d{2}:\d{2}', t.strip()):
                el = self.d(text=t)
                if el.exists(timeout=3):
                    el.click()
                    return
        raise RuntimeError("未找到清扫记录条目")

    def _do_room_zones(self, bounds=None, label="zones"):
        """识别地图上的彩色房间分区并依次点击
        bounds: [x1, y1, x2, y2] 地图区域，默认屏幕中间区域
        """
        screen = self.d.screenshot(format="opencv")
        h, w = screen.shape[:2]
        if bounds is None:
            x1, y1, x2, y2 = int(w * 0.01), int(h * 0.20), int(w * 0.84), int(h * 0.73)
        else:
            x1, y1, x2, y2 = bounds
        roi = screen[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        # 只取有颜色的像素（排除白/灰/黑背景）
        color_mask = (s > 10) & (v > 20) & (v < 240)
        # 对有色像素的色调做直方图，找峰值=不同颜色房间
        hue_hist = cv2.calcHist([h], [0], color_mask.astype(np.uint8), [180], [0, 180])
        hue_flat = hue_hist.flatten().astype(np.float32)
        # 简单均值平滑
        hue_flat = np.convolve(hue_flat, np.ones(5)/5, mode='same')
        # 找直方图峰值
        peaks = []
        for i in range(2, 178):
            if hue_flat[i] > hue_flat[i-1] and hue_flat[i] > hue_flat[i-2] and \
               hue_flat[i] > hue_flat[i+1] and hue_flat[i] > hue_flat[i+2] and \
               hue_flat[i] > 200:
                peaks.append(i)
        # 每个色调峰值找一个连通区域
        zones = []
        rh, rw = roi.shape[:2]
        roi_area = rw * rh
        mask_all = np.zeros_like(h)
        # 诊断计数:在循环外初始化,保证 zones 为空时错误信息里也有数据可看
        colored_px = int(cv2.countNonZero(color_mask.astype(np.uint8)))
        n_contours = 0
        min_area = roi_area * 0.001
        for pk in peaks:
            hue_mask = (h > pk - 8) & (h < pk + 8) & color_mask
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
            hue_mask = cv2.morphologyEx(hue_mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
            contours, _ = cv2.findContours(hue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            n_contours += len(contours)
            mask_all = mask_all | hue_mask
            for c in contours:
                area = cv2.contourArea(c)
                if area > min_area:
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = int(M["m10"] / M["m00"])
                        cy = int(M["m01"] / M["m00"])
                        # 向下偏移避开房间名字（名字通常在上方）
                        cy += int((y2 - y1) * 0.03)
                        bx, by, bw, bh = cv2.boundingRect(c)  # 外框,合并时判相邻用
                        zones.append((cx + x1, cy + y1, area,
                                      (bx + x1, by + y1, bx + bw + x1, by + bh + y1)))
        zones.sort(key=lambda z: -z[2])
        # 去重：合并中心距离过近的分区（同一房间被拆成多块）
        merged = []
        min_dist = min((x2 - x1), (y2 - y1)) * 0.15  # 距离阈值=区域尺寸的15%
        for z in zones:
            zx, zy = z[0], z[1]
            too_close = False
            for m in merged:
                if abs(zx - m[0]) < min_dist and abs(zy - m[1]) < min_dist:
                    too_close = True
                    break
            if not too_close:
                merged.append(z)
        zones = merged
        debug_dir = os.path.join(BASE_DIR, "reports", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        # 标注检测区域（红框）+ 分区中心（绿点）
        debug = screen.copy()
        cv2.rectangle(debug, (x1, y1), (x2, y2), (0, 0, 255), 3)
        for z in zones[:8]:
            cx, cy = z[0], z[1]
            cv2.circle(debug, (cx, cy), 10, (0, 255, 0), -1)
            bx1, by1, bx2, by2 = z[3]      # 外框:合并排序用的就是它,画出来方便核对
            cv2.rectangle(debug, (bx1, by1), (bx2, by2), (255, 128, 0), 2)
        # 用 PIL 保存，支持中文路径
        Image.fromarray(cv2.cvtColor(screen, cv2.COLOR_BGR2RGB)).save(os.path.join(debug_dir, "map_screen.png"))
        Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)).save(os.path.join(debug_dir, "map_roi.png"))
        Image.fromarray(mask_all).save(os.path.join(debug_dir, "map_mask.png"))
        path = os.path.join(debug_dir, f"map_{label}.png")
        Image.fromarray(cv2.cvtColor(debug, cv2.COLOR_BGR2RGB)).save(path)
        self._debug_map_path = path
        if not zones:
            # 分档提示:彩色像素=0 说明地图是灰度/深色主题/根本没加载出来;
            # 有彩色但没峰值 说明房间颜色太接近或峰值阈值过高;
            # 有轮廓但没分区 说明色块面积低于下限。
            raise RuntimeError(
                f"未识别到房间分区 (彩色像素={colored_px}, 色调峰值={len(peaks)}, "
                f"轮廓={n_contours}, 有效面积下限={min_area:.0f}px²)"
            )
        # 只存储，不点击
        self._stored_zones = [(z[0], z[1]) for z in zones[:8]]
        self._stored_zone_boxes = [z[3] for z in zones[:8]]
        self._next_room_idx = 0  # 重置点击游标
        msg = f"识别到 {len(self._stored_zones)} 个房间分区"
        log.info(f"[room_zones] {msg}")
        self._last_compare_msg = msg

    def _click_dialog_confirm(self):
        """点击弹窗确认按钮：优先按文本/描述定位，失败回退屏幕等比位置

        等比位置按 1080x1920 校准(原硬编码 558,1389),换分辨率设备也能用
        """
        for how, val in [("text", "确认"), ("text", "确定"), ("desc", "Popup_Confirm")]:
            el = self.d(description=val) if how == "desc" else self.d(text=val)
            if el.exists(timeout=1):
                el.click()
                log.info(f"[dialog] 点击确认按钮({val})")
                return True
        w, h = self.d.window_size()
        self.d.click(int(w * 558 / 1080), int(h * 1389 / 1920))
        log.info("[dialog] 未找到确认按钮,按等比位置点击")
        return True

    @staticmethod
    def _zone_pair_gap(a, b):
        """两个分区外框的边到边距离;相接或重叠为 0

        用切比雪夫式的轴向边距再取欧氏距离,相邻房间约为 0,
        隔着一个房间的会明显大于 0。
        """
        ax1, ay1, ax2, ay2 = a
        bx1, by1, bx2, by2 = b
        dx = max(bx1 - ax2, ax1 - bx2, 0)
        dy = max(by1 - ay2, ay1 - by2, 0)
        return math.hypot(dx, dy)

    @staticmethod
    def _merge_pair_order(zones, boxes):
        """合并候选对的尝试顺序:几何相邻的排前面

        只有相邻房间能合并。原来盲枚举 (0,1)(0,2)... 每撞一次不相邻就要
        白花约 10 秒(两次点击 + 全程 sleep + 重进合并模式),而且期间一直在动地图。
        没有外框信息(旧调用方/识别降级)时退回原枚举顺序,行为不变。
        """
        n = len(zones)
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
        if len(boxes) != n:
            return pairs
        pairs.sort(key=lambda p: ActionRunner._zone_pair_gap(boxes[p[0]], boxes[p[1]]))
        return pairs

    def _do_merge_zones(self):
        """合并房间：已在合并模式，按几何相邻顺序尝试分区对直到生效"""
        zones = getattr(self, '_stored_zones', [])
        boxes = getattr(self, '_stored_zone_boxes', [])
        if len(zones) < 2:
            raise RuntimeError(f"合并需要至少 2 个分区，当前仅 {len(zones)} 个")
        log.info(f"[merge] {len(zones)} 个分区")
        for i, j in self._merge_pair_order(zones, boxes):
            log.info(f"[merge] 尝试对 ({i},{j})")
            self.d.click(*zones[i]); time.sleep(2)
            self.d.click(*zones[j]); time.sleep(2)
            if self.d(text="合并").exists(timeout=2):
                self.d(text="合并").click(); time.sleep(3)
                self._wait_loading()
            if self.d(textContains="提示").exists(timeout=2):
                self._click_dialog_confirm(); time.sleep(2)
                log.info(f"[merge] {i},{j} 不相邻")
                continue
            if self.d(textContains="请选择").exists(timeout=2) or \
               self.d(textContains="区域分割").exists(timeout=2):
                log.info(f"[merge] {i},{j} 成功")
                return
            # 失败后重进合并模式（旧代码失败后 click room_merge）
            self.d(textContains="区域合并").click(); time.sleep(3)
            log.info(f"[merge] {i},{j} 未生效")
        raise RuntimeError(f"所有分区对未成功合并")

    def _do_set_time(self, offset, circular=False):
        """根据设备当前时间+offset分钟，用 OCR+滑动精确设置时间选择器
        circular: True=循环滚轮(最短路径环绕)，False=单向滚轮(纯数值不环绕)
        """
        # 1. 获取设备当前时间
        try:
            cur = subprocess.check_output(
                ['adb', '-s', d_serial(self.d), 'shell', 'date', '+%H:%M'],
                timeout=5).decode().strip()
            ch, cm = map(int, cur.split(':'))
        except Exception:
            raise RuntimeError("无法获取设备当前时间")
        # 2. 计算目标时间
        total = (ch * 60 + cm + offset) % (24 * 60)
        th, tm = total // 60, total % 60
        log.info(f"[set_time] 当前{ch:02d}:{cm:02d} +{offset}min → 目标{th:02d}:{tm:02d}")

        # 3. OCR 识别滚轮当前值（懒加载）
        self._ensure_ocr()

        # 4. 自动识别滚轮区域（DatePicker_ 或 Timer_TimerPicker_）
        hour_b = self._wheel_bounds(self._find_wheel_desc("Hour"))
        min_b = self._wheel_bounds(self._find_wheel_desc("Minute"))
        if hour_b and min_b:
            hx = (hour_b["left"] + hour_b["right"]) // 2
            mx = (min_b["left"] + min_b["right"]) // 2
            h_w = (hour_b["right"] - hour_b["left"]) // 4
            m_w = (min_b["right"] - min_b["left"]) // 8
        else:
            # 无滚轮控件时的兜底位置(按 1080x1920 校准的等比坐标)
            w, h = self.d.window_size()
            hx, mx = int(w * 276 / 1080), int(w * 810 / 1080)
            h_w, m_w = int(w * 110 / 1080), int(w * 28 / 1080)
        self._adjust_wheel("hour", hx, th, 24, step=60, crop_w=h_w, circular=circular)
        self._adjust_wheel("minute", mx, tm, 60, step=35, crop_w=m_w, circular=circular)
        self.store["预约时间"] = f"{th:02d}:{tm:02d}"
        self._last_compare_msg = f"设置{th:02d}:{tm:02d}(当前{ch:02d}:{cm:02d}+{offset}min)"

    def _wheel_bounds(self, desc):
        """获取滚轮控件 bounds（适配多APP）"""
        el = self.d(description=desc)
        if el.exists(timeout=2):
            return el.info.get("bounds")
        return None

    def _find_wheel_desc(self, name):
        """自动查找滚轮控件描述（DatePicker_ 或 Timer_TimerPicker_）"""
        for prefix in ["DatePicker_", "Timer_TimerPicker_"]:
            desc = f"{prefix}{name}"
            if self.d(description=desc).exists(timeout=1):
                return desc
        return f"DatePicker_{name}"

    def _adjust_wheel(self, name, cx, target, mod, step=50, crop_w=60, circular=False):
        """计数法：探测方向+每格步长→计数滑动（不反复OCR）→验证→小步补救"""
        self._ensure_ocr()
        b = self._wheel_bounds(self._find_wheel_desc(name.capitalize()))
        # 兜底 cy 按 1080x1920 校准等比换算
        _, wh = self.d.window_size()
        cy = (b["top"] + b["bottom"]) // 2 if b else int(wh * 1525 / 1920)
        y1, y2 = cy - 35, cy + 35

        def read_val():
            screen = self.d.screenshot(format="opencv")
            crop = screen[y1:y2, cx-crop_w:cx+crop_w]
            crop = cv2.resize(crop, (crop.shape[1]*2, crop.shape[0]*2),
                              interpolation=cv2.INTER_CUBIC)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            raw = self._ocr.classification(Image.fromarray(gray)).strip()
            try:
                return int(raw)
            except Exception:
                return -1

        def short_diff(a, b, m):
            # 带符号最短差值：边界(|d|=m/2)保持原方向（0→30增大，30→0减小）
            d = b - a
            if d > m // 2:
                d -= m
            elif d < -(m // 2):
                d += m
            return d

        def swipe_up(s):
            self.d.swipe(cx, cy, cx, cy - s)
        def swipe_down(s):
            self.d.swipe(cx, cy - s, cx, cy)

        # 1. 探测方向 + 每格步长 k（探测后恢复原位）
        v0 = read_val()
        up_increase, k = True, 1
        if v0 >= 0:
            determined = False
            for fn in [swipe_up, swipe_down]:
                fn(step); time.sleep(0.8)
                v1 = read_val()
                if v1 >= 0 and v1 != v0:
                    sd = short_diff(v0, v1, mod)
                    is_up = (fn == swipe_up)
                    moved_inc = sd > 0
                    up_increase = moved_inc if is_up else (not moved_inc)
                    k = max(1, abs(sd))
                    # 恢复原位（反向滑回去）
                    recover = swipe_down if is_up else swipe_up
                    recover(step); time.sleep(0.6)
                    log.info(f"[set_time] {name} 探测 {v0}→{v1}→恢复 k={k} 上滑={'增' if up_increase else '减'}")
                    determined = True
                    break
            if not determined:
                log.info(f"[set_time] {name} 探测无变化")

        # 2. 计数滑动（circular=最短路径环绕，非circular=纯数值不环绕）
        cur = read_val()
        if cur >= 0:
            diff = short_diff(cur, target, mod) if circular else (target - cur)
            need_inc = diff > 0
            grids = abs(diff)
            n = round(grids / k) if k > 0 else grids
            fn = (swipe_up if (need_inc == up_increase) else swipe_down)
            log.info(f"[set_time] {name} 从{cur}→{target} {diff:+d} 需滑{n}次({'上' if fn==swipe_up else '下'})")
            for _ in range(n):
                fn(step); time.sleep(0.4)

        # 3. 验证 + 小步补救
        for _ in range(10):
            cur = read_val()
            if cur == target:
                log.info(f"[set_time] {name}={target:02d} 达成")
                return
            if cur >= 0:
                diff = short_diff(cur, target, mod) if circular else (target - cur)
                if abs(diff) <= 1:
                    log.info(f"[set_time] {name}≈{target:02d} 达成(读{cur})")
                    return
                need_inc = diff > 0
                fn = swipe_up if (need_inc == up_increase) else swipe_down
                fn(max(step // 2, 20)); time.sleep(0.5)
            else:
                swipe_up(step); time.sleep(0.5)
        log.info(f"[set_time] {name} 未精确到{target} (停在{cur})")


    def _wait_loading(self, timeout=30):
        """等待"加载中"等提示消失"""
        loading_texts = ["加载中", "正在加载", "loading", "处理中", "请稍候"]
        end = time.time() + timeout
        while time.time() < end:
            found = False
            for t in loading_texts:
                if self.d(textContains=t).exists(timeout=0.5):
                    found = True
                    break
            if not found:
                return
            time.sleep(1)

    def _do_wait_for(self, target, timeout=15):
        """智能等待：文本/图片出现才继续，超时抛异常
        target: 文本(支持逗号分隔多值任一匹配) 或 模板图片(.png)
        """
        end = time.time() + timeout
        # 图片模板
        if isinstance(target, str) and self._is_image(target):
            while time.time() < end:
                pos = self._match_template(target, timeout=1)
                if pos:
                    return
                time.sleep(1)
            raise TimeoutError(f"wait_for 超时({timeout}s)未出现: {target}")
        # 文本（逗号分隔任一匹配）
        values = [v.strip() for v in str(target).split(",")]
        while time.time() < end:
            for v in values:
                if self._find_element(v).exists(timeout=0.5):
                    return
            time.sleep(1)
        raise TimeoutError(f"wait_for 超时({timeout}s)未出现: {target}")

    def _do_split_zone(self):
        """分割房间：已在分割模式，依次尝试分区坐标直到生效"""
        zones = getattr(self, '_stored_zones', [])
        if not zones:
            raise RuntimeError("当前无可用分区")
        log.info(f"[split] {len(zones)} 个候选分区")
        for i, (cx, cy) in enumerate(zones):
            log.info(f"[split] 尝试 {i+1}/{len(zones)} 坐标({cx},{cy})")
            self.d.click(cx, cy); time.sleep(3)
            if self.d(text="分割").exists(timeout=3):
                self.d(text="分割").click(); time.sleep(3)
                self._wait_loading()
            if self.d(textContains="请选择").exists(timeout=2) or \
               self.d(textContains="区域合并").exists(timeout=2):
                log.info(f"[split] {i+1} 成功")
                return
            # 失败后重进分割模式（旧代码每次循环都 click room_part）
            self.d(textContains="区域分割").click(); time.sleep(3)
            log.info(f"[split] {i+1} 未生效，重试下一个")
        raise RuntimeError(f"尝试 {len(zones)} 次未成功")

    def _do_room_click(self, count=1):
        """点击已存储的房间分区坐标
        count<=已识别数: 点击第count个分区(1=第1个,2=第2个)，不消耗
        count>已识别数: 视为"点击剩余全部"，从下一个未点位置开始连点
        """
        zones = getattr(self, '_stored_zones', [])
        if not zones:
            raise RuntimeError("未识别到任何房间分区")
        next_idx = getattr(self, '_next_room_idx', 0)
        if count <= len(zones):
            # 按序号点单个
            if next_idx >= len(zones):
                raise RuntimeError(f"无未点击分区可用(已点到第{next_idx}个，共{len(zones)}个)，请重新 room_zones")
            cx, cy = zones[next_idx]
            self.d.click(cx, cy)
            time.sleep(2)
            self._next_room_idx = next_idx + 1
        else:
            # count 超出分区数：连点剩余全部（兼容旧语义）
            remaining = len(zones) - next_idx
            for cx, cy in zones[next_idx:]:
                self.d.click(cx, cy)
                time.sleep(2)
            self._next_room_idx = len(zones)
            msg = f"连点剩余 {remaining} 个分区(要求 {count}, 可用 {len(zones)})"
            log.info(f"[room_click] {msg}")
            self._last_compare_msg = msg
    def _do_swipe(self, direction):
        """滑动: left/right/up/down/fast-left/fast-right 或 四元组 [sx,sy,ex,ey]"""
        if isinstance(direction, list):
            self.d.swipe(*direction)
        else:
            speed = 0.3 if direction.startswith("fast-") else 0.5
            dir_name = direction.replace("fast-", "")
            # 使用 drag 方式，更可靠（swipe_ext 会受 box 限制）
            w, h = self.d.window_size()
            cx, cy = w // 2, h // 2
            dist = int(w * speed)
            if dir_name == "left":
                self.d.swipe(cx + dist, cy, cx - dist, cy)
            elif dir_name == "right":
                self.d.swipe(cx - dist, cy, cx + dist, cy)
            elif dir_name == "up":
                self.d.swipe(cx, cy + dist, cx, cy - dist)
            elif dir_name == "down":
                self.d.swipe(cx, cy - dist, cx, cy + dist)

    def _do_compare(self, baseline, threshold=0.6, inverse=False):
        """对比当前屏幕与基准图，差异>阈值则断言失败
        只比较中间区域（地图区），忽略状态栏和底部控制区

        threshold 是相似度门槛，值域为 [0,1]：
          inverse=False (compare): similarity < threshold 判失败 → 需 threshold <= 1
          inverse=True  (diff)   : similarity > threshold 判失败 → 需 threshold <  1
        越界的阈值会让断言恒真或恒假，而报告里看不出任何异常，所以直接拒绝。
        """
        if inverse and threshold >= 1:
            raise ValueError(
                f"diff 的 threshold={threshold} 非法: 相似度值域为 [0,1], "
                f"similarity > {threshold} 永不成立, 断言会恒为通过。"
                f"请改用小于 1 的值(如 0.99)"
            )
        if not inverse and threshold > 1:
            raise ValueError(
                f"compare 的 threshold={threshold} 非法: 相似度值域为 [0,1], "
                f"similarity < {threshold} 恒成立, 断言会恒为失败。"
                f"请改用不超过 1 的值"
            )
        screen = self.d.screenshot(format="opencv")
        cur = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        # 基准图与 screenshot 步骤的保存规则一致: screenshots/ 前缀补 Test_img/
        if not os.path.isabs(baseline):
            prefix = "Test_img" if baseline.startswith("screenshots/") else ""
            baseline = os.path.join(BASE_DIR, prefix, baseline)
        try:
            ref_pil = Image.open(baseline).convert("L")
            ref = np.array(ref_pil)
        except Exception as e:
            raise FileNotFoundError(f"基准图读取失败: {baseline}, {e}")
        if cur.shape != ref.shape:
            ref = cv2.resize(ref, (cur.shape[1], cur.shape[0]))
        h, w = cur.shape
        # 只比较中间区域
        r1, r2 = int(h * 0.2), int(h * 0.8)
        c1, c2 = int(w * 0.1), int(w * 0.9)
        cur = cur[r1:r2, c1:c2]
        ref = ref[r1:r2, c1:c2]
        diff = cv2.absdiff(cur, ref)
        similarity = 1 - np.count_nonzero(diff > 25) / diff.size
        msg = f"相似度 {similarity:.1%}"
        if inverse:
            # diff 模式：相似度 < 阈值（即有差异）则通过
            # 默认 0.99 = 只要变化超过 1% 就算通过
            if similarity > threshold:
                raise AssertionError(f"{msg}，几乎无变化")
        else:
            if similarity < threshold:
                raise AssertionError(f"{msg}，低于阈值 {threshold:.0%}")
        self._last_compare_msg = msg

    def _do_find_click(self, options):
        """按优先级查找文本，点击第一个存在的"""
        if isinstance(options, str):
            options = [s.strip() for s in options.split(",")]
        texts = self._get_all_texts()
        for opt in options:
            if opt in texts:
                self.d(text=opt).click()
                return
            # 也支持模板图片
            if self._is_image(opt):
                self._do_click({"click": opt})
                return
        raise RuntimeError(f"find_click 未找到任何目标: {options}")

    def _do_if(self, step, negate=False):
        """if 条件判断：条件满足跳过，不满足执行 else 分支
        文本:    if: 扫地机器人  /  if not: 扫地机器人
        数值:    if: 电量 > 50
        图片:    if: 按钮.png
        ID:      if: com.xxx:id/btn
        """
        condition = step.get("if", step.get("if not", ""))
        timeout = step.get("timeout", 5)
        passed = False

        # 数值比较: "电量 > 50"
        for op, fn in _NUM_OPS.items():
            if op in condition:
                keyword, expected = condition.split(op, 1)
                keyword = keyword.strip()
                expected = expected.strip()
                val = self._extract_value(self._get_all_nodes(), keyword)
                if val:
                    try:
                        passed = bool(fn(float(val), float(expected)))
                    except (TypeError, ValueError):
                        pass
                break
        else:
            if self._is_image(condition):
                # 有 threshold 就用图像对比，否则用 SIFT 模板匹配
                threshold = step.get("threshold")
                if threshold is not None:
                    try:
                        self._do_compare(condition, threshold)
                        passed = True
                    except AssertionError:
                        passed = False
                else:
                    passed = self._match_template(condition, timeout=timeout) is not None
            elif ":id/" in condition:
                passed = self.d(resourceId=condition).exists(timeout=timeout)
            else:
                passed = self.d(textContains=condition).exists(timeout=timeout)

        if negate:
            passed = not passed

        if passed:
            return
        else_branch = step.get("else", [])
        self._pending_sub_results = []
        for s in else_branch:
            desc = s.get("desc", self._step_desc(s))
            ss = ""
            try:
                ss = self._execute(s) or ""
                self._pending_sub_results.append({"desc": desc, "passed": True, "error": "", "screenshot": ss})
            except Exception as e:
                self._pending_sub_results.append({"desc": desc, "passed": False, "error": str(e), "screenshot": ss})
                raise
            time.sleep(self.interval)

    def _extract_value(self, texts, kw):
        """从页面文本中提取关键字的对应数值
        面积优先找'数字+㎡'，时间优先找'数字+min'
        支持模糊匹配: 面积 可匹配 清扫面积, 时间 可匹配 用时

        texts 可以是纯文本列表(按下标邻近,旧行为),也可以是 _get_all_nodes()
        的 (text, bounds) 列表。有 bounds 时优先用空间邻近——XML 文档顺序不等于
        视觉顺序,下标法在列顺序变化时会静默取到隔壁指标的数字,而且取到的值
        看起来完全正常,很难发现。
        """
        nodes = self._as_nodes(texts)
        texts = [t for t, _ in nodes]
        # 面积/时间 对应的单位关键词
        unit_map = {
            '面积': '㎡', '清扫面积': '㎡', '时间': 'min', '用时': 'min',
            '清扫时间': 'min',
        }
        prefer_unit = unit_map.get(kw, '')
        # 扩展匹配: 也尝试匹配包含该关键词的变体
        match_words = [kw]
        if kw == '面积': match_words.extend(['清扫面积'])
        if kw == '时间': match_words.extend(['用时', '清扫时间'])

        for i, (t, box) in enumerate(nodes):
            if kw not in t and not any(mw in t for mw in match_words):
                continue
            # 0. 空间邻近优先(预约时间的 HH:MM 特例仍走下标)
            if box is not None and kw != '预约时间':
                found = self._value_near_label(nodes, i, prefer_unit)
                if found is not None:
                    return found
            # ── 以下为下标回退(无 bounds,或空间未命中任何候选)──
            # 时间类关键字：找 HH:MM 模式
            if kw == '预约时间':
                for offset in range(-3, 4):
                    j = i + offset
                    if 0 <= j < len(texts):
                        m = re.match(r'^(\d{1,2}:\d{2})$', texts[j].strip())
                        if m:
                            return m.group(1)
            # 1. 优先找"数字+单位"配对（如[1]=8, [2]=㎡）
            for offset in range(-3, 4):
                j = i + offset
                if 0 <= j < len(texts) and texts[j].strip():
                    val = texts[j].strip()
                    m = re.match(r'^(\d+\.?\d*)', val)
                    if not m: continue
                    # 检查下一项是否是匹配的单位
                    nj = j + 1
                    if 0 <= nj < len(texts) and prefer_unit and prefer_unit in texts[nj]:
                        return m.group(1)
            # 2. 回退：找最近纯数字
            candidates = []
            for offset in range(-2, 3):
                j = i + offset
                if 0 <= j < len(texts) and texts[j].strip():
                    m = re.match(r'^(\d+\.?\d*)', texts[j].strip())
                    if m:
                        candidates.append((abs(offset), m.group(1)))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                return candidates[0][1]
        return None

    @staticmethod
    def _as_nodes(items):
        """把输入规范化为 [(text, bounds|None)];裸字符串视为无 bounds"""
        out = []
        for it in items:
            if isinstance(it, str):
                out.append((it, None))
            else:
                text, box = it
                out.append((text, box))
        return out

    @staticmethod
    def _value_near_label(nodes, idx, prefer_unit):
        """取标签正上方同列最近的数字,返回字符串;没有可信候选返回 None

        主页清扫数据是"数值在上、标签在下"的两列版面(8㎡ / 清扫面积),所以按
        垂直邻近 + 同列判定。同列容差取标签自身宽度(至少 60px),避免把隔壁列
        的数字算进来。带正确单位的候选优先,其次比垂直距离。
        """
        _, label_box = nodes[idx]
        lx1, ly1, lx2, ly2 = label_box
        lcx = (lx1 + lx2) / 2
        col_tol = max(60, lx2 - lx1)

        cands = []
        for j, (t, box) in enumerate(nodes):
            if j == idx or box is None:
                continue
            m = re.match(r'^(\d+\.?\d*)', t.strip())
            if not m:
                continue
            x1, y1, x2, y2 = box
            if abs((x1 + x2) / 2 - lcx) > col_tol:
                continue                      # 不在标签所在列
            vgap = ly1 - y2                   # 标签上沿 - 候选下沿
            if vgap < -10:
                continue                      # 候选不在标签上方
            ok = ActionRunner._unit_attached(nodes, j, prefer_unit)
            cands.append((0 if ok else 1, abs(vgap), m.group(1)))
        if not cands:
            return None
        cands.sort(key=lambda c: (c[0], c[1]))
        return cands[0][2]

    @staticmethod
    def _unit_attached(nodes, j, prefer_unit):
        """数字节点同一行右侧是否紧跟期望单位(如 8 ㎡)

        单位可能是上标(㎡),竖直中心和数字不完全对齐,所以行判定容差放宽到整行高。
        """
        if not prefer_unit:
            return False
        _, (x1, y1, x2, y2) = nodes[j]
        cy = (y1 + y2) / 2
        h = max(1, y2 - y1)
        for k, (t, box) in enumerate(nodes):
            if k == j or box is None or prefer_unit not in t:
                continue
            ox1, oy1, ox2, oy2 = box
            if abs((oy1 + oy2) / 2 - cy) > h:
                continue                      # 不在同一行
            gap = ox1 - x2
            if -5 <= gap <= h * 4:            # 紧邻右侧
                return True
        return False

    def _get_all_texts(self):
        """获取当前页面所有 TextView 文本"""
        xml = self.d.dump_hierarchy()
        return re.findall(r'text="([^"]*)"', xml)

    def _get_all_nodes(self):
        """获取页面所有节点的 (文本, bounds),供空间邻近匹配使用

        与 _get_all_texts 的区别是保留 bounds。不筛空文本,保证节点序列与
        _get_all_texts 的文本序列一致(下标回退路径依赖这一点)。
        """
        xml = self.d.dump_hierarchy()
        nodes = []
        for tag in re.findall(r'<node\b[^>]*>', xml):
            tm = re.search(r'\btext="([^"]*)"', tag)
            if tm is None:
                continue
            bm = re.search(r'\bbounds="\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]"', tag)
            box = tuple(int(g) for g in bm.groups()) if bm else None
            nodes.append((tm.group(1), box))
        return nodes

    # ── 工具 ──
    def _is_image(self, value):
        return value.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp'))

    def _step_desc(self, step):
        # 优先使用 desc 字段
        if isinstance(step, dict) and "desc" in step:
            return step["desc"]
        for key in ("click", "assert", "input", "wait", "screenshot", "grab", "match", "latest_record", "find_click", "if", "if not", "if_click", "back", "swipe", "room_zones", "room_click", "add_timer", "assert_switch", "switch_to", "long_click"):
            if key in step:
                val = step[key]
                if isinstance(val, dict) and "value" in val:
                    return f"{key}: {val['value']}"
                return f"{key}: {val}"
        return str(step)
