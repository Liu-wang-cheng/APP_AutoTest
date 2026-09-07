import uiautomator2 as u2
import yaml


def load_config():
    """加载统一配置"""
    with open("config/config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def connect_device(device_id=None):
    """连接设备并启动 APP"""
    cfg = load_config()
    device_cfg = cfg["device"]
    app_cfg = cfg["app"]

    if device_id is None or device_id == "auto":
        device_id = device_cfg["default"]
        if device_id == "auto":
            device_id = device_cfg["list"][0]["id"]

    d = u2.connect(device_id)
    d.implicitly_wait(10)
    d.app_start(app_cfg["package"], app_cfg["main_activity"])
    return d
