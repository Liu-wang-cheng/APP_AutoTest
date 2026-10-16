# -*- coding: utf-8 -*-
"""import 本包即完成全部动作注册(顺序无关,分发按 priority)。

grab / match 不在此列 —— 它们与主动作共存于一步,由 runner._execute
单独调用 data_ops.do_grab / do_match。

★ `__all__` / `load_all()` 的由来(2026-10-16): 这一行 import 是**为了副作用**
  (导入子模块即注册动作), 名字本身不用 —— pyflakes 会把它报成"导入了却没用",
  而 CI 的静态检查 (`.github/workflows/static.yml`) 因此一直是红的 (noqa 只有
  flake8 认, pyflakes 不认)。把名字列进 `__all__` 是 Python 里表达"这些是本包的
  对外名字"的标准写法, 静态检查据此认账; `load_all()` 则给调用方一个显式入口。
"""
from core.actions import asserts, basic, data_ops, map_ops, timer_ops

__all__ = ["basic", "asserts", "data_ops", "map_ops", "timer_ops"]


def load_all():
    """导入全部动作子模块(幂等)并返回它们 —— 需要"确保动作都注册好了"时显式调用"""
    return (basic, asserts, data_ops, map_ops, timer_ops)
