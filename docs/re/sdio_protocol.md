# 手机 APP ↔ network 进程 SDIO 帧格式（离线逆向，2026-11-25）

> **定位：仅测试/验证用。** AutoTest 实际控制以 APOS 为主（跨项目 APP 不同，
> APP 协议不能作为产品依赖）。本文件只用于需要验证 APP 显示同步时的测试注入。


来源：`.scratch/app_water_capture.log`（7809 抓包，467 帧，含 APP 改水量/心跳/ACK）。
验证：467/467 帧长度与校验全部匹配；用水量命令帧重新构造后与原始字节完全一致。

## 帧结构

```
AA | ... | 校验 | BB
```

- `AA` 帧头，`BB` 帧尾。
- 倒数第 2 字节是校验：**checksum = (前面所有字节之和 & 0xFF) XOR 0x55**。
- 长度字段有两种格式：
  - 短帧：`byte1`（非 0）= 整帧总长度；
  - 长帧：`byte1 == 0x00`，`byte6` = 整帧总长度，JSON 从 `byte7` 开始。

## 观测到的字段（从抓包推断）

| 字节 | 含义 |
|---|---|
| 0 | `0xAA` 固定帧头 |
| 1 | 短帧总长度；长帧固定 `0x00` |
| 2 | `0xF0` = APP→机器人数据；`0xD1` = 机器人→APP 应答/控制 |
| 3 | 子类型：`0x00` 普通，`0xFF` 心跳 |
| 4 | `0x6A` = JSON 命令包；心跳为 `0xFF` |
| 5 | 客户端 ID（抓包里 APP 为 `0xA2`，另一路为 `0x04`） |
| 6 | 长帧总长度（短帧这里是 payload） |
| 7.. | 长帧 JSON payload |
| -2 | 校验 |
| -1 | `0xBB` 帧尾 |

## 校验函数（Python）

```python
def chk(buf: bytes) -> int:
    return (sum(buf) & 0xFF) ^ 0x55

def build_json_frame(payload: bytes, client_id=0xA2, total_len=None):
    n = total_len or (9 + len(payload))  # 7 头 + payload + 2 尾
    body = bytes([0xAA, 0x00, 0xF0, 0x6A, client_id, 0x00, n]) + payload
    return body + bytes([chk(body), 0xBB])
```

## 校验示例

- 水量设置帧（231B）：
  `AA 00 F0 6A A2 00 E7 <JSON 222B> 02 BB`
  `sum(前 229B)=0x57`，`0x57 ^ 0x55 = 0x02` ✔
- 心跳帧（196B）：`... <JSON> 86 BB`，`sum=0xD3 ^ 0x55 = 0x86` ✔
- ACK（8B）：`AA 08 D1 00 6A A2 DA BB`，`sum=0x8F ^ 0x55 = 0xDA` ✔

## APP 改水量的 JSON

```json
{"Cmd": "Control", "CmdOpt": "Set",
 "Value": {"WATER_SUPPLY_LEVEL": "WATER_SUPPLY_HIGH"},
 "clientId": "832d73...7609", "messageId": "..."}
```

档位映射：

| APP 值 | 机器人挡位 |
|---|---|
| `WATER_SUPPLY_LOW` | 1 |
| `WATER_SUPPLY_LOW1` | 2 |
| `WATER_SUPPLY_HIGH` | 3 |

机器人处理后内部发：

```
change_water_pump {"data": N}
config_file_attribute_change {"WaterLevelUserSet": N}
[status diff] water_level / water_level_user_set
```

## 注入风险

`/dev/csdio` 是 phone/cloud 共用的实时链路（network 进程唯一 fd），
写入需要正确的长度/校验/clientId/messageId；写错可能干扰 APP 或云连接。
建议：机器人空闲、APP 断开时先做受控测试，准备重启 network 服务回滚。
