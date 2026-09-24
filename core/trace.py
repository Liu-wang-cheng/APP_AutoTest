# -*- coding: utf-8 -*-
"""失败时序证据: 每步截屏留内存,失败才落盘。

trace.capture() 记录一步的时序证据(描述 + JPEG 截图,内存中只留最近 N 步);
trace.dump() 失败时落盘 steps.txt + 每步 jpg —— steps.txt 每行:
    序号 <TAB> PASS/FAIL <TAB> 步骤描述 <TAB> 截图文件名
这张表就是失败诊断(以及后续接模型做归因)的输入格式。
"""
import os

import cv2

from core.driver import BASE_DIR
from core.logger import get_logger

log = get_logger()

TRACE_LIMIT = 8
TRACE_JPEG_QUALITY = 70


class TraceRecorder:
    def __init__(self, case_name="", limit=TRACE_LIMIT, jpeg_quality=TRACE_JPEG_QUALITY):
        self.case_name = case_name
        self.limit = limit
        self.jpeg_quality = jpeg_quality
        self._trace = []

    def capture(self, device, desc, passed):
        """记录一步: 截屏存 JPEG 而不是原始位图 —— 一屏 BGR 约 6MB,留 8 步就是
        50MB;JPEG 编码后单张约 100KB。截图失败不影响执行,只丢这一张图。
        """
        if self.limit <= 0:
            return
        shot = None
        try:
            screen = device.screenshot(format="opencv")
            ok, buf = cv2.imencode(
                ".jpg", screen, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if ok:
                shot = buf.tobytes()
        except Exception as e:
            log.debug(f"[trace] 截图失败(不影响执行): {e}")
        self._trace.append({"desc": desc, "passed": passed, "shot": shot})
        if len(self._trace) > self.limit:
            del self._trace[:-self.limit]

    def dump(self, device, step_index, out_root=None):
        """失败时把时序证据落盘,返回目录(无内容返回 "")"""
        trace = self._trace
        if not trace:
            return ""
        prefix = f"{self.case_name}_" if self.case_name else ""
        root = out_root or os.path.join(BASE_DIR, "Test_img", "debug", "failures")
        out_dir = os.path.join(root, f"{prefix}trace")
        try:
            os.makedirs(out_dir, exist_ok=True)
            lines = []
            for n, item in enumerate(trace):
                shot_name = ""
                if item.get("shot"):
                    shot_name = f"{n:02d}_{'ok' if item['passed'] else 'fail'}.jpg"
                    with open(os.path.join(out_dir, shot_name), "wb") as f:
                        f.write(item["shot"])
                lines.append("\t".join([
                    f"{n:02d}",
                    "PASS" if item["passed"] else "FAIL",
                    item["desc"],
                    shot_name,
                ]))
            with open(os.path.join(out_dir, "steps.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            log.error(f"[诊断] 失败前 {len(lines)} 步的时序证据已保存: {out_dir}")
            return out_dir
        except Exception as e:
            log.warning(f"[trace] 落盘失败: {e}")
            return ""

    def failure_screenshot(self, device, step_index):
        """失败截图存到 Test_img/debug/failures/,文件名带用例名避免跨用例覆盖"""
        prefix = f"{self.case_name}_" if self.case_name else ""
        fail_path = os.path.join(BASE_DIR, "Test_img", "debug", "failures",
                                 f"{prefix}step_{step_index:02d}_fail.png")
        try:
            os.makedirs(os.path.dirname(fail_path), exist_ok=True)
            device.screenshot(fail_path)
            return fail_path
        except Exception:
            return ""
