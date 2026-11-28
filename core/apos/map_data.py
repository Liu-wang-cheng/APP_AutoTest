# -*- coding: utf-8 -*-
"""机器人地图读取: SSH 取 /userdata/run/record.tar.gz 并解析。

文件格式(2026-11-28 实测 10.21):
  map_record.map   原始栅格, width*height 字节, 5cm/格
  map_record.json  宽/高/分辨率/原点
  map.segmentmap   LZ4 压缩的房间分割图(头部 36B, 解压后 width*height 字节)
  area_info.json   已保存区域/禁区/地毯
"""
import json
import struct

from core.logger import get_logger

log = get_logger()

UNKNOWN = 127
SEG_UNKNOWN = 255
SEG_IGNORE = 15          # 分割图里的空白/边界标记


def _extract_to(ssh, remote_dir):
    """在机器人 /tmp 解压 record.tar.gz, 返回远程目录。"""
    dst = "/tmp/autotest_map_view"
    cmd = ("rm -rf %s && mkdir -p %s && "
           "tar -xzf %s/record.tar.gz -C %s" % (dst, dst, remote_dir, dst))
    _i, out, err = ssh.exec_command(cmd, timeout=20)
    out.read()
    err.read()
    return dst


def _read(ssh, path):
    _i, out, _e = ssh.exec_command("cat %s" % path, timeout=20)
    return out.read()


def parse_map_files(raw_map, raw_meta, raw_area, raw_segment=b"",
                    raw_charger=b""):
    """解析地图字节, 返回 dict(纯函数, 便于单测)。"""
    meta = json.loads(raw_meta.decode("utf-8"))
    width = int(meta["width"])
    height = int(meta["height"])
    res = float(meta["resolution"])
    x_min = float(meta["x_min"])
    y_min = float(meta["y_min"])
    grid = raw_map[:width * height]
    segment = b""
    if raw_segment:
        try:
            import lz4.block
            # 头部: [4]version [4]width [4]height [4]res [4]x_min [4]y_min,
            # 数据从 36 字节开始(实测), 解压后正好 width*height
            _ver, seg_w, seg_h = struct.unpack("<III", raw_segment[:12])
            seg_res, seg_x, seg_y = struct.unpack("<fff", raw_segment[12:24])
            segment = lz4.block.decompress(
                raw_segment[36:], uncompressed_size=seg_w * seg_h)
            segment_meta = {"width": seg_w, "height": seg_h,
                            "resolution": seg_res,
                            "x_min": seg_x, "y_min": seg_y}
        except Exception as e:
            log.warning("[地图] 房间分割图解析失败(忽略): %s" % e)
            segment, segment_meta = b"", {}
    else:
        segment_meta = {}
    areas = {}
    if raw_area:
        try:
            areas = json.loads(raw_area.decode("utf-8"))
        except Exception:
            areas = {}
    charger = None
    if raw_charger:
        try:
            charger = json.loads(raw_charger.decode("utf-8")).get("charger_pose")
        except Exception:
            charger = None
    return {"width": width, "height": height, "resolution": res,
            "x_min": x_min, "y_min": y_min, "grid": grid,
            "segment": segment, "segment_meta": segment_meta,
            "areas": areas, "meta": meta, "charger_pose": charger}


def _connect(ip, timeout):
    """按厂商密码表逐个尝试 SSH, 返回 (client, last_error)。"""
    import paramiko
    from core.apos import voice_package as vp

    last = None
    for pwd in vp.SSH_PASSWORDS:
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(ip, username=vp.SSH_USER, password=pwd,
                      timeout=timeout, banner_timeout=timeout,
                      auth_timeout=timeout, look_for_keys=False,
                      allow_agent=False)
            return c, None
        except Exception as e:
            last = e
    return None, last


def fetch_map(ip, timeout=8.0, attempts=2):
    """SSH 读取机器人当前地图; 失败抛异常。

    ★ 机器人负载高时 dropbear 偶尔直接断开握手(Error reading SSH protocol
      banner), 与密码无关 —— 隔 1 秒重连一次基本都能成功(实测 10.21)。
    """
    import time

    ssh = None
    last = None
    for _ in range(max(1, int(attempts))):
        ssh, last = _connect(ip, timeout)
        if ssh is not None:
            break
        time.sleep(1.0)
    if ssh is None:
        raise RuntimeError("SSH 连接失败: %s" % (
            last.__class__.__name__ if last is not None else "unknown"))
    try:
        remote = _extract_to(ssh, "/userdata/run")
        base = remote + "/record"
        raw_map = _read(ssh, base + "/map_record.map")
        raw_meta = _read(ssh, base + "/map_record.json")
        raw_area = _read(ssh, base + "/area_info.json")
        raw_seg = _read(ssh, base + "/map.segmentmap")
        raw_charger = _read(ssh, base + "/charger_pose.json")
        if not raw_map or not raw_meta:
            raise RuntimeError("机器人地图文件不完整")
        return parse_map_files(raw_map, raw_meta, raw_area, raw_seg,
                               raw_charger)
    finally:
        ssh.close()
