# -*- coding: utf-8 -*-
"""ActionRunner —— 执行引擎核心(源 1540 行巨无霸的瘦身版)。

只保留: 状态管理 / run_steps 主循环 / _execute 分发 / 各动作模块共享的
定位与工具方法。具体动作在 core/actions/* 里用 @action 注册。
"""
import os
import re
import time

import yaml

from core import registry as reg
import core.actions  # noqa: F401  导入即注册全部动作(见 core/actions/__init__.py)
from core.driver import BASE_DIR
from core.logger import get_logger
from core.trace import TraceRecorder
from vlm.backend import VisionRouter

log = get_logger()

POLL_SHORT, POLL_SHORT_N = 1, 3
POLL_MID, POLL_MID_N = 2, 10
POLL_MAX = 5

_NUM_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
            ">": lambda a, b: a > b, "<": lambda a, b: a < b,
            "==": lambda a, b: a == b}


class UserStopped(RuntimeError):
    pass


class XPathElement:
    """XPath 定位的轻量包装: 统一 exists/click/long_click 接口。

    从 v1.4(Appium)迁移过来的定位串大量使用 XPath,而 uiautomator2 的
    XPathSelector 与 UiObject 接口不同(exists 的参数语义、没有 info),
    这里做一层适配,让 _find_element 的调用方无感。
    """

    def __init__(self, device, xpath):
        self._d = device
        self._xpath = xpath

    def _sel(self):
        return self._d.xpath(self._xpath)

    def exists(self, timeout=1):
        try:
            return bool(self._sel().exists(float(timeout)))
        except Exception:
            return False

    def click(self):
        sel = self._sel()
        if not sel.exists(0.1):
            raise AssertionError(f"XPath 定位不到元素: {self._xpath}")
        sel.click()

    def long_click(self, duration=2.0):
        sel = self._sel()
        if not sel.exists(0.1):
            raise AssertionError(f"XPath 定位不到元素: {self._xpath}")
        sel.long_click(duration)

    @property
    def info(self):
        el = self._sel().get(timeout=1)
        b = el.bounds
        return {"bounds": {"left": b[0], "top": b[1], "right": b[2], "bottom": b[3]}}


def d_serial(d):
    try:
        return d.serial
    except Exception:
        return ""


class ActionRunner:
    # 数值比较操作符。动作模块(if 的数值分支)经 runner._NUM_OPS 访问,
    # 所以必须是类属性而非纯模块级常量。
    _NUM_OPS = _NUM_OPS

    def __init__(self, device, config: dict, case_wait=None, case_name=""):
        self.d = device
        self._device_id = getattr(device, "serial", None)
        self.case_name = case_name
        # 用例级 wait 优先,否则用全局 step_interval
        self.interval = case_wait if case_wait is not None else config.get("step_interval", 3)
        self.timeout = config.get("default_timeout", 30)
        self.click_timeout = config.get("click_timeout", 10)
        self.results = []
        self.store = {}          # grab/match 数据暂存
        self.last_click = None   # (x, y, label) —— 供步骤截图叠加点击标记
        self.stopped = False     # 停止标志,GUI 停止按钮置位
        self.on_result = None    # 结果回调(GUI 实时推送用),签名 fn(result_dict)
        self.trace = TraceRecorder(case_name, config.get("trace_limit", 8))
        self.router = VisionRouter(config)
        self._locators = self._load_locators()
        # 执行过程中动态写入的状态,集中在此初始化
        self._last_compare_msg = None
        self._pending_sub_results = None
        self._debug_map_path = None
        self._stored_zones = []
        self._stored_zone_boxes = []  # 与 _stored_zones 同序的分区外框,供合并排序
        self._next_room_idx = 0
        self._ocr = None         # ddddocr 懒加载(加载慢且非所有用例用到)

    def _load_locators(self):
        """加载定位器配置(用于 ${section.key} 引用,换 APP 时集中修改)"""
        try:
            loc_path = os.path.join(BASE_DIR, "config", "locators.yaml")
            if os.path.exists(loc_path):
                with open(loc_path, encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            log.warning(f"[locators] 加载失败: {e}")
        return {}

    # ── 等待与停止 ──
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
        """轮询间隔: 1s→2s→5s 封顶(保证刚出现的元素尽快被发现)"""
        if check_count <= POLL_SHORT_N:
            self._sleep(POLL_SHORT)
        elif check_count <= POLL_MID_N:
            self._sleep(POLL_MID)
        else:
            self._sleep(POLL_MAX)

    def stop(self):
        self.stopped = True

    # ── 设备 ──
    def _ensure_device(self):
        """操作前心跳检测,掉线自动重连(最多3次)"""
        for attempt in range(3):
            try:
                self.d.info  # 轻量调用,失败即掉线
                return
            except Exception as e:
                log.warning(f"[device] 连接异常({e}),尝试重连 {attempt+1}/3")
                if self._device_id:
                    try:
                        time.sleep(3)
                        import uiautomator2 as u2
                        self.d = u2.connect(self._device_id)
                        log.info(f"[device] 重连成功: {self._device_id}")
                    except Exception as e2:
                        log.error(f"[device] 重连失败: {e2}")
                else:
                    break

    # ── 定位器引用 ──
    def _resolve_step(self, step: dict) -> dict:
        return {k: self._resolve_str(v) if isinstance(v, str) else v
                for k, v in step.items()}

    def _resolve_str(self, s: str) -> str:
        for m in re.findall(r"\$\{([^}]+)\}", s):
            sec, _, key = m.partition(".")
            val = (self._locators.get(sec) or {}).get(key)
            if val:
                s = s.replace("${" + m + "}", str(val))
        return s

    # ── 共享工具(各动作模块经 runner 访问) ──
    def _is_image(self, value):
        return isinstance(value, str) and value.lower().endswith(
            (".png", ".jpg", ".jpeg", ".bmp"))

    def _step_desc(self, step: dict) -> str:
        desc = str(step.get("desc", "")).strip()
        if desc:
            return desc
        for key, val in step.items():
            if key in reg.ACTIONS or key in ("grab", "match", "screenshot"):
                return f"{key}:{val}" if not isinstance(val, bool) else key
        return str(step)

    def _append_result(self, result: dict):
        self.results.append(result)
        if self.on_result:
            try:
                self.on_result(result)
            except Exception as e:
                log.warning(f"[on_result] 回调异常: {e}")

    def _get_all_texts(self):
        """当前页面所有节点文本(含空串)"""
        xml = self.d.dump_hierarchy()
        return re.findall(r'text="([^"]*)"', xml)

    def _get_all_nodes(self):
        """dump_hierarchy → [(text, bounds|None)]

        两点必须与 _get_all_texts 的文本序列保持一致,否则 _extract_value 的
        下标回退路径会错位:
        · **不筛空文本** —— 筛掉空串会让下标整体偏移,取到隔壁指标的数字,
          而且取回来的值看起来完全正常
        · bounds 解析不到时为 None,调用方据此走"无 bounds"的下标回退
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

    @staticmethod
    def _as_nodes(texts):
        """纯文本列表 → (text, None) 节点(无 bounds,走下标回退)"""
        return [(t, None) for t in texts]

    def _find_element(self, value):
        # XPath(v1.4 Appium 用例迁移来的定位串): 以 // 开头或 xpath= 前缀
        if value.startswith("//") or value.startswith("xpath="):
            return XPathElement(self.d, value[6:] if value.startswith("xpath=") else value)
        # #N后缀 → 第N个匹配(如 "虚拟墙#2" 点第2个虚拟墙)
        index = None
        if "#" in value and value.split("#")[-1].isdigit():
            value, index = value.rsplit("#", 1)
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
            return self.d(textContains=value if not value.startswith("=") else value[1:])[index]
        return el

    def _click_by_locator(self, value, timeout=None):
        """按定位串点击,超时内轮询。

        两处细节都由真机踩出来:
        1. **轮询重新查找**: 界面是动态的,一次 `_find_element` 拿到的引用会过期,
           而且元素可能延迟出现 —— 必须每次重新找。
        2. **不可点击元素回退到整行**: 涂鸦是 React Native,列表项的文字节点
           本身 clickable=false,点它没反应,得点它所属的 `*_Row`。
        """
        timeout = timeout if timeout is not None else self.click_timeout
        end_time = time.time() + timeout if timeout > 0 else None
        check_count = 0
        while True:
            if end_time and time.time() >= end_time:
                raise AssertionError(f"超时({timeout}s)未找到可点击元素: {value}")
            el = self._find_element(value)
            if el.exists(timeout=1):
                try:
                    info = el.info
                    if not info.get("clickable"):
                        if self._click_row_fallback(value, info.get("text", "")):
                            return
                except Exception as e:
                    log.debug(f"[click] 行点击回退异常: {e}")
                el.click()
                return
            check_count += 1
            self._poll_sleep(check_count)

    def _click_row_fallback(self, value, text_val):
        """点不动文字节点时,改为点击包含它的整行(RN 列表行)

        在层级 XML 里找 `content-desc="*_Row"` 的子树,若搜索文本(或元素自身
        文本)落在该子树内,就点这行 —— 优先用 content-desc 定位,退而用中心坐标。
        """
        xml = self.d.dump_hierarchy()
        for rm in re.finditer(
                r'content-desc="(\w+_Row)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
            row_end = xml.find('content-desc="', rm.end())
            subtree = xml[rm.start():row_end if row_end > 0 else len(xml)]
            if value in subtree or (text_val and text_val in subtree):
                if self.d(description=rm.group(1)).exists(timeout=1):
                    self.d(description=rm.group(1)).click()
                else:
                    self.d.click((int(rm.group(2)) + int(rm.group(4))) // 2,
                                 (int(rm.group(3)) + int(rm.group(5))) // 2)
                return True
        return False

    def _click_dialog_confirm(self):
        """点弹窗的确认按钮: 文本/描述优先,落空时按屏幕等比位置兜底

        等比位置以 1080x1920 校准,换分辨率设备也能落到同一相对位置。
        """
        for how, val in (("text", "确认"), ("text", "确定"), ("desc", "Popup_Confirm")):
            el = self.d(description=val) if how == "desc" else self.d(text=val)
            if el.exists(timeout=1):
                el.click()
                log.info(f"[dialog] 点击确认按钮({val})")
                return True
        w, h = self.d.window_size()
        self.d.click(int(w * 558 / 1080), int(h * 1389 / 1920))
        log.info("[dialog] 未找到确认按钮,按等比位置点击")
        return True

    def _click_by_template(self, name, timeout=None):
        timeout = timeout if timeout is not None else self.click_timeout
        import cv2
        from core import vision
        end = time.time() + timeout
        while time.time() < end:
            screen = self.d.screenshot(format="opencv")
            pos = vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY),
                                      vision.resolve_template(name))
            if pos:
                self.d.click(*pos)
                log.info("[模板点击] %s @ (%d, %d)", name, pos[0], pos[1])
                self.last_click = (pos[0], pos[1], name)
                self._save_click_marker(screen, pos, name)
                return pos
            self._sleep(2)
        raise AssertionError(f"超时({timeout}s)未匹配到模板图: {name}")

    def _assert_template(self, name, timeout=None):
        timeout = timeout if timeout is not None else self.timeout
        import cv2
        from core import vision
        end = time.time() + timeout if timeout > 0 else None
        n = 0
        while True:
            if end and time.time() >= end:
                raise AssertionError(f"超时({timeout}s)未匹配到模板图: {name}")
            screen = self.d.screenshot(format="opencv")
            pos = vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY),
                                      vision.resolve_template(name))
            if pos:
                log.info("[模板断言] %s @ (%d, %d)", name, pos[0], pos[1])
                return pos
            n += 1
            self._poll_sleep(n)

    # ── 点击标记:每次模板点击都留「点在哪」的图像证据 ──
    @staticmethod
    def _draw_click_marker(img, x, y):
        """红圈 + 四向短线,中心即点击坐标"""
        import cv2
        x, y = int(x), int(y)
        cv2.circle(img, (x, y), 26, (0, 0, 255), 2)
        cv2.line(img, (x - 40, y), (x - 14, y), (0, 0, 255), 2)
        cv2.line(img, (x + 14, y), (x + 40, y), (0, 0, 255), 2)
        cv2.line(img, (x, y - 40), (x, y - 14), (0, 0, 255), 2)
        cv2.line(img, (x, y + 14), (x, y + 40), (0, 0, 255), 2)

    def _save_click_marker(self, frame, pos, label):
        """把点击位置标注在匹配帧上存档(报告之外的第一手证据)。

        存 reports/debug/clicks/<case>/,conftest 每轮收集前清 debug 目录,
        不会无限堆积。cv2 的 imwrite/imdecode 在 Windows 上不支持中文路径,
        统一走 imencode + open()。
        """
        try:
            import cv2
            vis = frame.copy()
            self._draw_click_marker(vis, pos[0], pos[1])
            d = os.path.join(BASE_DIR, "reports", "debug", "clicks",
                             self.case_name or "case")
            os.makedirs(d, exist_ok=True)
            safe = "".join(c for c in str(label).rsplit(".", 1)[0]
                           if c.isascii() and c not in '\\/:*?"<>|') or "tpl"
            path = os.path.join(d, "%s_%s.png" % (time.strftime("%H%M%S"), safe))
            ok, buf = cv2.imencode(".png", vis)
            if ok:
                with open(path, "wb") as f:
                    f.write(buf.tobytes())
                log.info("[点击标记] %s", path)
        except Exception:
            pass                      # 标记只是附加证据,绝不能影响执行

    def _mark_last_click_on(self, path):
        """本步骤若发生过点击,把位置标记到步骤截图上(标记一次即消费)"""
        lc = getattr(self, "last_click", None)
        if not lc or not path:
            return
        try:
            import cv2
            import numpy as np
            buf = np.fromfile(path, dtype=np.uint8)   # 中文路径须用 imdecode
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                return
            self._draw_click_marker(img, lc[0], lc[1])
            ok, out = cv2.imencode(".png", img)
            if ok:
                with open(path, "wb") as f:
                    f.write(out.tobytes())
        except Exception:
            pass
        finally:
            self.last_click = None

    def _assert_locator(self, value, timeout=None):
        """文本断言: 逗号分隔多值任一命中即通过,轮询至超时(0=无限等)"""
        timeout = timeout if timeout is not None else self.timeout
        end_time = time.time() + timeout if timeout > 0 else None
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

    # ── 主循环 ──
    def run_steps(self, steps: list) -> bool:
        """依次执行所有步骤,任一失败则中断。步骤支持 retry:N 自动重试;支持中途停止"""
        for i, step in enumerate(steps):
            if self.stopped:
                break
            step = self._resolve_step(step)  # 解析 ${section.key} 定位器引用
            desc = self._step_desc(step)
            retry = step.get("retry", 0) if isinstance(step, dict) else 0
            max_attempts = retry + 1
            succeeded = False
            last_err = None
            screenshot = ""
            self.last_click = None   # 标记只属于发生点击的当前步
            for attempt in range(max_attempts):
                if self.stopped:
                    break
                self._ensure_device()
                try:
                    screenshot = self._execute(step) or ""
                    if getattr(self, "_last_compare_msg", None):
                        desc = f"{desc} ({self._last_compare_msg})"
                        self._last_compare_msg = None
                    self._append_result({"desc": desc, "passed": True, "error": "",
                                         "screenshot": screenshot})
                    self.trace.capture(self.d, desc, True)
                    if getattr(self, "_pending_sub_results", None):
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
                        log.warning(f"[retry] 步骤「{desc}」第{attempt+1}次失败: {e},重试...")
                        self._sleep(2)
                    else:
                        # 最终失败:截图 + 诊断
                        screenshot = self.trace.failure_screenshot(self.d, i)
                        try:
                            xml = self.d.dump_hierarchy()
                            texts = [t for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]
                            log.error(f"[FAIL诊断] 步骤「{desc}」失败: {e}")
                            log.error(f"[FAIL诊断] 当前页面文本(前30): {texts[:30]}")
                            log.error(f"[FAIL诊断] 截图: {screenshot}")
                        except Exception:
                            pass
            if not succeeded:
                err_text = ("用户手动停止执行"
                            if (self.stopped or isinstance(last_err, UserStopped))
                            else str(last_err))
                self._append_result({"desc": desc, "passed": False, "error": err_text,
                                     "screenshot": screenshot})
                # 真实失败才留时序证据;用户主动停止没有诊断价值
                if not (self.stopped or isinstance(last_err, UserStopped)):
                    self.trace.capture(self.d, desc, False)
                    self.trace.dump(self.d, i)
                if getattr(self, "_pending_sub_results", None):
                    for sub in self._pending_sub_results:
                        self._append_result(sub)
                    self._pending_sub_results = None
                return False
            # 步骤间等待(可中断)。
            # 末步通常不等,但如果末步本身就是「纯等待步骤」(只有 desc + wait,
            # 没有任何动作键),这个 wait 就是它的全部意义,必须执行 —— 否则
            # 用户写的"等 60 秒"被静默丢弃。
            is_last = i >= len(steps) - 1
            has_action = any(k in reg.ACTIONS or k in ("grab", "match", "screenshot")
                             for k in step)
            if is_last and has_action:
                continue
            wait_time = step.get("wait") if isinstance(step, dict) else None
            try:
                self._sleep(wait_time if wait_time is not None
                            else (0 if is_last else self.interval))
            except UserStopped:
                self._append_result({"desc": "用户手动停止", "passed": False,
                                     "error": "用户手动停止执行", "screenshot": ""})
                return False
        if self.stopped:
            self._append_result({"desc": "用户手动停止", "passed": False,
                                 "error": "用户手动停止执行", "screenshot": ""})
            return False
        return True

    def _execute(self, step: dict):
        """动作分发入口: 主表按 priority 找第一个命中键;grab/match/screenshot 单独处理"""
        screenshot = ""
        executed_key = None
        # 1. 主操作:按优先级找第一个命中的动作键执行
        for act in reg.dispatch_order():
            if act.key in step:
                act.fn(self, step)
                executed_key = act.key
                break
        # 2. 数据抓取/对比(与主动作共存于一步)
        if "grab" in step:
            from core.actions.data_ops import do_grab
            do_grab(self, step["grab"])
        elif "match" in step:
            from core.actions.data_ops import do_match
            do_match(self, step["match"])
        # 3. 等待条件(与主动作共存)。
        # "点一下,再等某元素出现" 是很自然的写法(如 click: 安静 + wait_for: 安静,标准,强力),
        # 但主分发只执行优先级最高的一个动作,wait_for 会被静默丢掉 —— 所以这里单独补执行。
        # 独立使用时(整个步骤只有 wait_for)已在上面命中,不重复执行。
        if "wait_for" in step and executed_key != "wait_for":
            from core.actions.basic import do_wait_for
            do_wait_for(self, step)
        # 4. 截图(点击导航后页面有过场动画,先等页面落定再截 —— 否则会截到
        #    转场中的旧页面:地图编辑用例的对比基线因此截成了设置页,77.9% 假失败)
        if "screenshot" in step:
            sv = step["screenshot"]
            if isinstance(sv, str) and sv.strip():
                path = sv                       # 旧用例的手动路径, 兼容保留
            else:
                # ★ 开关模式(True): 截图进 APP 组目录(用户要求: 和用例/模板放一起)
                from core import vision as _vision
                safe_desc = re.sub(r'[\\/:*?"<>|\s]+', "_",
                                   str(step.get("desc") or ""))[:30]
                idx = len(self.results) + 1
                path = (f"Test_cases/{_vision.current_app_group() or '未分组'}/"
                        f"screenshots/{self.case_name or 'case'}/"
                        f"step{idx:02d}_{safe_desc}.png")
            # screenshots/ 开头的路径补全 Test_img/ 前缀(reports/ 等其他路径原样)
            if path.startswith("screenshots/"):
                path = os.path.join(BASE_DIR, "Test_img", path)
            elif not os.path.isabs(path):
                path = os.path.join(BASE_DIR, path)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self._sleep(1.5)
            self.d.screenshot(path)
            screenshot = path
            self._mark_last_click_on(path)
        # room_zones 的标注图优先
        if getattr(self, "_debug_map_path", None):
            screenshot = self._debug_map_path
            self._debug_map_path = None
        return screenshot
