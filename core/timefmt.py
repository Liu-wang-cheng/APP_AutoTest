# -*- coding: utf-8 -*-
"""时间戳格式: 全项目**只在这里**定义(结构整理, 用户 2026-10-15)。

★ 为什么要收口
  同一个"运行标签"格式原先在 3 个地方各写一遍字符串(runner_thread / excel_report /
  backup 还是**带横线**的变体), "时分秒"在多处各写一遍 —— 改一处漏一处的典型温床。
  更麻烦的是**解析侧**(core/bootstrap 的 `_shot_case_of` / `_run_tag_of` 正则,
  core/excel_report 的表名)也硬编码了同一套格式: 生产端与解析端格式不一致时,
  表现是"截图封顶永远匹配不上目录"这类静默失效(2026-10-15 实测踩过一次)。

★ 用法
  `time.strftime(timefmt.RUN_TAG)` / `datetime.now().strftime(timefmt.STAMP)`;
  改动这里的格式串时, 请连带看 core/bootstrap.py 的 `_RUN_TAG_RE` 一族正则。
"""

RUN_TAG = "%Y%m%d_%H%M%S"        # 运行标签(截图目录「_r<标签>_第N次」/ 报告文件名)
STAMP = "%Y%m%d-%H%M%S"          # 备份文件名(带横线, 便于肉眼读)
DATE = "%Y%m%d"                  # 只到天(备份目录)
ISO_DATE = "%Y-%m-%d"            # 对外文档/发布说明里的日期
CLOCK = "%H:%M:%S"               # 日志/报错里的时刻(冒号分隔)
CLOCK_TIGHT = "%H%M%S"           # 文件名里的时刻(不能有冒号)
DATETIME = "%Y-%m-%d %H:%M:%S"   # 报告里给人看的完整时间
REPORT_TIME = "%m-%d %H:%M:%S"   # 报告表格里的起止时间(省掉年份)


def run_tag():
    """本次运行的标签(截图目录与报告的命名基准)"""
    import time
    return time.strftime(RUN_TAG)


def clock():
    """当前时刻 `HH:MM:SS`(日志/失败信息用)"""
    import time
    return time.strftime(CLOCK)


def iso_date():
    """今天 `YYYY-MM-DD`"""
    import time
    return time.strftime(ISO_DATE)
