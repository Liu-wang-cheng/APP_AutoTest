# -*- coding: utf-8 -*-
"""控制台输出的编码兜底。

★ 为什么需要(2026-10-10 实测)
    Windows 上 Python 的 stdout 编码跟着**控制台代码页**走:
      · 中文系统 = cp936 —— 打印中文没问题, 所以本地一直看不出毛病;
      · 英文系统 = cp1252 —— 打印中文直接
        `UnicodeEncodeError: 'charmap' codec can't encode characters`.
    更隐蔽的是**重定向到文件/管道时同样按 ANSI 代码页**, 不看有没有控制台 ——
    CI(windows-latest) 上我们所有会打印中文的工具都崩在这, 测试全红。
    所以这不只是"CI 环境特殊": 任何把输出重定向走的用户(demo 演示、录制日志、
    接进别的脚本)都会撞上。

★ 处理
    把 stdout/stderr 显式切成 UTF-8 且 errors="replace"(编码不了就替换字符, 不抛异常)。
    重定向到文件时是 UTF-8 字节, 控制台窗口下由 Windows 自己按 UTF-8 解码。
"""
import sys


def force_utf8_stdout():
    """把 stdout/stderr 换成 UTF-8 + 不抛编码错; 返回改动过的流数量。

    没有该流时不报错: 打包成**无控制台**的窗口程序时 `sys.stdout` 可能是 None,
    对 None 调 reconfigure 会 AttributeError —— 那种情况下本来也没有输出。
    """
    changed = 0
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            changed += 1
        except Exception:
            pass                    # 既不是 TextIOWrapper 也不支持重配: 保持原样
    return changed
