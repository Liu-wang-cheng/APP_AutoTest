# 逆向/协议资料

- `sdio_protocol.md`：APP `/dev/csdio` 帧格式（`AA ... checksum BB`，校验=sum^0x55）、水量同步命令。
- 7809 调试流（SUB）与 7813 控制帧（PUSH 心跳）说明见 `core/apos/mcu.py` 顶部注释；
  帧类型 1001 文本日志、1009 MCU 反馈、1008 位姿、1111/1011/1012 传感器/地图流。
- 7813 只是控制通道，不承载日志；AP 上位机同样是订阅 7809 + 向 7813 发心跳。
- 更早的原始反编译文本/抓包输出已归档到 `.scratch/re_archive.zip`（gitignored，可按需解压）。
