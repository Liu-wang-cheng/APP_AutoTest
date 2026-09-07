import os

import yaml

# 项目根目录:所有路径(配置/用例/图片/报告)都以此为基准,
# 不再依赖 pytest 的启动目录
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_config():
    """加载统一配置"""
    with open(os.path.join(BASE_DIR, "config", "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)
