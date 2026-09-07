import time
import re
import os
import logging
import cv2
import numpy as np
from PIL import Image
import uiautomator2 as u2

log = logging.getLogger("vacuum_test")


def d_serial(d):
    """获取设备序列号"""
    try:
        return d.serial
    except Exception:
        return ""


class ActionRunner:
    """YAML 步骤执行引擎"""

    IMAGE_DIR = "Test_img/templates"  # 模板图片固定目录

    def __init__(self, device: u2.Device, config: dict, case_wait=None):
        self.d = device
        self._device_id = getattr(device, 'serial', None)
        # 用例级 wait 优先，否则用全局 step_interval
        self.interval = case_wait if case_wait is not None else config.get("step_interval", 3)
        self.timeout = config.get("default_timeout", 30)
        self.click_timeout = config.get("click_timeout", 10)
        self.confidence = config.get("template_confidence", 0.8)
        self.results = []
        self.store = {}  # grab/match 数据暂存
        # 加载定位器配置（用于 ${section.key} 引用，换 APP 时集中修改）
        self._locators = {}
        try:
            import yaml, os
            loc_path = os.path.join("config", "locators.yaml")
            if os.path.exists(loc_path):
                with open(loc_path, encoding="utf-8") as f:
                    self._locators = yaml.safe_load(f) or {}
        except Exception as e:
            log.warning(f"[locators] 加载失败: {e}")

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
        """依次执行所有步骤，任一失败则中断。步骤支持 retry:N 自动重试"""
        for i, step in enumerate(steps):
            step = self._resolve_step(step)  # 解析 ${section.key} 定位器引用
            desc = self._step_desc(step)
            self._ensure_device()  # 操作前心跳检测+自动重连
            # retry: 失败自动重试次数（默认0=不重试）
            retry = step.get("retry", 0) if isinstance(step, dict) else 0
            max_attempts = retry + 1
            succeeded = False
            last_err = None
            for attempt in range(max_attempts):
                self._ensure_device()
                try:
                    screenshot = self._execute(step) or ""
                    if getattr(self, '_last_compare_msg', None):
                        desc = f"{desc} ({self._last_compare_msg})"
                        self._last_compare_msg = None
                    self.results.append({"desc": desc, "passed": True, "error": "", "screenshot": screenshot})
                    if getattr(self, '_pending_sub_results', None):
                        self.results.extend(self._pending_sub_results)
                        self._pending_sub_results = None
                    succeeded = True
                    break
                except Exception as e:
                    last_err = e
                    if attempt < max_attempts - 1:
                        log.warning(f"[retry] 步骤「{desc}」第{attempt+1}次失败: {e}，重试...")
                        time.sleep(2)
                    else:
                        # 最终失败：截图 + 诊断
                        screenshot = ""
                        try:
                            self.d.screenshot(f"reports/failures/step_{i:02d}_fail.png")
                            screenshot = f"reports/failures/step_{i:02d}_fail.png"
                        except Exception:
                            pass
                        try:
                            xml = self.d.dump_hierarchy()
                            texts = [t for t in re.findall(r'text="([^"]+)"', xml) if t.strip()]
                            log.error(f"[FAIL诊断] 步骤「{desc}」失败: {e}")
                            log.error(f"[FAIL诊断] 当前页面文本(前30): {texts[:30]}")
                            log.error(f"[FAIL诊断] 截图: {screenshot}")
                        except Exception:
                            pass
            if not succeeded:
                self.results.append({"desc": desc, "passed": False, "error": str(last_err), "screenshot": screenshot})
                if getattr(self, '_pending_sub_results', None):
                    self.results.extend(self._pending_sub_results)
                    self._pending_sub_results = None
                return False
            # 步骤间等待
            if i < len(steps) - 1:
                wait_time = step.get("wait") if isinstance(step, dict) else None
                time.sleep(wait_time if wait_time is not None else self.interval)
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
                path = f"Test_img/{path}"
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
                        xml = self.d.dump_hierarchy()
                        import re
                        text_val = info.get("text", "")
                        idx = xml.find(f'text="{text_val}"')
                        if idx > 0:
                            # 找包含此文本的 *_Row 子树（Row 内的标签文本和搜索文本匹配）
                            import re
                            for rm in re.finditer(
                                r'content-desc="(\w+_Row)"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml):
                                row_start = rm.start()
                                row_end = xml.find('content-desc="', rm.end())
                                if row_end < 0:
                                    row_end = len(xml)
                                subtree = xml[row_start:row_end]
                                # 检查搜索文本是否在这个 Row 的子树中
                                if value in subtree or (text_val and text_val in subtree):
                                    if self.d(description=rm.group(1)).exists(timeout=1):
                                        self.d(description=rm.group(1)).click()
                                    else:
                                        row_cx = (int(rm.group(2)) + int(rm.group(4))) // 2
                                        row_cy = (int(rm.group(3)) + int(rm.group(5))) // 2
                                        self.d.click(row_cx, row_cy)
                                    return
                except Exception as e:
                    print(f"[DEBUG] 异常: {e}")
                el.click()
                return
            check_count += 1
            if check_count <= 10:
                time.sleep(2)
            else:
                time.sleep(30)

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
            if check_count <= 10:
                time.sleep(2)
            else:
                time.sleep(30)

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
            raise TimeoutError(f"click 超时({self.click_timeout}s)模板匹配失败: {img_name}")
        self.d.click(pos[0], pos[1])

    def _assert_template(self, img_name, timeout=None):
        timeout = timeout if timeout is not None else self.timeout
        pos = self._match_template(img_name, timeout)
        if pos is None:
            raise AssertionError(f"超时({timeout}s)图片未找到: {img_name}")

    def _match_template(self, img_name, timeout=None, min_matches=4):
        """SIFT 特征点匹配定位图标
        - 只比图标的特征点(角点/边缘/轮廓)，不比背景像素
        - 小模板自动放大 3x，保证 SIFT 有足够像素
        - 中位数定位 + 离群点过滤，抗假阳性
        """
        timeout = timeout if timeout is not None else self.timeout
        img_path = f"{self.IMAGE_DIR}/{img_name}"
        end_time = time.time() + timeout if timeout > 0 else None
        check_count = 0

        # SIFT 检测器（降低门槛以适配简单小图标）
        sift = cv2.SIFT_create(
            contrastThreshold=0.01,
            edgeThreshold=5,
            nOctaveLayers=5,
        )

        while True:
            if end_time and time.time() >= end_time:
                return None

            screen = self.d.screenshot(format="opencv")
            screen_gray = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
            pos = self._find_sift(screen_gray, img_path, sift, min_matches)
            if pos is not None:
                return pos

            check_count += 1
            if check_count <= 10:
                time.sleep(2)
            else:
                time.sleep(30)

    def _find_sift(self, screenshot_gray, template_path, sift, min_matches=4, ratio=0.7):
        """SIFT 特征点匹配定位图标"""
        # 1. 读取模板，小图自动放大
        pil_img = Image.open(template_path).convert("L")
        tpl = np.array(pil_img)
        h, w = tpl.shape
        if w < 100 or h < 100:
            tpl = cv2.resize(tpl, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)

        # 2. SIFT 检测特征点
        kp1, des1 = sift.detectAndCompute(tpl, None)
        kp2, des2 = sift.detectAndCompute(screenshot_gray, None)

        if des1 is None or des2 is None or len(des1) < 2 or len(des2) < 2:
            return None

        # 3. BFMatcher + Lowe ratio test
        bf = cv2.BFMatcher()
        try:
            raw_matches = bf.knnMatch(des1, des2, k=2)
        except cv2.error:
            return None

        good = [m for m, n in raw_matches if m.distance < ratio * n.distance]

        if len(good) < min_matches:
            return None

        # 4. 中位数定位 + 离群点过滤（< 40px）
        pts = np.array([kp2[m.trainIdx].pt for m in good])
        median = np.median(pts, axis=0)
        dists = np.linalg.norm(pts - median, axis=1)
        inliers = pts[dists < 40]
        if len(inliers) < min_matches:
            return None

        mean = np.mean(inliers, axis=0).astype(int)
        return (int(mean[0]), int(mean[1]))

    # ── 抓取/对比数据 ──
    def _do_grab(self, keywords):
        """抓取页面上包含关键字的文本及相邻数字，存入 self.store"""
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]
        texts = self._get_all_texts()
        results = []
        for kw in keywords:
            val = self._extract_value(texts, kw)
            if val is None:
                raise RuntimeError(f"grab 未找到 '{kw}' 的数据")
            self.store[kw] = val
            results.append(f"{kw}={val}")
        self._last_compare_msg = "主页: " + ", ".join(results)

    def _do_match(self, keywords):
        """对比当前页面数据与 grab 存储的数据（数值允许 ±1 浮动）"""
        if isinstance(keywords, str):
            keywords = [k.strip() for k in keywords.split(",")]
        texts = self._get_all_texts()
        mismatches = []
        match_results = []
        for kw in keywords:
            stored = self.store.get(kw, "")
            found = self._extract_value(texts, kw)
            # 预约时间特殊处理：从层级中取第一个非状态栏(y>80)的 HH:MM
            if found is None and kw == "预约时间":
                import re as _re2
                xml = self.d.dump_hierarchy()
                items = _re2.findall(r'text="(?:\d{4}-\d{2}-\d{2} )?(\d{1,2}:\d{2})"[^>]*bounds="\[\d+,(\d+)\]', xml)
                times = [t for t, y in items if int(y) > 80]
                if times:
                    found = times[0]
            match_results.append(f"{kw}={found}")
            if found is not None and found != stored:
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
        # 判断当前状态（内部会缓存开关位置到 self._last_switch_pos）
        is_on = self._get_switch_state(area, switch_tpl, switch_label)
        if is_on == expect_on:
            log.info(f"[switch] 已是{'打开' if expect_on else '关闭'}状态，跳过")
            self._last_compare_msg = f"开关={'打开' if expect_on else '关闭'}(无需切换)"
            return
        # 用 _get_switch_state 已定位的开关位置点击
        pos = getattr(self, '_last_switch_pos', None)
        if pos is None:
            raise RuntimeError("无法定位开关位置")
        self.d.click(pos[0], pos[1])
        log.info(f"[switch] 点击开关({pos[0]},{pos[1]})")
        time.sleep(3)
        # 验证
        is_on = self._get_switch_state(area, switch_tpl, switch_label)
        if is_on == expect_on:
            log.info(f"[switch] 切换成功→{'打开' if expect_on else '关闭'}")
            self._last_compare_msg = f"开关={'打开' if expect_on else '关闭'}"
        else:
            raise AssertionError(f"开关切换失败，当前{'打开' if is_on else '关闭'}")

    def _get_switch_state(self, area=None, switch_tpl=None, switch_label=None):
        """获取开关状态：优先颜色检测
        switch_tpl: 用于定位开关的模板图片名（默认 勿扰打开.png）
        area: 可选 [x1,y1,x2,y2] 直接指定开关区域
        switch_label: 可选，通过同行文本标签（如"定制模式"）自动定位开关
        """
        screen = self.d.screenshot(format="opencv")
        if area:
            x1, y1, x2, y2 = area
            crop = screen[y1:y2, x1:x2]
            self._last_switch_pos = ((x1 + x2) // 2, (y1 + y2) // 2)
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
            crop = screen[y1:y2, x1:x2]
            self._last_switch_pos = ((x1 + x2) // 2, (y1 + y2) // 2)
        else:
            tpl_name = switch_tpl or "勿扰打开.png"
            tpl_path = f"{self.IMAGE_DIR}/{tpl_name}"
            from PIL import Image as _PI
            tpl = np.array(_PI.open(tpl_path).convert("L"))
            h, w = tpl.shape
            pos = None
            # 只在右半屏搜索（开关始终在右侧，避免左侧UI误匹配）
            screen_w = screen.shape[1]
            half = screen_w // 2
            right_screen = screen[:, half:, :]
            right_gray = cv2.cvtColor(right_screen, cv2.COLOR_BGR2GRAY)
            sift = cv2.SIFT_create(contrastThreshold=0.01, edgeThreshold=5, nOctaveLayers=5)
            for _ in range(2):
                pos = self._find_sift(right_gray, tpl_path, sift, min_matches=3)
                if pos: pos = (pos[0] + half, pos[1]); break
                time.sleep(0.3)
                screen = self.d.screenshot(format="opencv")
                right_screen = screen[:, half:, :]
                right_gray = cv2.cvtColor(right_screen, cv2.COLOR_BGR2GRAY)
            if not pos:
                r = cv2.matchTemplate(right_gray, tpl, cv2.TM_CCORR_NORMED)
                _, v, _, loc = cv2.minMaxLoc(r)
                if v >= 0.7: pos = (loc[0] + half + w//2, loc[1] + h//2)
            if not pos: raise AssertionError(f"未定位到开关模板: {tpl_name}")
            self._last_switch_pos = pos
            # 以匹配位为中心扩散 2x 采样颜色（3x会稀释蓝色信号导致ON误判为OFF）
            x1 = max(0, pos[0] - w)
            y1 = max(0, pos[1] - h)
            x2 = min(screen.shape[1], pos[0] + w)
            y2 = min(screen.shape[0], pos[1] + h)
            crop = screen[y1:y2, x1:x2]
        # 蓝色通道占比：打开=蓝色>0.34，关闭=灰色≈0.33
        bgr_mean = np.mean(crop, axis=(0, 1))
        blue_ratio = bgr_mean[0] / (bgr_mean[0] + bgr_mean[1] + bgr_mean[2] + 1)
        return blue_ratio > 0.34

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
        is_on = mean_sat > 30  # 均值饱和度>30=有色=打开
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
        import re

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
        for pk in peaks:
            hue_mask = (h > pk - 8) & (h < pk + 8) & color_mask
            kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
            hue_mask = cv2.morphologyEx(hue_mask.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
            contours, _ = cv2.findContours(hue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            mask_all = mask_all | hue_mask
            for c in contours:
                area = cv2.contourArea(c)
                if area > roi_area * 0.001:
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = int(M["m10"] / M["m00"])
                        cy = int(M["m01"] / M["m00"])
                        # 向下偏移避开房间名字（名字通常在上方）
                        cy += int((y2 - y1) * 0.03)
                        zones.append((cx + x1, cy + y1, area))
        zones.sort(key=lambda x: -x[2])
        # 去重：合并中心距离过近的分区（同一房间被拆成多块）
        merged = []
        min_dist = min((x2 - x1), (y2 - y1)) * 0.15  # 距离阈值=区域尺寸的15%
        for z in zones:
            zx, zy, _ = z
            too_close = False
            for mx, my, _ in merged:
                if abs(zx - mx) < min_dist and abs(zy - my) < min_dist:
                    too_close = True
                    break
            if not too_close:
                merged.append(z)
        zones = merged
        import os
        os.makedirs("reports/debug", exist_ok=True)
        # 标注检测区域（红框）+ 分区中心（绿点）
        debug = screen.copy()
        cv2.rectangle(debug, (x1, y1), (x2, y2), (0, 0, 255), 3)
        for cx, cy, _ in zones[:8]:
            cv2.circle(debug, (cx, cy), 10, (0, 255, 0), -1)
        # 用 PIL 保存，支持中文路径
        import os
        os.makedirs("reports/debug", exist_ok=True)
        Image.fromarray(cv2.cvtColor(screen, cv2.COLOR_BGR2RGB)).save("reports/debug/map_screen.png")
        Image.fromarray(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)).save("reports/debug/map_roi.png")
        Image.fromarray(mask_all).save("reports/debug/map_mask.png")
        path = f"reports/debug/map_{label}.png"
        Image.fromarray(cv2.cvtColor(debug, cv2.COLOR_BGR2RGB)).save(path)
        self._debug_map_path = path
        if not zones:
            raise RuntimeError(f"未识别到房间分区 (contours={len(contours)}, mask={cv2.countNonZero(mask)})")
        # 只存储，不点击
        self._stored_zones = [(cx, cy) for cx, cy, _ in zones[:8]]
        self._next_room_idx = 0  # 重置点击游标
        msg = f"识别到 {len(self._stored_zones)} 个房间分区"
        log.info(f"[room_zones] {msg}")
        self._last_compare_msg = msg

    def _do_merge_zones(self):
        """合并房间：已在合并模式，依次尝试相邻分区对直到生效"""
        zones = getattr(self, '_stored_zones', [])
        if len(zones) < 2:
            raise RuntimeError(f"合并需要至少 2 个分区，当前仅 {len(zones)} 个")
        log.info(f"[merge] {len(zones)} 个分区")
        for i in range(len(zones)):
            for j in range(i + 1, len(zones)):
                log.info(f"[merge] 尝试对 ({i},{j})")
                self.d.click(*zones[i]); time.sleep(2)
                self.d.click(*zones[j]); time.sleep(2)
                if self.d(text="合并").exists(timeout=2):
                    self.d(text="合并").click(); time.sleep(3)
                    self._wait_loading()
                if self.d(textContains="提示").exists(timeout=2):
                    self.d.click(558, 1389); time.sleep(2)
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
        import subprocess, ddddocr
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
        if not hasattr(self, '_ocr'):
            self._ocr = ddddocr.DdddOcr(show_ad=False)

        # 4. 自动识别滚轮区域（DatePicker_ 或 Timer_TimerPicker_）
        hour_b = self._wheel_bounds(self._find_wheel_desc("Hour"))
        min_b = self._wheel_bounds(self._find_wheel_desc("Minute"))
        if hour_b and min_b:
            hx = (hour_b["left"] + hour_b["right"]) // 2
            mx = (min_b["left"] + min_b["right"]) // 2
            h_w = (hour_b["right"] - hour_b["left"]) // 4
            m_w = (min_b["right"] - min_b["left"]) // 8
        else:
            hx, mx, h_w, m_w = 276, 810, 110, 28
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
        import cv2 as _cv2
        from PIL import Image as _Image
        if not hasattr(self, '_ocr'):
            import ddddocr
            self._ocr = ddddocr.DdddOcr(show_ad=False)
        b = self._wheel_bounds(self._find_wheel_desc(name.capitalize()))
        cy = (b["top"] + b["bottom"]) // 2 if b else 1525
        y1, y2 = cy - 35, cy + 35

        def read_val():
            screen = self.d.screenshot(format="opencv")
            crop = screen[y1:y2, cx-crop_w:cx+crop_w]
            crop = _cv2.resize(crop, (crop.shape[1]*2, crop.shape[0]*2),
                               interpolation=_cv2.INTER_CUBIC)
            gray = _cv2.cvtColor(crop, _cv2.COLOR_BGR2GRAY)
            raw = self._ocr.classification(_Image.fromarray(gray)).strip()
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
            for cx, cy in zones[next_idx:]:
                self.d.click(cx, cy)
                time.sleep(2)
            self._next_room_idx = len(zones)
            log.info(f"[WARNING] {msg}")
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
        只比较中间区域（地图区），忽略状态栏和底部控制区"""
        screen = self.d.screenshot(format="opencv")
        cur = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
        try:
            from PIL import Image
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
            # 默认 0.95 = 只要变化超过 5% 就算通过
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
        for op in (">=", "<=", ">", "<", "=="):
            if op in condition:
                keyword, expected = condition.split(op, 1)
                keyword = keyword.strip()
                expected = expected.strip()
                val = self._extract_value(self._get_all_texts(), keyword)
                if val:
                    try:
                        passed = eval(f"float({val}) {op} float({expected})")
                    except Exception:
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
        """从文本列表中提取关键字的对应数值
        面积优先找'数字+㎡'，时间优先找'数字+min'
        支持模糊匹配: 面积 可匹配 清扫面积, 时间 可匹配 用时
        """
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

        for i, t in enumerate(texts):
            if kw not in t and not any(mw in t for mw in match_words):
                continue
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

    def _get_all_texts(self):
        """获取当前页面所有 TextView 文本"""
        xml = self.d.dump_hierarchy()
        return re.findall(r'text="([^"]*)"', xml)

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
