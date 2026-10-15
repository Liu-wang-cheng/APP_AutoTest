# -*- coding: utf-8 -*-
"""动作注册表: 用 @action 装饰器替代源项目 _MAIN_DISPATCH 有序列表。

顺序即优先级的语义保留 —— dispatch_order() 按 priority 升序返回,
runner._execute 找「第一个命中的动作键」执行。新增动作只需在 actions/
任意模块加装饰器,不改 runner 核心。
"""
from dataclasses import dataclass
from typing import Callable

# 无参数动作键: GUI 序列化时保证标量 true(引擎按 True 语义处理)
FIXED_BOOL_DEFAULTS = {"back", "room_zones", "split_zone", "merge_zones",
                       "latest_record", "wait_loading"}


@dataclass
class Action:
    key: str
    fn: Callable
    priority: int
    fixed_bool: bool


ACTIONS: dict = {}


def action(key: str, priority: int = 50, fixed_bool: bool = False):
    """注册动作。key 重复注册直接抛错(防手误把老动作顶掉)。"""
    def deco(fn):
        if key in ACTIONS:
            raise ValueError(f"动作重复注册: {key} (已由 {ACTIONS[key].fn.__module__} 注册)")
        ACTIONS[key] = Action(key=key, fn=fn, priority=priority, fixed_bool=fixed_bool)
        return fn
    return deco


def dispatch_order():
    """按 priority 升序的动作列表 —— 顺序即分发的优先级。"""
    return sorted(ACTIONS.values(), key=lambda a: a.priority)


def fixed_bool_keys() -> set:
    return {k for k, a in ACTIONS.items() if a.fixed_bool}


#: 与主动作**共存于同一步**的键: 它们不是"主动作", 由 runner._execute 单独处理,
#: 因此可以和任意一个主动作写在一起(如 click + grab / click + wait_for)。
#: ★ 引擎(runner)与界面(卡片显示什么动作)必须用同一份定义 —— 判据分家过一次,
#:   结果是 {assert, click} 这种步骤"卡片显示断言、实际执行点击"(2026-10-11 实测)。
COEXIST_KEYS = frozenset({"grab", "match", "screenshot", "wait_for"})


def main_action_keys(step) -> list:
    """步骤里的**主动作**键, 按执行优先级升序 —— 第一个就是真正会被执行的那个。

    grab/match/screenshot/wait_for 不算主动作(见 COEXIST_KEYS), 它们与主动作共存。
    ★ `wait_for` 特殊: 它**既是注册动作**(优先级 110, 可以单独成步), 又在共存集合里
      —— 所以这里必须显式排除, 否则 `click + wait_for` 这种正常写法会被误判成
      "一步多个动作"而报错(2026-10-11 守护测试当场抓到这个错)。
    """
    if not isinstance(step, dict):
        return []
    return [a.key for a in dispatch_order()
            if a.key in step and a.key not in COEXIST_KEYS]


def primary_action_key(step):
    """会被执行的那个主动作键; 没有主动作(如只有 grab)则返回 None"""
    keys = main_action_keys(step)
    return keys[0] if keys else None


#: 「点击类」动作: 它们**默认并强制**留下带点击标记的截图(用户要求 2026-10-11:
#: "所有的点击操作都要在测试图片上标注出点击的位置并自动放报告中, 点击操作默认要截图,
#:  不可以关闭")。点错地方是最难查的一类问题 —— 报告里能看到"当时点在哪"。
CLICK_ACTIONS = frozenset({"click", "click_template", "long_click",
                           "if_click", "find_click"})


def is_click_action(key):
    """这个动作是不是"点击类"(→ 强制截图并把点击位置标在图上)"""
    return key in CLICK_ACTIONS


#: 「图像对比」类: 它们的截图 = **标注图本身**(对比范围矩形 + 相似度/阈值 + PASS/FAIL),
#: 所以同样**默认产出**、且必须用标注后的那张(用户 2026-10-15: "图像对比步骤也需要
#: 改为默认截图, 并且需要显示对比标注图")。与 click_shots 开关无关 —— 前置里也要。
COMPARE_ACTIONS = frozenset({"compare", "diff"})


def is_shot_action(key):
    """这个动作是否**默认产出截图**(点击类 / 图像对比)"""
    return key in CLICK_ACTIONS or key in COMPARE_ACTIONS
