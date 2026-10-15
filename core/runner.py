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
from core import ocr
import core.actions  # noqa: F401  导入即注册全部动作(见 core/actions/__init__.py)
from core.driver import BASE_DIR, split_texts
from core.session import (text_present, text_present_detail, warm_webview,  # noqa: E501  判断统一入口 + WebView 预热
                          scan_status_words, set_status_words, FaultStateError)
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

    def __init__(self, device, config: dict, case_wait=None, case_name="",
                 device_name="", cancel_check=None, click_shots=True):
        self.d = device
        self._device_id = getattr(device, "serial", None)
        self.case_name = case_name
        # ★ 故障/空闲哨兵词表: 用 config runner 段的 fault_texts / idle_texts 覆盖
        #   (不配则用内置默认, 见 core/session.py)
        set_status_words(config.get("fault_texts"), config.get("idle_texts"),
                         fault_streak=config.get("fault_streak"),
                         idle_streak=config.get("idle_streak"))
        #: 点击类动作是否**强制截图**(把点击位置标在图上、写进报告)。
        #  ★ 只对**用例步骤**开(用户要求 2026-10-11: "测试步骤的点击操作才需要截图");
        #    前置条件里的自定义步骤属于准备动作, 不在这里留证据。
        self.click_shots = click_shots
        # ★ True = 每一步都留截图(前置的自定义步骤用): 以前只有点击类/显式 screenshot
        #   才截, wait/assert/if 这类子步骤在"前置也默认截图"里缺席(用户 2026-10-13)
        self.always_screenshot = False
        # ★ 点击前证据截图(用户 2026-10-13): mark_click 拍好并存路径,
        #   _execute 的后置截图块见到它就让路(每步最多一张, 见 mark_click)
        self._pre_click_shot = None
        self._cur_step = None          # 当前步骤(点击前截图判断"显式 screenshot"用)
        self._cur_step_desc = ""
        # 设备页名称(配置里的 target_device): 插件页整页文本读不出来时, 用它重进设备页
        self.device_name = device_name
        # 用例级 wait 优先,否则用全局 step_interval
        self.interval = case_wait if case_wait is not None else config.get("step_interval", 3)
        self.timeout = config.get("default_timeout", 30)
        self.click_timeout = config.get("click_timeout", 10)
        self.results = []
        self.store = {}          # grab/match 数据暂存
        self.last_click = None   # (x, y, label) —— 供步骤截图叠加点击标记
        #: 截图子目录名(worker 传"用例名-第N次", 让多轮截图互不覆盖); 空则用 case_name
        self.shot_subdir = ""
        self._stopped = False    # 停止标志,GUI 停止按钮置位(对外经 stopped 属性读)
        self._paused = False     # 暂停标志,GUI 暂停按钮置位(2026-10-13)
        # ★ 外部取消信号(如前置阶段的 should_cancel 回调): 与自身停止标志等效。
        #   前置里的「自定义步骤」会新建一个 ActionRunner, 它拿不到主 runner 的
        #   stop() —— 于是用户点停止后那串前置会一路跑完(实测: 4s 的等待步骤在
        #   0.5s 请求停止后仍跑满 4.0s)。用回调把取消意图穿透进来。
        self._cancel_check = cancel_check
        self.on_result = None    # 结果回调(GUI 实时推送用),签名 fn(result_dict)
        #: 步骤**开始**回调: fn(desc, attempt) —— GUI 靠它先插一行「执行中」,
        #  跑完再由 on_result 就地改成 PASS/FAIL(用户要求 2026-10-12)
        self.on_step_start = None
        #: 暂停状态回调(2026-10-15): 挂起/恢复时通知界面把当前步骤行标「⏸ 暂停中」
        self.on_pause_state = None
        self._sub_seq = 0        # 子步骤行序号(界面按它在原地更新嵌套容器的行)
        self._if_depth = 0       # if 嵌套深度: 只有**最外层** if 结束后统一落账
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
        """可中断等待: 收到停止请求后快速退出并抛 UserStopped。

        ★ 暂停感知(2026-10-13): 分片里若处于暂停, 挂起等待 resume, 且**挂起时长
          不计入等待** —— 返回挂起秒数, 调用方的 timeout 据此顺延(用户直觉: 暂停
          10 分钟回来继续等剩下的)。停止优先于暂停(挂起中点停止立即抛)。
        """
        end = time.time() + max(0.0, seconds)
        paused_total = 0.0
        while not self.stopped:
            remain = end - time.time()
            if remain <= 0:
                return paused_total
            time.sleep(min(0.2, remain))
            if self._paused:
                p = self._wait_if_paused()
                paused_total += p
                end += p            # 挂起不计入等待: timeout 顺延
        raise UserStopped("用户手动停止")

    def _poll_sleep(self, check_count):
        """轮询间隔: 1s→2s→5s 封顶(保证刚出现的元素尽快被发现)。
        返回挂起秒数(调用方的 timeout 据此顺延, 见 _sleep)"""
        paused = 0.0
        if check_count <= POLL_SHORT_N:
            paused = self._sleep(POLL_SHORT)
        elif check_count <= POLL_MID_N:
            paused = self._sleep(POLL_MID)
        else:
            paused = self._sleep(POLL_MAX)
        return paused or 0.0

    def _poll_until(self, checks, pred):
        """按节奏探测 `checks` 次, `pred(i)` 真值即成立(审查 2026-10-12 #21)。

        if 条件的四类检测(数值/图片阈值/模板/ID)共用这一个循环 —— 以前是四段
        近乎复制的 for+break+_poll_sleep, 已经漂移出过 bug(模板分支丢了
        resolve_template)。间隔走 _poll_sleep 阶梯, 末次已定局不再白睡。
        """
        for i in range(1, checks + 1):
            if pred(i):
                return True
            if i < checks:
                self._poll_sleep(i)
        return False

    @property
    def stopped(self):
        """停止标志: 自身标志 **或** 外部取消信号(cancel_check)任一为真。

        做成属性是为了让"停止"能穿透到前置里新建的 runner —— 它们有自己的
        实例状态, 拿不到主 runner 的 stop()。
        """
        return self._stopped or bool(self._cancel_check and self._cancel_check())

    @stopped.setter
    def stopped(self, value):
        self._stopped = bool(value)

    def stop(self):
        self.stopped = True

    # ── 暂停/继续(用户 2026-10-13: "支持测试用例暂停再继续") ──
    @property
    def paused(self):
        return self._paused

    def pause(self):
        """请求暂停: 当前步骤跑完后, 在步骤间/等待分片里挂起(不碰设备)"""
        self._paused = True

    def resume(self):
        self._paused = False

    def _wait_if_paused(self):
        """挂起点: 暂停请求到达后阻塞在这里, 直到 resume 或 stop。

        返回挂起了多少秒(0 = 没挂起) —— 调用方据此**顺延 timeout**(用户直觉:
        暂停 10 分钟回来, 继续等剩下的, 而不是恢复即超时)。
        ★ 停止优先于暂停: 挂起中收到停止请求 → 抛 UserStopped(同正常等待路径)。
        ★ 设备侧不碰: 暂停的真实场景是"机器人卡住要人工处理" —— 设备保持现状。
        """
        if not self._paused:
            return 0.0
        self._notify_pause(True)
        t0 = time.time()
        try:
            while self._paused:
                if self.stopped:
                    raise UserStopped("用户手动停止")
                time.sleep(0.2)
        finally:
            self._notify_pause(False)     # 恢复(或抛错)都要还原界面状态
        return time.time() - t0

    def _notify_pause(self, paused):
        """通知界面暂停状态(用户 2026-10-15: "点击暂停时用例步骤需要显示暂停中")"""
        cb = getattr(self, "on_pause_state", None)
        if cb:
            try:
                cb(paused)
            except Exception as e:        # 界面回调异常不能影响执行
                log.debug(f"[on_pause_state] 回调异常: {e}")

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

    def _emit_sub(self, sub: dict):
        """if 子步骤结果**即时**推 GUI(用户 2026-10-15: "子步骤在 if 全部结束后才
        显示出来") —— 只推回调不入 results(落账统一在分支结束时做, 重试切换分支
        时 pending 清空, 不会留幽灵行)。"""
        if self.on_result:
            try:
                self.on_result(sub)
            except Exception as e:
                log.warning(f"[on_result] 回调异常: {e}")

    def _append_result(self, result: dict, fire=True):
        """结果落账; fire=False = 只入 results 不推 GUI(if 子步骤已流式推过, 见下)"""
        self.results.append(result)
        if fire and self.on_result:
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
        # ★ 空值必须**明确报错**(2026-10-11 真机实测): `d(textContains="")` 会匹配到
        #   **任意**元素(exists=True) —— 于是"空的文本断言"不检测就通过、"空的点击"
        #   点到随机位置。宁可报错也不能静默放过(用户实测: 连续两个断言, 第二个没检测
        #   成功却通过了, 根因就是它的值空了)。
        #   ★ 放在最前面: 手写 YAML 里的 `assert:`(后面什么都没写)解析出来是 **None**,
        #   排在 startswith 之后的话会先撞 AttributeError, 用户看到的是一句看不懂的崩栈。
        if not str(value or "").strip():
            raise ValueError("定位串为空: 请填断言文本 / 模板名 / x,y 坐标")
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

    def _text_present(self, *texts, timeout=None, round_no=None, rounds=None):
        """判断类统一入口(转发 session.text_present): 原生优先 → 树稀疏时 OCR 兜底。

        ★ 插件页(WebView)切视图后主体不再提供可访问节点, 只读无障碍树的判断
          (if 条件/wait_for/wait_loading…)会集体判错 —— 见 core/ocr.py 注释。
        timeout: 给了就轮询等待(等价原 `d(textContains=..).exists(timeout=..)`)
        round_no: 调用方自己的轮次计数(从 1 起); 不给就用本函数循环的计数。
                  OCR 兜底只在第 5/10/15… 轮触发(见 ocr.OCR_EVERY_N_ROUNDS)。
        rounds: 给了就**按次数**轮询(查这么多次就收手, 不看时间) —— if 条件用它实现
                "两轮 × 每轮五次"的固定节奏(用户 2026-10-12 定)。
        """
        wanted = [t for t in texts if t]
        if not wanted:
            return False
        end = time.time() + timeout if timeout else None
        n = 0
        while True:
            n += 1
            if rounds is not None:
                # ★ 按**次数**轮询(if 条件的"两轮×五次"): 查够就收手, 不看时间。
                #   (2.0.7 曾把"查够"的判断放在 end 之后, 而 rounds 模式 end=None ——
                #    首次失败即 return False, 文本条件实际只查 1 次, 审查 2026-10-12 抓到)
                if n > rounds:
                    return False
                rn = n          # OCR 兜底落在第 5/10/15… 次(与其它条件同一节奏)
            else:
                # 调用方给了轮次就用它的(断言/等待各自计数); 自己在**轮询**时用自己的
                # 计数; **单次判断**(没给 timeout, 只查一次)不算轮询 —— 那种场合 OCR 是
                # 唯一机会, 传 None 让它不受"5 轮一次"的限制(否则插件页永远用不上)
                if round_no is not None:
                    rn = round_no
                elif end is not None:
                    rn = n
                else:
                    rn = None
            if text_present(self.d, wanted, round_no=rn):
                return True
            if rounds is not None:
                if n < rounds:
                    self._poll_sleep(n)   # 与其它条件同一阶梯(1/2/5s); 末次不再白睡
                continue
            if end is None or time.time() >= end:
                return False
            end += self._sleep(1) or 0    # 暂停挂起 → timeout 顺延(2026-10-13)

    def _click_by_locator(self, value, timeout=None):
        """按定位串点击,超时内轮询。

        两处细节都由真机踩出来:
        1. **轮询重新查找**: 界面是动态的,一次 `_find_element` 拿到的引用会过期,
           而且元素可能延迟出现 —— 必须每次重新找。
        2. **不可点击元素回退到整行**: 涂鸦是 React Native,列表项的文字节点
           本身 clickable=false,点它没反应,得点它所属的 `*_Row`。
        3. **OCR 兜底点击坐标(用户 2026-10-13)**: 插件页切视图时无障碍树暂态空
           (实测 _find_element 一直 exists=False 直到 10s 超时), 原生找不到 ——
           切到 OCR 找文本行坐标直接 click。timeout 内每隔 2s 尝试一次, 命中即点,
           点不到再说。
        """
        timeout = timeout if timeout is not None else self.click_timeout
        end_time = time.time() + timeout if timeout > 0 else None
        check_count = 0
        ocr_last = 0.0
        while True:
            if end_time and time.time() >= end_time:
                raise AssertionError(f"超时({timeout}s)未找到可点击元素: {value}")
            warm_webview(self.d)     # ★ 同上: WebView 文本预热(见 _assert_locator)
            el = self._find_element(value)
            if el.exists(timeout=1):
                if not self._element_visible(el):
                    # 隐藏/未渲染的节点点不动(点了也没效果) → 继续轮询等它真的出现
                    log.info(f"[点击] 「{value}」当前是未渲染的隐藏节点, 继续等待: "
                             f"{self._element_desc(el)}")
                    check_count += 1
                    if end_time is not None:      # timeout=0(无限等)时 end_time 是 None
                        end_time += self._poll_sleep(check_count) or 0
                    continue
                try:
                    info = el.info
                    if not info.get("clickable"):
                        if self._click_row_fallback(value, info.get("text", "")):
                            return
                except Exception as e:
                    log.debug(f"[click] 行点击回退异常: {e}")
                self.mark_click(self._center_xy(el, value))   # ★ 点击前截图标注
                el.click()
                # ★ 点击必须留痕(写清点了谁、落在哪): 真机排查"点不动/点错地方"时,
                #   这是唯一能拿到的第一手信息(模板点击一直有, 文本/坐标以前是空白)
                log.info(f"[点击] 文本「{value}」 @ {self._center_of(el)}")
                return
            # ★ 原生找不到, 每 2s 试一次 OCR 兜底(2026-10-13): 插件页切视图
            #   树暂态空, OCR 此时能拿到目标文本的真实坐标 → 直接 d.click(x, y)
            if ocr.available() and time.time() - ocr_last >= 2.0:
                ocr_last = time.time()
                hit, pos = self._ocr_find_xy(value)
                if hit and pos:
                    self.mark_click((pos[0], pos[1]), f"OCR: {value}")
                    self.d.click(pos[0], pos[1])
                    log.info(f"[点击] OCR 兜底命中「{value}」 @ ({pos[0]},{pos[1]})")
                    return
            check_count += 1
            if end_time is not None:              # timeout=0(无限等): 无 deadline 可顺延
                end_time += self._poll_sleep(check_count) or 0

    def _ocr_find_xy(self, value):
        """OCR 全文扫描, 找含 value 的那行, 返回 (value, (x, y)) / (None, None)。"""
        from core import ocr as _ocr
        for line, pos in _ocr.screen_texts(self.d):
            if value and value in line:
                return value, pos
        return None, None

    @staticmethod
    def _element_visible(el):
        """命中的元素是否**真的渲染了**(bounds 非全 0)。

        ★ 与 session.visible_nodes 同一判据: 收起/隐藏的控件仍留在无障碍树里, 拿它当
          "命中"就是假通过 —— 2026-10-11 真机实测: `断言: 干燥拖布,已完成,已取消` 通过,
          而截图页面上只有「正在清洁」, 命中的是收起面板留下的隐藏节点。
        读不到 bounds 时**当可见**(宁可不误杀)。
        """
        try:
            b = (el.info or {}).get("bounds") or {}
            return not (int(b.get("left", 0)) == 0 and int(b.get("right", 0)) == 0
                        and int(b.get("top", 0)) == 0 and int(b.get("bottom", 0)) == 0)
        except Exception:
            return True

    @staticmethod
    def _center_xy(el, label):
        """(x, y, label) —— 元素中心坐标; 取不到就返回 None(不写标注)"""
        try:
            b = el.info["bounds"]
            return ((b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2, label)
        except Exception:
            return None

    @staticmethod
    def _center_of(el):
        """元素的中心坐标(拿不到就写"位置未知") —— 只用于日志, 不能因它出错"""
        try:
            b = el.info["bounds"]
            return f"({(b['left'] + b['right']) // 2},{(b['top'] + b['bottom']) // 2})"
        except Exception:
            return "位置未知"

    @staticmethod
    def _element_desc(el):
        """命中元素的"长什么样"(类名/文本/描述/坐标) —— 排查**断言假通过**就看这一行。

        ★ 为什么必须记: 断言是 `textContains` + content-desc 兜底, 页面上**按钮的
          无障碍描述**同样算命中。真机上出现过「断言: 暂停」瞬间通过 —— 其实匹配到的
          是暂停**图标按钮**的描述, 并不是真的暂停了(2026-10-11)。只记查询值的话,
          这种假通过永远看不出来。
        """
        try:
            i = el.info or {}
            b = i.get("bounds") or {}
            pos = (f"@ ({(b['left'] + b['right']) // 2},{(b['top'] + b['bottom']) // 2})"
                   if b else "")
            desc = i.get("contentDescription") or i.get("content-desc") or ""
            return (f"元素[{(i.get('className') or '?').split('.')[-1]}] "
                    f"text={i.get('text') or ''!r} desc={desc!r} {pos}").strip()
        except Exception:
            return "元素信息读不到"

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
                center = ((int(rm.group(2)) + int(rm.group(4))) // 2,
                          (int(rm.group(3)) + int(rm.group(5))) // 2)
                self.mark_click(center, value)   # ★ 点击前截图标注(2026-10-13)
                if self.d(description=rm.group(1)).exists(timeout=1):
                    self.d(description=rm.group(1)).click()
                else:
                    self.d.click(*center)
                log.info(f"[点击] 文本「{value}」点不动 → 点它所在的整行({rm.group(1)})")
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
                self.mark_click(pos, name)      # ★ 点击前截图标注(2026-10-13)
                self.d.click(*pos)
                log.info("[模板点击] %s @ (%d, %d)", name, pos[0], pos[1])
                # ★ 不再另存一份 Test_img/debug/clicks/ 标记图(用户 2026-10-15:
                #   "把点击的标注图直接替换步骤截图统一放在测试步骤截图的目录中")
                #   —— 带标记的步骤截图就是唯一证据, 同一次运行的所有图都在一处。
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

    # ★ Test_img/debug/clicks/ 那份重复标记图已删除(用户 2026-10-15:
    #   "把点击的标注图直接替换步骤截图统一放在测试步骤截图的目录中")
    #   —— 带标记的步骤截图是唯一证据,见 _mark_last_click_on()。

    def _annotate_shot(self, path, draw):
        """在已落盘的截图上作标注(中文路径安全读写); 失败不影响证据本身"""
        try:
            import cv2
            import numpy as np
            buf = np.fromfile(path, dtype=np.uint8)   # 中文路径须用 imdecode
            img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
            if img is None:
                return
            draw(img)
            ok, out = cv2.imencode(".png", img)
            if ok:
                with open(path, "wb") as f:
                    f.write(out.tobytes())
        except Exception:
            pass

    def _draw_swipe_marker(self, img, sx, sy, ex, ey):
        """滑动路径: 起点**实心圆**(出发) + 青色**箭头线**指向终点(区分点击的红圈)"""
        import cv2
        sx, sy, ex, ey = int(sx), int(sy), int(ex), int(ey)
        color = (255, 170, 0)         # BGR 青蓝色 —— 与点击的红圈一眼区分
        cv2.arrowedLine(img, (sx, sy), (ex, ey), color, 4, tipLength=0.12)
        cv2.circle(img, (sx, sy), 16, color, -1)      # 起点: 实心(从这里出发)
        cv2.circle(img, (ex, ey), 22, color, 3)       # 终点: 空圈(滑到这里)

    def mark_swipe(self, sx, sy, ex, ey, label=""):
        """滑动**前**截图并标注路径(用户 2026-10-14: 滑动也算点击操作 —— 默认
        截图, 且要显示滑动路径: 起点圈 + 箭头指向终点)。

        机制与 mark_click 同一套证据槽: 每步最多一张, _execute 后置块见到就让路;
        勾了截图/写了 screenshot 键**也在此拍**(证据必须是滑动前的界面)。
        """
        if getattr(self, "_pre_click_shot", None):
            return
        if not (self.click_shots or self.always_screenshot):
            return
        sv = (self._cur_step or {}).get("screenshot")
        if isinstance(sv, str) and sv.strip():
            path = sv           # 手动路径 → 标记图写在那儿(与 mark_click 同一规则)
        else:
            path = self._step_shot_path(self._cur_step_desc)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.d.screenshot(path)     # 滑动前: 界面还是"要滑"的样子, 立即截
            self._annotate_shot(
                path, lambda img: self._draw_swipe_marker(img, sx, sy, ex, ey))
            self._pre_click_shot = path
            log.info(f"[滑动标注] {path} ({sx},{sy})→({ex},{ey})")
        except Exception as e:
            log.warning(f"[截图] 滑动前截图失败(滑动照常, 由后置截图兜底): "
                        f"{type(e).__name__}: {e}")

    def _step_shot_path(self, step_desc=""):
        """步骤截图的标准路径: Test_cases/<组>/screenshots/<shot_subdir>/stepNN_<desc>.png"""
        from core import vision as _vision
        safe_desc = re.sub(r'[\\/:*?"<>|\s]+', "_", str(step_desc))[:30]
        idx = len(self.results) + 1
        # ★ 目录名要能区分**第几次执行**: 用 shot_subdir(worker 传的"用例名-第N次"),
        #   否则同一个用例跑多轮时, 每轮的 step01_xxx.png 是**同一个路径** ——
        #   后一轮把前一轮覆盖掉(用户 2026-10-12 实测报的正是这个)。
        #   没给(单元测试/旧调用)时退回用例名。
        sub = getattr(self, "shot_subdir", "") or self.case_name or "case"
        # ★ 文件名前缀(2026-10-15): 前置的自定义步骤与用例步骤**共用同一个运行目录**,
        #   两边都是 stepNN_描述.png —— 序号与描述恰好相同时会互相覆盖。前置侧给
        #   "前置" 前缀, 同目录不冲突(文件仍随本轮一起被循环清理)。
        file_prefix = getattr(self, "shot_prefix", "")
        path = (f"Test_cases/{_vision.current_app_group() or '未分组'}/"
                f"screenshots/{sub}/"
                f"{file_prefix}step{idx:02d}_{safe_desc}.png")
        # screenshots/ 开头的路径补全 Test_img/ 前缀(reports/ 等其他路径原样)
        if path.startswith("screenshots/"):
            path = os.path.join(BASE_DIR, "Test_img", path)
        elif not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        return path

    def mark_click(self, pos, label=None):
        """点击**前**截图并标注位置(用户 2026-10-13): 点完再截, 页面已经变了 ——
        标注的坐标对不上点击那一刻的界面, 证据就失去意义。

        所有点击类动作在拿到坐标后、真正点击前调用本方法; 截图进本步结果的
        screenshot 字段(_execute 的后置截图块见到它就让路)。
        ★ 每步最多一张(重复调用直接返回 —— 一步里先点 A 再点 B, 证据取第一击);
        ★ 勾了截图/写了 `screenshot:` 的步骤**也在此拍**(用户 2026-10-15: 点击的
          证据必须是"点击前 + 带点位标记"那张, 否则与点击位置对不上); 写了字符串
          路径的, 标记图就写到那个路径;
        ★ pos: (x, y) / (x, y, label) / None(取不到中心 —— 不标注不截图, 只置标记)。
        """
        if getattr(self, "_pre_click_shot", None):
            return
        if pos:
            self.last_click = (pos[0], pos[1],
                               label or (pos[2] if len(pos) > 2 else ""))
        if not pos or not (self.click_shots or self.always_screenshot):
            return
        sv = (self._cur_step or {}).get("screenshot")
        if isinstance(sv, str) and sv.strip():
            path = sv           # 旧用例点了名的手动路径 → 标记图就写在那儿
        else:
            path = self._step_shot_path(self._cur_step_desc)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.d.screenshot(path)     # 点击前: 界面还是"要点"的样子, 立即截不安定等
            self._mark_last_click_on(path)
            self._pre_click_shot = path
        except Exception as e:
            # 证据截图尽力而为: 失败了 last_click 还在, 后置截图块兜底补拍并标注
            log.warning(f"[截图] 点击前截图失败(点击照常, 由后置截图兜底): "
                        f"{type(e).__name__}: {e}")

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
        """文本断言: 多值任一命中即通过,轮询至超时(0=无限等)

        多值分隔与前置共用 `split_texts` —— 半角/全角逗号、顿号、分号、换行都算
        (曾只认半角逗号, 用户写「清洁中，正在吸尘」被当成一整串, 必然超时失败)。
        """
        timeout = timeout if timeout is not None else self.timeout
        end_time = time.time() + timeout if timeout > 0 else None
        values = split_texts(value) or [value]
        check_count = 0
        while True:
            if end_time and time.time() >= end_time:
                raise AssertionError(f"超时({timeout}s)未找到: {value}")
            # ★ WebView 内容预热: 插件页的文本要完整层级 dump 才会进无障碍树,
            #   轻量 exists() 不触发 —— 不预热会一直读到空页(真机实测 30s 白等)
            xml = warm_webview(self.d)
            # ★ 故障/空闲哨兵(用户 2026-10-13): 页面报故障 → 直接报错(带整段原文);
            #   设备待机/空闲 → 目标不可能出现, 立即失败不再傻等。与统一判断入口
            #   (text_present_detail)里的哨兵同一套词表, 这里补原生路径这一份。
            #   误触防护(2026-10-13): 必须连续 N 轮才触发(默认 3, set_status_words 配)
            kind, word, full = scan_status_words(xml)
            from core.session import (fingerprint_xml, streak_status_word,
                                     _status_words as _sw)
            if kind in ("fault", "idle"):
                fp = fingerprint_xml(xml)
                n = streak_status_word(kind, True, fp)
                need = _sw.get(f"{kind}_streak", 3)
                if n >= need:
                    if kind == "fault":
                        raise FaultStateError(
                            f"检测到故障状态(连续{n}次): 「{word}」← {full!r}"
                            f"(等待目标「{value}」不可能出现)")
                    raise AssertionError(
                        f"设备处于空闲状态(连续{n}次): 「{word}」← {full!r}"
                        f"(等待目标「{value}」不会再出现)")
            else:
                fp = fingerprint_xml(xml)
                streak_status_word("fault", False, fp)
                streak_status_word("idle", False, fp)
            for v in values:
                el = self._find_element(v)
                if el.exists(timeout=1):
                    if not self._element_visible(el):
                        # 未渲染的隐藏节点不算"看到了"(见 _element_visible) —— 继续等下一个候选
                        log.info(f"[断言] 命中的「{v}」是未渲染的隐藏节点, 不计: "
                                 f"{self._element_desc(el)}")
                        continue
                    # ★ 必须写清**命中了什么**: 断言是 textContains + content-desc 兜底,
                    #   页面上"按钮的无障碍描述"也算命中 —— 真机上出现过"断言: 暂停"
                    #   立刻通过(其实是匹配到了暂停**图标按钮**的描述, 并不是真的暂停了)。
                    #   只记查询值的话, 这种假通过永远看不出来。
                    log.info(f"[断言] 命中「{v}」← {self._element_desc(el)}")
                    return
            # ★ 兜底(用户定的方向: 原生优先, OCR 兜底): 页面主体没暴露时截图识别。
            #   复用统一入口并带上本次的 xml —— 省一次 dump, 且语义与其它判断一致。
            #   ★ 日志要区分**命中的来源**(节点文本/无障碍描述/OCR): 曾经这里一律写成
            #     "OCR 兜底命中", 结果真机上一次假通过被指向 OCR, 排查方向整个跑偏
            #     (实际是某个看不见控件的 content-desc 里含候选词, 2026-10-11)。
            hit, src, raw, pos = text_present_detail(self.d, values, xml=xml,
                                                     round_no=check_count + 1)
            if hit:
                where = {"text": "节点文本", "desc": "无障碍描述(看不见的控件)",
                         "ocr": "截图识别(OCR)"}.get(src, src)
                log.info(f"[断言] 命中「{hit}」← 来自{where}: {raw!r}"
                         + (f" @ {pos}" if pos else ""))
                return
            check_count += 1
            if end_time is not None:              # timeout=0(无限等): 无 deadline 可顺延
                end_time += self._poll_sleep(check_count) or 0

    # ── 主循环 ──
    def run_steps(self, steps: list) -> bool:
        """依次执行所有步骤,任一失败则中断。步骤支持 retry:N 自动重试;支持中途停止"""
        for i, step in enumerate(steps):
            if self.stopped:
                break
            # ★ 暂停挂起点(2026-10-13): 当前步骤已跑完, 在步骤间挂起 —— 不打断
            #   设备交互; 挂起中收到停止 → UserStopped(与停止路径同一出口)
            self._wait_if_paused()
            step = self._resolve_step(step)  # 解析 ${section.key} 定位器引用
            desc = self._step_desc(step)
            retry = step.get("retry", 0) if isinstance(step, dict) else 0
            max_attempts = retry + 1
            succeeded = False
            last_err = None
            screenshot = ""
            self.last_click = None   # 标记只属于发生点击的当前步
            self._pre_click_shot = None
            self._cur_step = step
            self._cur_step_desc = desc
            for attempt in range(max_attempts):
                if self.stopped:
                    break
                self._ensure_device()
                if self.on_step_start:
                    try:
                        self.on_step_start(desc, attempt + 1)
                    except Exception as e:      # 回调异常不能影响执行
                        log.debug(f"[on_step_start] 回调异常: {e}")
                try:
                    screenshot = self._execute(step) or ""
                    if getattr(self, "_last_compare_msg", None):
                        desc = f"{desc} ({self._last_compare_msg})"
                        self._last_compare_msg = None
                    self._append_result({"desc": desc, "passed": True, "error": "",
                                         "screenshot": screenshot})
                    self.trace.capture(self.d, desc, True)
                    # ★ 仅**最外层** if 结束后落账(嵌套的交给外层, 顺序才对);
                    #   嵌套时把列表留给孩子, 由外层统一按序追加(2026-10-15)
                    if getattr(self, "_if_depth", 0) == 0 and                             getattr(self, "_pending_sub_results", None):
                        for sub in self._pending_sub_results:
                            self._append_result(sub, fire=False)
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
                            # ★ 树稀疏/空时 OCR 兜底(2026-10-13): 用户贴的真机日志
                            #   看到 ['16:41','扫地机器人 0050'] —— 目标元素压根不在树里,
                            #   没 OCR 兜底, 失败原因只能盲猜。顺手加一行 OCR 文本。
                            ocr_texts = []
                            try:
                                from core import ocr as _ocr
                                if _ocr.available():
                                    ocr_texts = [line for line, _p in
                                                 _ocr.screen_texts(self.d)]
                            except Exception:
                                pass
                            log.error(f"[FAIL诊断] 步骤「{desc}」失败: {e}")
                            log.error(f"[FAIL诊断] 当前页面文本(树, 前30): {texts[:30]}")
                            if ocr_texts:
                                log.error(f"[FAIL诊断] 当前页面文本(OCR, 前20): "
                                          f"{ocr_texts[:20]}")
                            log.error(f"[FAIL诊断] 截图: {screenshot}")
                        except Exception:
                            pass
            if not succeeded:
                if self.stopped or isinstance(last_err, UserStopped):
                    err_text = "用户手动停止执行"      # 中性信息, 不加时间戳
                else:
                    # ★ 失败发生时间写进错误信息(用户 2026-10-15: 报告/执行结果要能
                    #   看出"什么时候失败的") —— 结果表与报告的 error 列同源
                    err_text = f"[{time.strftime('%H:%M:%S')}] {last_err}"
                self._append_result({"desc": desc, "passed": False, "error": err_text,
                                     "screenshot": screenshot,
                                     # ★ 用户停止不算"失败"(用户 2026-10-15: 前置那些
                                     #   未执行项显示成 FAIL 之后, 正常步骤同样要中性)
                                     "stopped": bool(self.stopped or
                                                     isinstance(last_err, UserStopped))})
                # 真实失败才留时序证据;用户主动停止没有诊断价值
                if not (self.stopped or isinstance(last_err, UserStopped)):
                    self.trace.capture(self.d, desc, False)
                    self.trace.dump(self.d, i)
                if getattr(self, "_if_depth", 0) == 0 and                         getattr(self, "_pending_sub_results", None):
                    for sub in self._pending_sub_results:
                        self._append_result(sub, fire=False)
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
                                     "error": "用户手动停止执行", "screenshot": "",
                                     "stopped": True})
                return False
        if self.stopped:
            self._append_result({"desc": "用户手动停止", "passed": False,
                                 "error": "用户手动停止执行", "screenshot": "",
                                 "stopped": True})
            return False
        return True

    def _execute(self, step: dict):
        """动作分发入口: 主表按 priority 找第一个命中键;grab/match/screenshot 单独处理"""
        self._cur_step = step   # 直调 _execute 的场合(单元测试)也要能判断显式截图键
        screenshot = ""
        executed_key = None
        # 1. 主操作:按优先级找第一个命中的动作键执行
        # ★ 一步里写**多个主动作**时不再静默丢掉后面的(2026-10-11 实测量过: 300 对全部
        #   只跑一个): 真机上是"设好时间却没设、直接去断言"(set_time 被 assert 顶掉)这种
        #   最难查的失败。设计上可与主动作共存的键(grab/match/screenshot/wait_for)不算
        #   主动作, 照旧共存执行。
        main = reg.main_action_keys(step)
        if len(main) > 1:
            raise ValueError(
                f"一步里有多个动作: {', '.join(main)} —— 引擎只会执行优先级最高的一个"
                f"({main[0]}), 其余的会被静默丢掉; 请把它们拆成多个步骤")
        if main:
            # ★ 延时等待类步骤: 证据在**等待开始前**拍(用户 2026-10-14: 等待 180s
            #   的步骤截图也等了 180s —— 等待本身没有画面变化, 证据应记录"开始
            #   等待时的状态"; 拍完再等, 证据槽让后置块采用)
            if main[0] == "sleep" and (self.always_screenshot or "screenshot" in step) \
                    and not getattr(self, "_pre_click_shot", None):
                try:
                    path = self._step_shot_path(self._cur_step_desc)
                    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                    self.d.screenshot(path)
                    self._pre_click_shot = path
                except Exception as e:
                    log.warning(f"[截图] 等待前截图失败(不影响等待): "
                                f"{type(e).__name__}: {e}")
            reg.ACTIONS[main[0]].fn(self, step)
            executed_key = main[0]
        # 2. 数据抓取/对比(与主动作共存于一步)
        # grab/match 现在有"面积/时间"两个独立输入框(grab_area/grab_time),
        # 老写法 grab: 面积,时间 仍然兼容 —— 两种都由 do_grab 内部按字段摊开
        has_grab = "grab" in step or "grab_area" in step
        has_match = "match" in step or "match_area" in step
        # ★ 抓取与比对写在同一"步"里同样是静默丢一个(它们是 if/elif): 抓取从当前页取值,
        #   比对是拿取值去比**另一个页面**, 一步里做两件事语义上就不成立 —— 报错而不是
        #   悄悄只做前一半(2026-10-11 实测: {grab,match} 只跑了 grab)。
        if has_grab and has_match:
            raise ValueError("一步里同时有『抓取数据』和『比对数据』 —— 引擎只执行抓取, "
                             "比对会被静默丢掉; 请把它们拆成两个步骤")
        if has_grab:
            from core.actions.data_ops import do_grab
            do_grab(self, step.get("grab_area", step.get("grab", "")),
                    step.get("grab_time"))
        elif has_match:
            from core.actions.data_ops import do_match
            do_match(self, step.get("match_area", step.get("match", "")),
                     step.get("match_time"))
        # 3. 等待条件(与主动作共存)。
        # "点一下,再等某元素出现" 是很自然的写法(如 click: 安静 + wait_for: 安静,标准,强力),
        # 但主分发只认一个主动作(两个主动作已经改成直接报错, 见上), wait_for 在这里单独补执行。
        # 独立使用时(整个步骤只有 wait_for)不会走到这里,不重复执行。
        if "wait_for" in step and executed_key != "wait_for":
            from core.actions.basic import do_wait_for
            do_wait_for(self, step)
        # 4. 截图(点击导航后页面有过场动画,先等页面落定再截 —— 否则会截到
        #    转场中的旧页面:地图编辑用例的对比基线因此截成了设置页,77.9% 假失败)
        # ★ 点击类动作**强制截图**(用户要求 2026-10-11: "点击操作默认要截图, 不可以关闭"):
        #   点错地方是最难查的一类问题 —— 报告里必须能看到"当时点在哪"。
        #   步骤里写 screenshot: false 也照样截; 写了路径就用那个路径。
        _key = reg.primary_action_key(step)
        click_step = (self.click_shots and reg.is_click_action(_key))
        # ★ 图像对比类步骤**默认截图**(用户 2026-10-15) —— 与 click_shots 开关无关:
        #   前置里的对比步骤也要留标注图(它的证据 = 标注本身)
        shot_step = click_step or _key in reg.COMPARE_ACTIONS
        pre_shot = getattr(self, "_pre_click_shot", None)
        # ★ sleep 步骤的 pre_shot 即便带显式 screenshot 键也采用 —— 等待前后画面
        #   相同, 没必要再等 180s 重拍一张(2026-10-14 用户: 等待不应延迟截图)
        if pre_shot:
            # ★ 证据槽里有就用(用户 2026-10-15: "点击截图还是没有先标记点位截图再进行
            #   点击…前置步骤的点击(也一样)"): 以前只有"没勾截图"的步骤才采用 —— 一旦
            #   勾了截图/写了 screenshot 键, 点击步骤就会走"点击后重拍", 那张既是点击
            #   后的页面、也没有点位标记, 与点击位置对不上。
            #   槽里的图由点击/滑动/长按/等待/if判定/图像对比**在动作前**拍好并标注,
            #   且每步(含每个子步骤)开始前都会清空 —— 所以"有就采用"不会串味。
            screenshot = pre_shot
            # ★ 采用即消费(用户 2026-10-15): 不清掉的话, if 分支的后续子步骤和
            #   父 if 行会把这张图**当成自己的**继承下去 —— 子步骤截图显示错乱的根因
            self._pre_click_shot = None
        elif "screenshot" in step or shot_step or self.always_screenshot:
            sv = step.get("screenshot")
            if isinstance(sv, str) and sv.strip():
                path = sv                       # 旧用例的手动路径, 兼容保留
            else:
                # ★ 开关模式(True): 截图进 APP 组目录(用户要求: 和用例/模板放一起)
                path = self._step_shot_path(step.get("desc") or "")
            # screenshots/ 开头的路径补全 Test_img/ 前缀(reports/ 等其他路径原样)
            if path.startswith("screenshots/"):
                path = os.path.join(BASE_DIR, "Test_img", path)
            elif not os.path.isabs(path):
                path = os.path.join(BASE_DIR, path)
            try:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                if click_step or "screenshot" in step:
                    self._sleep(1.5)        # 点击后的画面安定(原语义; 默认截图路径即时截)
                self.d.screenshot(path)
                screenshot = path
                self._mark_last_click_on(path)
            except Exception as e:
                # ★ 只有"点击强制截图"这一路是尽力而为的(证据是附加物, 不该让步骤失败);
                #   步骤里**显式**写了 screenshot 的仍然是老行为(截不到就报错)。
                if "screenshot" in step:
                    raise
                log.warning(f"[截图] 点击后自动截图失败(不影响本步结果): "
                            f"{type(e).__name__}: {e}")
        # room_zones 的标注图优先
        if getattr(self, "_debug_map_path", None):
            screenshot = self._debug_map_path
            self._debug_map_path = None
        return screenshot
