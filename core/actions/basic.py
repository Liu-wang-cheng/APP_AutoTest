# -*- coding: utf-8 -*-
"""交互类动作: click/input/back/long_click/swipe/if_click/find_click/
wait_for/wait_loading/if。

click 值支持四种形态:
  "开始清扫.png"  模板图(SIFT 定位点击)
  "确认"          textContains(回退 description);"=确认" 精确 text;"确认#2" 第2个
  [x, y]          坐标直点(v1.4 Appium 用例转换而来,硬编码坐标先用坐标跑通,
                  后续逐步替换成模板图)
  "x,y"           坐标直点的**字符串写法**(界面上的字段是文本框, 用户按提示写坐标
                  得到的就是它);"x，y"(全角逗号)、"x y"(空格) 同样认
★ 坐标在 click/long_click/swipe 里都同时认列表与字符串 —— 曾经只认列表, 于是界面上
  写的 "x,y" 会掉进"按文本找元素"的分支, 轮询到超时报错(用户实测「点击坐标点不动」)。
"""
import os
import re
import time

from core import registry as reg
from core.driver import split_texts      # 多值文本拆分: 半角/全角逗号、顿号、分号、换行
from core.logger import get_logger

log = get_logger()


def parse_xy(value, n):
    """把坐标解析成 n 个整数; 不像坐标(方向/按钮名/图片名)返回 None。

    ★ 为什么需要(2026-10-10 用户实测「点击坐标不能正常执行」):
      界面上 click/long_click/swipe 这些字段是**文本框**, 用户按提示写 "x,y 坐标"
      得到的是**字符串**; 而实现原来只认 YAML 列表 `[x, y]`(v1.4 Appium 用例转换来的
      那种写法) —— 字符串会掉进"按文本找元素"的分支, 找不到就轮询到超时报错。
      两种写法都支持: [x, y] / "x,y" / "x，y"(全角) / "x y"。
    """
    if isinstance(value, (list, tuple)):
        if len(value) < n:
            return None
        try:
            return [int(float(v)) for v in value[:n]]
        except (TypeError, ValueError):
            return None
    text = str(value or "").strip()
    # ★ 方括号串 "[sx,sy,ex,ey]" 也认(schema 提示一直这么写, 但正则漏了 [ ] ——
    #   字符串带方括号从来解析不了, 只有 YAML 列表形式可以; 2026-10-14 顺手补上)
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1].strip()
    # 只由数字/分隔符组成才算坐标 —— 含字母或中文的一律不是(left/fast-up/按钮名/图片名)
    if not text or not re.fullmatch(r"[\d\s,，.]+", text):
        return None
    parts = [x for x in re.split(r"[\s,，]+", text) if x]
    if len(parts) != n:
        return None
    try:
        return [int(float(x)) for x in parts]
    except ValueError:
        return None


def looks_like_numbers(value):
    """整串就是数字与分隔符(用于报错时区分"坐标给少了"和"方向名不认识")"""
    return bool(re.fullmatch(r"[\d\s,，.]+", str(value or "").strip()))


@reg.action("click", priority=5)
def do_click(runner, step):
    value = step["click"]
    timeout = step.get("timeout", runner.click_timeout)
    xy = parse_xy(value, 2)
    if xy:
        runner.mark_click(xy, "坐标")       # ★ 点击前截图标注(2026-10-13)
        runner.d.click(xy[0], xy[1])
        log.info(f"[点击] 坐标 ({xy[0]},{xy[1]})")
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
    xy = parse_xy(value, 2)
    dur3 = None
    if isinstance(value, str):
        three = parse_xy(value, 3)
        if three:                         # "x,y,时长" 也认(与 [x, y, 时长] 一致)
            xy, dur3 = three[:2], three[2]
    if xy:
        x, y = xy
        if dur3 is not None:
            duration = dur3
        elif isinstance(value, (list, tuple)) and len(value) >= 3:
            duration = value[2]           # 老写法允许 [x, y, duration]
        what = f"坐标 ({x},{y})"
    elif runner._is_image(value):
        pos = runner._assert_template(value, step.get("timeout", runner.click_timeout))
        runner.mark_click(pos, str(value))  # ★ 点击前截图标注(2026-10-13)
        x, y = pos
        what = f"模板 {value} @ ({x},{y})"
    else:
        el = runner._find_element(value)
        if not el.exists(timeout=runner.click_timeout):
            raise AssertionError(f"未找到长按目标: {value}")
        if hasattr(el, "long_click"):     # XPathElement 原生支持长按
            runner.mark_click(runner._center_xy(el, f"长按 {value}"))
            log.info(f"[长按] 文本「{value}」 {float(duration):g}s")
            el.long_click(float(duration))
            runner._sleep(1)
            return
        b = el.info["bounds"]
        x, y = (b["left"] + b["right"]) // 2, (b["top"] + b["bottom"]) // 2
        what = f"文本「{value}」 @ ({x},{y})"
    # 原地滑动=长按。★ duration 单位是秒(uiautomator2 语义)——曾经 ×1000
    # 当毫秒用,4 秒长按变成 4000 秒(66 分钟),机器人被持续按住前进键不放
    runner.mark_click((x, y), f"长按 {value}")   # ★ 点击前截图标注(2026-10-13)
    log.info(f"[长按] {what} {float(duration):g}s")
    runner.d.swipe(x, y, x, y, duration=float(duration))
    runner._sleep(1)


@reg.action("input", priority=15)
def do_input(runner, step):
    """向当前聚焦元素写入文本"""
    text = str(step["input"])
    runner.d(focused=True).set_text(text)
    log.info(f"[输入] {text[:40]}{'…' if len(text) > 40 else ''}")


@reg.action("back", priority=40, fixed_bool=True)
def do_back(runner, step):
    runner.d.press("back")
    log.info("[返回] 系统返回键")


@reg.action("swipe", priority=65)
def do_swipe(runner, step):
    """left/right/up/down、fast-*、或 [sx,sy,ex,ey]

    方向滑动从「中心向两侧展开」: 起点在 cx+dist、终点在 cx-dist,总行程
    是 2*dist(普通 1.0 屏宽、fast 0.6 屏宽)。只从中心往单侧滑行程太短,
    列表翻页和地图拖动经常带不动。

    ★ 起止分开填(用户 2026-10-14): 界面分「开始点位/结束点位」两框 ——
      swipe: "100,500" + swipe_end: "100,1500"; 两种写法都兼容:
      整串 [sx,sy,ex,ey] 与方向名照旧(旧用例不动)。
    ★ 滑动前截图并标注路径(起点圈+箭头, 2026-10-14): 与点击证据同等待遇。
    """
    value = step["swipe"]
    end_value = str(step.get("swipe_end") or "").strip()
    xy = None
    if end_value:
        sxy = parse_xy(value, 2)
        exy = parse_xy(end_value, 2)
        if not sxy or not exy:
            raise ValueError(
                f"开始/结束点位要各 2 个数字(x,y): 开始={value!r} 结束={end_value!r}")
        xy = (sxy[0], sxy[1], exy[0], exy[1])
    else:
        xy = parse_xy(value, 4)
    if xy:
        runner.mark_swipe(xy[0], xy[1], xy[2], xy[3], step.get("desc") or "滑动")
        log.info(f"[滑动] 坐标 ({xy[0]},{xy[1]})→({xy[2]},{xy[3]})")
        runner.d.swipe(*xy)
    elif looks_like_numbers(value):
        # 是数字但个数不对(如写了 3 个) —— 报清楚, 别让人以为是"方向名不认识"
        raise ValueError(f"swipe 坐标要 4 个数字(sx,sy,ex,ey), 收到: {value!r}")
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
        runner.mark_swipe(*quad, step.get("desc") or f"滑动 {name}")
        log.info(f"[滑动] {name} ({quad[0]},{quad[1]})→({quad[2]},{quad[3]})")
        runner.d.swipe(*quad)
    runner._sleep(step.get("wait_after", 1))


@reg.action("if_click", priority=90)
def do_if_click(runner, step):
    """存在才点: 多值(逗号/顿号/分号/换行分隔),点到第一个出现的就停;都不存在不算失败"""
    values = split_texts(step["if_click"]) or [str(step["if_click"])]
    for v in values:
        el = runner._find_element(v)
        if el.exists(timeout=1):
            runner.mark_click(runner._center_xy(el, v))   # ★ 点击前截图标注
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
            runner.mark_click(runner._center_xy(el, v))   # ★ 点击前截图标注
            el.click()
            log.info(f"[find_click] 命中并点击: {v}")
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
                log.info(f"[wait_for] 等到模板: {value}")
                return
        else:
            # ★ 判断走 runner._text_present: 原生优先 + 树稀疏时 OCR 兜底
            # ★ 多值(逗号分隔)要写清命中的是哪个(用户 2026-10-15, 与 if 同一问题)
            if runner._text_present(*(split_texts(value) or [value]), round_no=n + 1):
                from core.session import hit_suffix
                log.info(f"[wait_for] 等到文本(任一): {value}"
                         f" → {hit_suffix(getattr(runner, '_last_text_hit', None))}")
                return
        n += 1
        if end is not None:                  # timeout=0(无限等): 无 deadline 可顺延
            end += runner._poll_sleep(n) or 0    # 暂停挂起 → 超时顺延(2026-10-14 审查)


# 加载态提示语。涂鸦各页面用词不统一(列表页"正在加载"、地图页"请稍候"),
# 少一个词就会误判成"已加载完"而立刻往下走。
# ★ 公开名(结构整理 2026-10-15): map_ops 也要用同一份词表, 原先它跨层取下划线私有名。
LOADING_TEXTS = ("加载中", "正在加载", "loading", "Loading", "处理中", "请稍候")


@reg.action("wait_loading", priority=105, fixed_bool=True)
def do_wait_loading(runner, step):
    """等加载提示消失,默认最长 30s(可用 timeout 覆盖)

    超时不报错 —— 加载慢不该让用例失败,后面的断言自然会判定。
    """
    timeout = step.get("timeout", 30)
    end = time.time() + timeout
    n = 0
    while time.time() < end:
        n += 1
        # ★ 判断走 runner._text_present —— 树稀疏时"看不到加载提示"是假象,
        #   只读无障碍树会把"还在加载"误判成"已加载完"; 轮次传下去, OCR 仍按 5 轮一次
        if not runner._text_present(*LOADING_TEXTS, round_no=n):
            log.info(f"[wait_loading] 加载提示已消失(第 {n} 轮确认)")
            return
        end += runner._sleep(1) or 0    # 暂停挂起 → 超时顺延(2026-10-14 审查)
    log.warning(f"[wait_loading] {timeout}s 后加载提示仍在,继续执行")


@reg.action("if", priority=30)
def do_if(runner, step):
    do_if_impl(runner, step, negate=False)


@reg.action("if not", priority=35)
def do_if_not(runner, step):
    do_if_impl(runner, step, negate=True)


#: if 条件的检测节奏(用户 2026-10-12 定): **两轮 × 每轮五次 = 10 次检测**;
#: 每轮**前四次普通检测、第五次上 OCR 兜底**(正好落在第 5、10 次)。
#: ★ 原来是 timeout=5 秒兜底, 用户反馈"5s 容错太低" → 改成**按次数**跑满两轮, 不再被秒数支配;
#:   步骤里显式写了 timeout 时按 max(10, timeout) 次(秒数≈每次间隔, 向后兼容)。
IF_ROUNDS = 2
IF_CHECKS_PER_ROUND = 5
IF_TOTAL_CHECKS = IF_ROUNDS * IF_CHECKS_PER_ROUND


def _if_checks(step):
    """这一步的 if 条件要检测几次"""
    try:
        # ★ 上限 120(审查 2026-10-12): timeout 的旧语义是"秒", 被重载成"次数"后大值
        #   会爆炸 —— timeout: 1800 → 1800 次 × 阶梯(1/2/5s) ≈ 2.5 小时, 像挂死。
        #   120 次 ≈ 9 分钟, 比任何合理的等待都长。
        return min(max(IF_TOTAL_CHECKS, int(step.get("timeout", 0) or 0)), 120)
    except (TypeError, ValueError):
        return IF_TOTAL_CHECKS


def do_if_impl(runner, step, negate):
    """条件分支(语义反直觉): 条件成立→直接跳过;条件不成立→执行 else 列表。

    文本:    if: 扫地机器人  /  if not: 扫地机器人
    数值:    if: 电量 > 50
    图片:    if: 按钮.png —— 在页面里找这张模板图(与图像对比分为两个步骤, 用户
             2026-10-13; 要按相似度对比用「图像相似度对比」步骤)
    ID:      if: com.xxx:id/btn
    """
    condition = step.get("if", step.get("if not", ""))
    # (timeout 已不再被直接读 —— 检测次数统一由 _if_checks 按"两轮×五次, 上限 120"定)
    passed = False

    checks = _if_checks(step)
    num_seen = {"v": None, "read": False}    # 数值条件的实测值(留痕, 用户 2026-10-15)
    for op, fn in runner._NUM_OPS.items():
        if op in condition:
            keyword, expected = condition.split(op, 1)
            from core.actions.data_ops import extract_value

            def _num_ok(_i):
                try:
                    val = extract_value(runner, runner._get_all_nodes(), keyword.strip())
                except ValueError:
                    num_seen["read"] = False
                    return False
                num_seen["v"] = num_seen["read"] = val
                try:
                    return bool(fn(float(val), float(expected.strip())))
                except (TypeError, ValueError):
                    return False
            passed = runner._poll_until(checks, _num_ok)
            break
    else:
        if runner._is_image(condition):
            # ★ 图片条件只判"这张模板图出现在页面上"(用户要求 2026-10-13: if 判断与
            #   图像对比**分为两个步骤**) —— 以前这里还认 step["threshold"] 走整屏
            #   相似度对比, 那是 compare 步骤的职责(阈值属于它), if 里已移除。
            #   老用例 YAML 里写的 threshold 会被忽略(模板匹配本就是严格判断)。
            import cv2
            from core import vision
            # ★ 先解析到组模板目录(审查 2026-10-12): 裸名直接丢给 find_in_gray 会在
            #   getmtime 处 FileNotFoundError —— wait_for/_assert_template 都先
            #   resolve, 这里原本漏了。
            tpl = vision.resolve_template(condition)

            def _tpl_ok(_i):
                screen = runner.d.screenshot(format="opencv")
                return vision.find_in_gray(
                    cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY),
                    tpl, min_matches=3) is not None
            passed = runner._poll_until(checks, _tpl_ok)
        elif ":id/" in condition:
            passed = runner._poll_until(
                checks, lambda _i: runner.d(resourceId=condition).exists(timeout=0.2))
        else:
            # 多值(逗号/顿号/分号/换行分隔): 任一命中即条件成立(与 assert 的多值语义一致)。
            # v1.4 的 TextViewContentChecker(driver, ["充电中","充电完成"], t) 就是这个语义,
            # 转换后的用例大量依赖它。
            # ★ 判断走 runner._text_present(原生优先 + OCR 兜底)
            # ★ 文本: 按**次数**跑(两轮×五次); 第 5、10 次走 OCR 兜底(见 core/ocr.py)
            passed = runner._text_present(*(split_texts(condition) or [condition]),
                                          rounds=checks)

    if negate:
        passed = not passed
    # ★ 条件分支也要留痕: 走哪个分支直接决定后面为什么"多跑/少跑了几步"
    then_branch = step.get("then") or []
    else_branch = step.get("else") or []
    if passed:
        what = f"执行 then({len(then_branch)} 步)" if then_branch else "跳过(没有 then 子步骤)"
    else:
        what = f"执行 else({len(else_branch)} 步)" if else_branch else "跳过(没有 else 子步骤)"
    # ★ 多值条件要写清**按哪个候选判的**(用户 2026-10-15: "条件成立('今天,已取消,结尾')
    #   这里也没显示命中的内容"): 光看整串条件, 出了分歧只能一格格猜。命中详情由
    #   runner._text_present 留下; 图片/id 条件不产生**(也不读旧的)**, 免得串味。
    #   数值条件同理: 写出**实测值**, 否则"电量 > 50 不成立"看不出当时读到了多少。
    from core.session import hit_suffix
    is_num = any(op in condition for op in runner._NUM_OPS)
    if is_num:
        extra = (f" 实测 {num_seen['v']!r}" if num_seen["read"] else " 该字段读不到")
    elif not runner._is_image(condition) and ":id/" not in condition:
        extra = " " + hit_suffix(getattr(runner, "_last_text_hit", None))
    else:
        extra = ""
    log.info(f"[if{'(not)' if negate else ''}] 条件{'成立' if passed else '不成立'}"
             f"({condition!r}{extra}) → {what}")

    # ★ if 步骤自己的证据在**判定完、跑分支之前**拍(用户 2026-10-15: "截图也是在子
    #   步骤后面截图的"): 后置块在分支跑完后才拍, 那时画面已被子步骤改过, 还会被
    #   标上最后一个子步骤的点击标记 —— 证据应记录"判定那一刻的页面"。
    #   经证据槽, _execute 后置块直接采用(见其中 if/if not 的采用条件)。
    if not getattr(runner, "_pre_click_shot", None) and \
            (getattr(runner, "click_shots", False) or
             getattr(runner, "always_screenshot", False)):
        try:
            path = runner._step_shot_path(getattr(runner, "_cur_step_desc", ""))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            runner.d.screenshot(path)
            runner._pre_click_shot = path
        except Exception as e:
            log.warning(f"[截图] if 判定前截图失败(不影响判定): {type(e).__name__}: {e}")

    runner._if_depth = getattr(runner, "_if_depth", 0) + 1
    if runner._if_depth == 1:
        # ★ **最外层** if 每次调用都重新开始攒账(含带 retry 的第二次尝试) —— 上一次
        #   走 else 留下的失败子行不得混进本轮(否则整体通过却留下 FAIL 行, Excel 会把
        #   该用例标 FAIL); 内层(嵌套 if)则**共享**父层的列表, 顺序才正确(2026-10-15)
        runner._pending_sub_results = []
    _t0 = time.time()
    try:
        if passed:
            _run_branch(runner, then_branch, "条件成立")
        else:
            _run_branch(runner, else_branch, "条件不成立")
    finally:
        runner._if_depth = max(0, getattr(runner, "_if_depth", 1) - 1)
        # ★ 分支完毕留痕(用户 2026-10-15): 只有"判定 + 分支首步"两行看不出先后 ——
        #   补这行就把"判定→(分支, 可能含长等待)→完毕"括起来了
        log.info(f"[if] {condition!r} 分支执行完毕(耗时 {time.time() - _t0:.1f} 秒)")


def _run_branch(runner, steps, label=""):
    """执行 if 的某个分支子步骤(条件成立→`then`, 不成立→`else`)。

    label: 分支的中文名("条件成立"/"条件不成立") —— **会加在每条子步骤描述前面**。
      ★ 用户 2026-10-12: "if 分支下 then/else 子分支执行的步骤没有在测试步骤中标注一下,
        不好区分" —— 结果表/报告里子步骤原来和普通步骤长得一样, 看不出属于哪个分支。

    ★ 子结果进 `_pending_sub_results`, 由**最外层 if** 的 `_execute` 统一按序落账
      (嵌套 if 共享同一列表, 见下); 子步骤失败直接抛, 由主循环按重试/失败流程处理。
    ★ 嵌套 if(用户 2026-10-15 审查发现两个真 bug):
      · 以前每层都 `= []` 重置 —— 内层会把**外层已攒的子结果清掉**, 结果里直接少行
        (A 步消失);
      · 容器型子步骤(嵌套 if 本身)以前在**跑完**才登记 —— 它的子步骤会排在它前面。
      现在: 列表只在为 None 时新建(嵌套沿用父层, 顺序天然正确); 容器子步骤**先登记
      行**(sub_id + running) 再跑, 跑完就地更新(界面按 sub_id 原地改, 不重复插行)。
    ★ 空分支什么都不做(不动列表) —— 带 retry 的 if 上一轮的子行已由失败路径落账并清空,
      不会漏进本轮。
    """
    if not steps:
        return
    pending = getattr(runner, "_pending_sub_results", None)
    if pending is None:
        pending = runner._pending_sub_results = []       # 最外层 if 新建
    prefix = f"[{label}] " if label else ""
    parent_desc = getattr(runner, "_cur_step_desc", "")
    # ★ 父步骤(if)在**判定那一刻**拍的证据要跨分支保住(用户 2026-10-15: "if判断的
    #   步骤截图也是延迟180s之后的"): 下面每个子步骤都会清证据槽以隔离归属, 不先存
    #   一份的话父步骤那张会被清掉 —— 后置块只好等分支(可能含 180s 等待)跑完再重拍。
    parent_shot = getattr(runner, "_pre_click_shot", None)
    for s in steps:
        desc = prefix + str(s.get("desc", runner._step_desc(s)))
        # ★ 子步骤的证据归属(用户 2026-10-15: "if 子步骤的截图显示不正确"):
        #   · 证据文件名用**子步骤自己的描述**(以前继承父 if 的, 一串子步骤全同名);
        #   · 清掉上一个子步骤遗留的证据槽 —— 否则"点击子步骤拍的照片"会被后面的
        #     断言子步骤、乃至父 if 行**当成自己的**继承下去(截图错乱的根因)。
        runner._cur_step_desc = desc
        runner._pre_click_shot = None
        # 容器型子步骤(嵌套 if): 先登记并推一行「执行中」, 跑完就地更新 —— 否则它的
        # 子步骤会排在它前面(用户 2026-10-15 同类顺序问题)
        entry = None
        if reg.primary_action_key(s) in ("if", "if not"):
            runner._sub_seq += 1
            entry = {"desc": desc, "sub": True, "sub_id": runner._sub_seq,
                     "running": True, "passed": None, "error": "", "screenshot": ""}
            pending.append(entry)
            runner._emit_sub(dict(entry))
        err = None
        ss = ""
        try:
            ss = runner._execute(s) or ""
            # ★ sub=True: 界面上**追加新行**而不占用父 if 的"执行中"占位 ——
            #   否则父行会被第一个子步骤顶掉, 最后又追加到子步骤后面(用户 2026-10-15:
            #   "if 判断的步骤会在子步骤后面…执行结果顺序也是")
            sub = {"desc": desc, "passed": True, "error": "", "screenshot": ss,
                   "sub": True}
        except Exception as e:
            sub = {"desc": desc, "passed": False, "error": str(e), "screenshot": ss,
                   "sub": True}
            err = e
        # ★ 子步骤结果**即时**推 GUI(用户 2026-10-15: "子步骤在 if 全部结束后才显示")
        if entry is not None:                # 容器: 就地更新(界面按 sub_id 原地改)
            entry.update(sub)
            entry.pop("running", None)
            runner._emit_sub(dict(entry))
        else:
            runner._sub_seq += 1
            sub["sub_id"] = runner._sub_seq
            pending.append(sub)
            runner._emit_sub(dict(sub))
        if err is not None:
            raise err   # 失败子步骤: 已推给界面, 这里继续抛给主循环走失败流程
        time.sleep(runner.interval)
    runner._cur_step_desc = parent_desc      # 还原父 if 的描述(后续后置块用它)
    runner._pre_click_shot = parent_shot     # 还原父步骤证据(子步骤不得继承, 父也不得丢)
