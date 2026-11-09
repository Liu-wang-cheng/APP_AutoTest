# -*- coding: utf-8 -*-
"""把底层异常翻成用户能看懂的提示（保留原始错误便于排查）。"""


def friendly_error(exc):
    text = str(exc or "").strip()
    low = text.lower()
    original = f"（原始错误：{text}）" if text else ""

    if ("can't find any android device" in low
            or "cannot find any android device" in low
            or "no devices/emulators found" in low
            or "device not found" in low):
        return ("未找到 Android 设备/模拟器。请检查 USB 连接是否正常、"
                "手机是否开启 USB 调试，并在命令行执行 `adb devices`，"
                "确认设备状态为 device。" + original)

    if "more than one device" in low or "multiple devices" in low:
        return ("检测到多个 Android 设备。请断开多余的设备/模拟器，"
                "只保留本轮要测试的一台后重试。" + original)

    if "unauthorized" in low:
        return ("Android 设备未授权。请在手机上点击“允许 USB 调试”，"
                "或重新插拔 USB 后重试。" + original)

    if "offline" in low:
        return ("Android 设备处于离线状态。请重新插拔 USB，"
                "或执行 `adb kill-server` 后重试。" + original)

    if "connection refused" in low or "actively refused" in low:
        return ("无法连接设备上的服务（Connection refused）。"
                "请确认设备在线、APP 已启动，并检查端口是否被占用。" + original)

    if "timed out" in low or "timeout" in low:
        return ("操作超时。请检查设备是否卡死、USB/网络是否稳定，"
                "然后重试。" + original)

    if "not installed" in low or "package" in low and "not found" in low:
        return ("目标 APP 未安装，或包名配置不正确。"
                "请确认 APP 已安装后重试。" + original)

    if "no module named" in low:
        return ("缺少运行依赖。请重新安装工具依赖后再试。" + original)

    return text or type(exc).__name__
