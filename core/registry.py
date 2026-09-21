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
