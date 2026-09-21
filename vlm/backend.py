# -*- coding: utf-8 -*-
"""VLM 可插拔视觉后端。

引擎(ActionRunner)只认 VisionRouter,不感知后端是 SIFT 还是 GLM-4V。
当前只实现 SiftBackend(透传 core.vision);VLM 本体不实现 —— 未来接
GLM-4V 时新增 Glm4vBackend(VisionBackend) 并在 config 打开 vlm.enabled。
"""
from typing import Optional, Protocol, Tuple

from core import vision


class VisionBackend(Protocol):
    def locate(self, screen_gray, template_path: str,
               min_matches: int = 4) -> Optional[Tuple[int, int]]:
        """在灰度屏图中定位模板,返回中心坐标;找不到返回 None。"""
        ...

    def assert_on_screen(self, screen_gray, expectation: str) -> Tuple[bool, str]:
        """断言 screen 满足自然语言 expectation,返回 (是否通过, 证据说明)。"""
        ...


class SiftBackend:
    """默认后端: 直接透传 core.vision 的 SIFT 匹配。"""

    def locate(self, screen_gray, template_path, min_matches=4):
        return vision.find_in_gray(screen_gray, template_path, min_matches=min_matches)

    def assert_on_screen(self, screen_gray, expectation):
        raise NotImplementedError("VLM 后端未启用; 文本/图片断言请用传统动作")


def load_vlm_backend(cfg: dict):
    """按 config 的 vlm 段加载 VLM 后端;未启用返回 None。"""
    vlm_cfg = (cfg or {}).get("vlm") or {}
    if not vlm_cfg.get("enabled"):
        return None
    impl = vlm_cfg.get("impl", "vlm.glm4v:Glm4vBackend")
    mod_name, cls_name = impl.split(":")
    import importlib
    return getattr(importlib.import_module(mod_name), cls_name)(vlm_cfg)


class VisionRouter:
    """runner 只认这个。vlm 未启用时全部走 SIFT。"""

    def __init__(self, cfg: dict):
        self.sift = SiftBackend()
        self.vlm = load_vlm_backend(cfg)

    def locate(self, screen_gray, template_path, min_matches=4):
        return self.sift.locate(screen_gray, template_path, min_matches=min_matches)
