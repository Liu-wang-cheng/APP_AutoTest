# -*- coding: utf-8 -*-
"""结构守护: 长轮询循环必须**可中断**(停止/暂停能当场响应)。

背景(2026-10-15 评估结论): 用户反复报过"点了停止要等很久才响应" —— 根因都是某个
轮询循环里用了**裸 `time.sleep`**, 而项目里本来就有可中断的原语:
  · 引擎侧 `ActionRunner._sleep` / `_poll_sleep` —— 停止抛 UserStopped, 暂停挂起并顺延;
  · 前置侧 `core.session._sleep(seconds, should_cancel)` —— 0.2s 粒度响应取消。
实测最坏情况: 用户点停止后 worker 还卡在 restart_app 的 sleep(10) /
_wait_foreground 的 20s 轮询 / ensure_map_loaded 的 6×10s 里, 1~2 分钟才响应,
用户以为卡死就去关窗口(那会引出线程生命周期的问题)。

这条守护用 AST 扫描: 凡是"循环体里有等待"的 `while`, 必须出现可中断原语或停止标志。
新写循环漏掉时它立刻红 —— 比等真机上发现"停不下来"便宜得多。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCAN = [
    "core/runner.py",
    "core/session.py",
    "core/actions/basic.py",
    "core/actions/map_ops.py",
    "core/actions/timer_ops.py",
    "core/actions/asserts.py",
    "core/actions/data_ops.py",
    "core/ocr.py",
]

#: 循环里出现这些调用 = "这一轮要等" → 需要能被打断
WAIT_CALLS = {"sleep", "_sleep", "_poll_sleep"}
#: 出现这些 = 已经是**可中断**的(原语内部会抛 UserStopped / 或显式查了停止标志)
INTERRUPTIBLE = {"_sleep", "_poll_sleep", "_wait_if_paused", "should_cancel",
                 "stopped", "_cancel_check", "UserStopped"}


def _calls(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_polling_loops_are_interruptible():
    """★ 轮询循环里不许只用裸 `time.sleep` —— 停止/暂停会失灵"""
    bad = []
    for rel in SCAN:
        src = (ROOT / rel).read_text(encoding="utf-8")
        for fn in [n for n in ast.walk(ast.parse(src))
                   if isinstance(n, ast.FunctionDef)]:
            fn_calls = _calls(fn)
            for w in [x for x in ast.walk(fn) if isinstance(x, ast.While)]:
                w_calls = _calls(w)
                if not (w_calls & WAIT_CALLS):
                    continue                      # 不等待的循环(纯计算)不管
                if w_calls & INTERRUPTIBLE or fn_calls & INTERRUPTIBLE:
                    continue
                bad.append(f"{rel}:{w.lineno} ({fn.name})")
    assert not bad, (
        "这些轮询循环只用了裸 sleep, 收到停止/暂停要等满超时才响应: "
        f"{bad} —— 请改用 runner._sleep/_poll_sleep 或 session._sleep(should_cancel=)")


def test_default_preconditions_ship_and_seed():
    """★ `Test_preconditions/*.yaml` 是"真实配置也是示例"(用户 2026-10-15):
    它既要**进包**(新装开箱就有可用前置), 又要**首启铺到用户目录**(缺才补),
    还要被 OTA 当用户数据保护(更新不覆盖)。

    所以这条守护盯三处必须一致 —— 少任何一处, 效果就变成"新装的用户没有前置配置"
    或"用户改过的前置被更新覆盖"。
    """
    import inspect
    from core import bootstrap

    # ① 首启"缺才补"的清单里有它
    assert "Test_preconditions" in bootstrap._SEED_DIRS, bootstrap._SEED_DIRS
    # ② 打包脚本把它收进包(与 Test_cases 同款扫描)
    spec = (ROOT / "AutoTest.spec").read_text(encoding="utf-8")
    assert "Test_preconditions" in spec, "打包脚本没把默认前置配置收进包"
    # ③ OTA 用户数据清单里有它(更新不碰)
    from core.driver import USER_DATA_PATHS
    assert "Test_preconditions" in USER_DATA_PATHS
    # ④ 铺默认是"缺才补"(不覆盖用户改过的)
    src = inspect.getsource(bootstrap.ensure_data_dirs)
    assert "os.path.exists(dst)" in src and "continue" in src, \
        "铺默认必须是「已存在就跳过」, 否则会把用户改过的配置覆盖掉"
