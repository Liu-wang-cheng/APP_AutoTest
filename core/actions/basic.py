# -*- coding: utf-8 -*-
"""交互类动作: click/input/back/long_click/swipe/if_click/find_click/
wait_for/wait_loading/if。

click 值支持四种形态:
  "开始清扫.png"  模板图(SIFT 定位点击)
  "确认"          textContains(回退 description);"=确认" 精确 text;"确认#2" 第2个
  [x, y]          坐标直点(v1.4 Appium 用例转换而来,硬编码坐标先用坐标跑通,
                  后续逐步替换成模板图)
"""
import time

from core import registry as reg
from core.driver import split_texts      # 多值文本拆分: 半角/全角逗号、顿号、分号、换行
from core.logger import get_logger

log = get_logger()


@reg.action("click", priority=5)
def do_click(runner, step):
    value = step["click"]
    timeout = step.get("timeout", runner.click_timeout)
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        runner.d.click(int(value[0]), int(value[1]))
        runner.last_click = (int(value[0]), int(value[1]), "坐标")
        return
    if runner._is_image(value):
        runner._click_by_template(value, timeout)
    else:
        runner._click_by_locator(value, timeout)


@reg.action("click_template", priority=46)
def do_click_template(runner, step):
    """点击模板: 值 = 当前 APP 组的模板名(下拉选择), 按模板匹配点击"""
    value = step["click_template"]
    timeout = step.get("timeout", runner.click_timeout)
    runner._click_by_template(value, timeout)


@reg.action("long_click", priority=45)
def do_long_click(runner, step):
    value = step["long_click"]
    duration = step.get("duration", 2)
    if isinstance(value, (list, tuple)):
        x, y = int(value[0]), int(value[1])
        if len(value) >= 3:
            duration = value[2]
    elif runner._is_image(value):
        pos = runner._assert_template(value, step.get("timeout", runner.click_timeout))
        runner.last_click = (pos[0], pos[1], str(value))
        x, y = pos
    else:
        el = runner._find_element(value)
        if not el.exists(timeout=runner.click_timeout):
            raise AssertionError(f"未找到长按目标: {value}")
        if hasattr(el, "long_click"):     # XPathElement 原生支持长按
            el.long_click(float(duration))
            runner._sleep(1)
            return
        b = el.info["bounds"]
        x, y = (b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2
    # 原地滑动=长按。★ duration 单位是秒(uiautomator2 语义)——曾经 ×1000
    # 当毫秒用,4 秒长按变成 4000 秒(66 分钟),机器人被持续按住前进键不放
    runner.d.swipe(x, y, x, y, duration=float(duration))
    runner._sleep(1)


@reg.action("input", priority=15)
def do_input(runner, step):
    """向当前聚焦元素写入文本"""
    runner.d(focused=True).set_text(str(step["input"]))


@reg.action("back", priority=40, fixed_bool=True)
def do_back(runner, step):
    runner.d.press("back")


@reg.action("swipe", priority=65)
def do_swipe(runner, step):
    """left/right/up/down、fast-*、或 [sx,sy,ex,ey]

    方向滑动从「中心向两侧展开」: 起点在 cx+dist、终点在 cx-dist,总行程
    是 2*dist(普通 1.0 屏宽、fast 0.6 屏宽)。只从中心往单侧滑行程太短,
    列表翻页和地图拖动经常带不动。
    """
    value = step["swipe"]
    if isinstance(value, (list, tuple)):
        runner.d.swipe(*[int(v) for v in value[:4]])
    else:
        speed = 0.3 if str(value).startswith("fast-") else 0.5
        name = str(value).replace("fast-", "")
        w, h = runner.d.window_size()
        cx, cy = w // 2, h // 2
        dist = int(w * speed)
        quad = {
            "left":  (cx + dist, cy, cx - dist, cy),
            "right": (cx - dist, cy, cx + dist, cy),
            "up":    (cx, cy + dist, cx, cy - dist),
            "down":  (cx, cy - dist, cx, cy + dist),
        }.get(name)
        if quad is None:
            raise ValueError(f"swipe 方向不认识: {value}(支持 left/right/up/down/fast-*)")
        runner.d.swipe(*quad)
    runner._sleep(step.get("wait_after", 1))


@reg.action("if_click", priority=90)
def do_if_click(runner, step):
    """存在才点: 多值(逗号/顿号/分号/换行分隔),点到第一个出现的就停;都不存在不算失败"""
    values = split_texts(step["if_click"]) or [str(step["if_click"])]
    for v in values:
        el = runner._find_element(v)
        if el.exists(timeout=1):
            el.click()
            log.info(f"[if_click] 命中并点击: {v}")
            return
    log.info(f"[if_click] 均未出现,跳过: {values}")


@reg.action("find_click", priority=25)
def do_find_click(runner, step):
    """候选列表,点第一个存在的;全都没有则失败"""
    values = step["find_click"]
    if isinstance(values, str):
        values = split_texts(values)
    for v in values:
        el = runner._find_element(v)
        if el.exists(timeout=1):
            el.click()
            return
    raise AssertionError(f"find_click 全部候选未找到: {values}")


@reg.action("wait_for", priority=110)
def do_wait_for(runner, step):
    """智能等待: 文本(逗号分隔任一)或图片出现"""
    value = step["wait_for"]
    # 默认 15s(不是全局 default_timeout):等待是"顺带确认",等太久没意义,
    # 真出不来后面的 assert 会给出更明确的失败
    timeout = step.get("timeout", 15)
    end = time.time() + timeout if timeout > 0 else None
    n = 0
    while True:
        if end and time.time() >= end:
            raise AssertionError(f"wait_for 超时({timeout}s): {value}")
        if runner._is_image(value):
            import cv2
            from core import vision
            screen = runner.d.screenshot(format="opencv")
            if vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY),
                                   vision.resolve_template(value), min_matches=4):
                return
        else:
            for v in (split_texts(value) or [value]):
                if runner._find_element(v).exists(timeout=1):
                    return
        n += 1
        runner._poll_sleep(n)


# 加载态提示语。涂鸦各页面用词不统一(列表页"正在加载"、地图页"请稍候"),
# 少一个词就会误判成"已加载完"而立刻往下走。
_LOADING_TEXTS = ("加载中", "正在加载", "loading", "Loading", "处理中", "请稍候")


@reg.action("wait_loading", priority=105, fixed_bool=True)
def do_wait_loading(runner, step):
    """等加载提示消失,默认最长 30s(可用 timeout 覆盖)

    超时不报错 —— 加载慢不该让用例失败,后面的断言自然会判定。
    """
    timeout = step.get("timeout", 30)
    end = time.time() + timeout
    while time.time() < end:
        if not any(runner.d(textContains=t).exists(timeout=0.5) for t in _LOADING_TEXTS):
            return
        runner._sleep(1)
    log.warning(f"[wait_loading] {timeout}s 后加载提示仍在,继续执行")


@reg.action("if", priority=30)
def do_if(runner, step):
    do_if_impl(runner, step, negate=False)


@reg.action("if not", priority=35)
def do_if_not(runner, step):
    do_if_impl(runner, step, negate=True)


def do_if_impl(runner, step, negate):
    """条件分支(语义反直觉): 条件成立→直接跳过;条件不成立→执行 else 列表。

    文本:    if: 扫地机器人  /  if not: 扫地机器人
    数值:    if: 电量 > 50
    图片:    if: 按钮.png(可配 threshold 走图像对比)
    ID:      if: com.xxx:id/btn
    """
    condition = step.get("if", step.get("if not", ""))
    timeout = step.get("timeout", 5)
    passed = False

    for op, fn in runner._NUM_OPS.items():
        if op in condition:
            keyword, expected = condition.split(op, 1)
            from core.actions.data_ops import extract_value
            val = extract_value(runner, runner._get_all_nodes(), keyword.strip())
            if val:
                try:
                    passed = bool(fn(float(val), float(expected.strip())))
                except (TypeError, ValueError):
                    pass
            break
    else:
        if runner._is_image(condition):
            threshold = step.get("threshold")
            if threshold is not None:
                from core.actions.asserts import compare_screen
                try:
                    compare_screen(runner, condition, float(threshold))
                    passed = True
                except AssertionError:
                    passed = False
            else:
                import cv2
                from core import vision
                screen = runner.d.screenshot(format="opencv")
                passed = vision.find_in_gray(cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY),
                                             condition, min_matches=3) is not None
        elif ":id/" in condition:
            passed = runner.d(resourceId=condition).exists(timeout=timeout)
        else:
            # 多值(逗号/顿号/分号/换行分隔): 任一命中即条件成立(与 assert 的多值语义一致)。
            # v1.4 的 TextViewContentChecker(driver, ["充电中","充电完成"], t) 就是这个语义,
            # 转换后的用例大量依赖它。
            passed = any(runner.d(textContains=c).exists(timeout=timeout)
                         for c in (split_texts(condition) or [condition]))

    if negate:
        passed = not passed

    if passed:
        return
    else_branch = step.get("else", [])
    runner._pending_sub_results = []
    for s in else_branch:
        desc = s.get("desc", runner._step_desc(s))
        ss = ""
        try:
            ss = runner._execute(s) or ""
            runner._pending_sub_results.append({"desc": desc, "passed": True,
                                                "error": "", "screenshot": ss})
        except Exception as e:
            runner._pending_sub_results.append({"desc": desc, "passed": False,
                                                "error": str(e), "screenshot": ss})
            raise
        time.sleep(runner.interval)
