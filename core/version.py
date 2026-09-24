# -*- coding: utf-8 -*-
"""应用版本号 —— 单一真源(single source of truth)。

★ 为什么单独一个模块, 而不是继续写在 gui/main_window.py 里:
  OTA 需要在**不导入 GUI** 的前提下读到版本号。更新器、打包脚本、"比较远程版本"
  这些逻辑都不该为了拿一个字符串去 import PySide6(打包后还得多带一整份 Qt)。

★ 发版时三处保持一致(见 docs/RELEASE.md 的清单):
      这里 __version__  ==  CHANGELOG.md 的 [x.y]  ==  git tag vx.y

版本号格式用 `主.次` 或 `主.次.修订` 的纯数字点分(便于比较, 也便于 OTA 判断),
不要带 v 前缀 —— 显示时才加。
"""

__version__ = "1.0"

#: 上一次发版对应的日期(CHANGELOG 里那一节的日期), 打包进"关于"信息便于排查
__release_date__ = "2026-09-21"


def parse(v):
    """版本串 → 可比较的元组: "1.2.3" → (1, 2, 3)。

    ★ 必须容错: 远程清单(version.json)是外部数据, 可能是 "v1.2"、"" 甚至 None ——
      解析失败一律给 (0,), 让它排在所有正常版本之前(即"不算更新"), 而不是抛异常
      把整个检查更新的流程打断。
    """
    parts = []
    for seg in str(v or "").strip().lstrip("vV").split("."):
        digits = ""
        for ch in seg:
            if ch.isdigit():
                digits += ch
            else:
                break               # "1.2-beta" 这类后缀直接截断
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def is_newer(remote, local=None):
    """远程版本是否比本地新;本地缺省用当前 __version__。

    比较前补齐长度, 让 "1.2" 与 "1.2.0" 等价(否则 (1,2) < (1,2,0) 会误判为有新版)。
    """
    a = parse(remote)
    b = parse(local if local is not None else __version__)
    n = max(len(a), len(b))
    a = a + (0,) * (n - len(a))
    b = b + (0,) * (n - len(b))
    return a > b
