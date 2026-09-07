"""视觉定位: SIFT 特征匹配 + 模板特征缓存

模板图是固定文件,特征点只需检测一次;
屏幕截图的特征每次都要算,由调用方传入灰度图。
"""
import os
import threading

import cv2
import numpy as np
from PIL import Image

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(BASE_DIR, "Test_img", "templates")

_sift = None
_lock = threading.Lock()
_tpl_cache = {}  # (路径, mtime) -> (keypoints, descriptors)


def get_sift():
    """SIFT 检测器单例(创建有开销,全进程共享一个)"""
    global _sift
    if _sift is None:
        with _lock:
            if _sift is None:
                _sift = cv2.SIFT_create(
                    contrastThreshold=0.01,
                    edgeThreshold=5,
                    nOctaveLayers=5,
                )
    return _sift


def template_features(template_path):
    """读取模板并检测特征点,按 (路径, mtime) 缓存,模板更新后自动失效"""
    template_path = str(template_path)
    key = (template_path, os.path.getmtime(template_path))
    if key not in _tpl_cache:
        tpl = np.array(Image.open(template_path).convert("L"))
        h, w = tpl.shape
        if w < 100 or h < 100:  # 小模板放大 3x,保证 SIFT 有足够像素
            tpl = cv2.resize(tpl, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
        kp, des = get_sift().detectAndCompute(tpl, None)
        # 同一路径的过期缓存清掉,避免换图后无限增长
        for k in [k for k in _tpl_cache if k[0] == template_path and k != key]:
            del _tpl_cache[k]
        _tpl_cache[key] = (kp, des)
    return _tpl_cache[key]


def find_in_gray(screen_gray, template_path, min_matches=4, ratio=0.7, inlier_dist=40):
    """SIFT 特征匹配定位模板,返回中心坐标 (x, y),找不到返回 None

    - 只比图标的特征点(角点/边缘/轮廓),不比背景像素
    - Lowe ratio test 过滤模糊匹配
    - 中位数定位 + 离群点过滤(inlier_dist 像素内),抗重复 UI 假阳性
    """
    kp1, des1 = template_features(template_path)
    if des1 is None or len(des1) < 2:
        return None
    kp2, des2 = get_sift().detectAndCompute(screen_gray, None)
    if des2 is None or len(des2) < 2:
        return None

    bf = cv2.BFMatcher()
    try:
        raw_matches = bf.knnMatch(des1, des2, k=2)
    except cv2.error:
        return None

    good = [m for m, n in raw_matches if m.distance < ratio * n.distance]
    if len(good) < min_matches:
        return None

    pts = np.array([kp2[m.trainIdx].pt for m in good])
    median = np.median(pts, axis=0)
    dists = np.linalg.norm(pts - median, axis=1)
    inliers = pts[dists < inlier_dist]
    if len(inliers) < min_matches:
        return None

    mean = np.mean(inliers, axis=0).astype(int)
    return (int(mean[0]), int(mean[1]))
