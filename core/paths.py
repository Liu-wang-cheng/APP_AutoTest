# -*- coding: utf-8 -*-
"""路径与文件名: 全项目**只在这里**拼这几类路径(结构整理, 用户 2026-10-15)。

★ 为什么要收口
  `Test_cases/<组>/screenshots/...` 与「`screenshots/` 前缀补 `Test_img/`」这两条规则
  原先散在 runner / runner_thread / asserts / bootstrap / vision 五处各写一遍,
  `screenshots/` 那条甚至有 3 份**写法都不一样**的副本。规则一改就得同步 N 个地方,
  已经出过"改了一处、另一处还在用旧格式"的返工。

★ 改这里要连带看的
  · tests/conftest.py 的 BASE_DIR_MODULES(本模块绑定了 BASE_DIR, 必须被钉到临时目录);
  · 截图目录命名「<用例键>_r<运行标签>_第N次」由 bootstrap 的
    `_shot_case_of` / `_run_tag_of` / `_round_of` 反向解析 —— 命名规则改了要同步。
"""
import os
import re

from core.driver import BASE_DIR

#: 文件名里不能出现的字符(Windows)+ 所有空白
_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def safe_name(text, limit=40):
    """任意文本(用例描述/模板名/前置名) → 安全的文件名片段。

    ★ 全项目统一一套(原先两套: runner 用正则 + 截 30 字, runner_thread 用字符黑名单
      + 截 40 字 —— 同一个描述在两处得到**不同的文件名**, 排查时对不上号)。
    """
    out = _BAD_CHARS.sub("_", str(text or "")).strip("_")
    return out[:limit] or "未命名"


def group_case_dir(group=None):
    """APP 组目录: Test_cases/<组>/(用例 / 模板 / 截图都收在这里, 用户要求)

    group 为空时用**当前执行中的组**(core.vision.current_app_group());
    全局模板目录已取消, 模板一律走 core.vision.group_templates_dir(基于本函数)。
    """
    if not group:
        from core import vision            # 延迟导入: 避免与 vision 循环依赖
        group = vision.current_app_group()
    return os.path.join(BASE_DIR, "Test_cases", group or "未分组")


def group_screenshots_dir(group=None):
    """组截图目录: Test_cases/<组>/screenshots/"""
    return os.path.join(group_case_dir(group), "screenshots")


def shot_dir(sub, group=None):
    """某一次执行的截图目录(绝对路径): screenshots/<sub>/

    sub 就是 runner.shot_subdir(形如「甲用例_r20260101_090000_第1次」);
    前置步骤与用例步骤**共用**同一个目录(用户 2026-10-15: "同一次测试任务的截图
    都应该放同一目录下" —— 靠文件名前缀区分, 见 runner.shot_prefix)。
    """
    return os.path.join(group_screenshots_dir(group), str(sub or "case"))


def resolve_path(path):
    """把用例里写的路径解析成绝对路径 —— **一套规则**, 全项目共用。

      · 已经是绝对路径 → 原样;
      · `screenshots/...` 开头 → 补 `Test_img/`(老的截图目录约定);
      · 其余相对路径     → 相对 BASE_DIR。
    """
    p = str(path or "")
    if os.path.isabs(p):
        return p
    if p.startswith("screenshots/"):
        return os.path.join(BASE_DIR, "Test_img", p)
    return os.path.join(BASE_DIR, p)
