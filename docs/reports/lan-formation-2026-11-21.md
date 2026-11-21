# 碧游宫 · 联阵协议最小验证服务实测报告

> 生成时间：2026-11-22 +0800　|　协议基准：`docs/lan-formation-contract.md` v1.0　|　任务：`11-21-lan-formation`
> 结论：**验收脚本 3/3 通过（连续 3 次）**；服务端 P0 协议用例可复现；租约离线（L6）与 SSE 事件已补充验证；
> mDNS、Argon2id、非私网源地址拒绝尚未实现，散阵后入阵语义与契约存在偏差，均在下文如实记录。

## 一、背景与目的

联阵契约与最小验证服务已经写完，但仓库里缺少一次可复现的实测记录。本报告补齐：

- 用 `scripts/lan_formation_acceptance.py` 对 `scripts/lan_formation_server.py` 做黑盒验收；
- 覆盖开放阵、闭阵、UDP 发现、心跳租约、踢人/封禁、离阵、散阵、限速；
- 补充验收脚本未覆盖的 L6（心跳停止 30 秒判离线）与 SSE 事件；
- 明确哪些契约条款只是“实现了”，哪些还“没实现”。

本次不涉及 Electron 开阵 UI 与 Android 入阵 App，两者分别属于 `11-21-desktop-client`（S5）与
`11-21-mobile-client`。

## 二、被测对象

| 项 | 路径 | 说明 |
|---|---|---|
| 协议 | `docs/lan-formation-contract.md` | 联阵契约 v1.0，端口 `4022`/`4021`，租约 30s |
| 参考实现 | `scripts/lan_formation_server.py` | 纯标准库 HTTP + UDP 最小验证服务 |
| 验收脚本 | `scripts/lan_formation_acceptance.py` | 自动拉起临时服务、随机端口、仅标准库 |
| 验收分组 | O1–O13 / C1–C8 / D1 | 开放阵 / 闭阵 / UDP 发现 |

关键默认值（实现内常量）：

| 常量 | 值 |
|---|---|
| `PROTOCOL` | `1` |
| `LEASE_TTL_SECONDS` | `30` |
| `HEARTBEAT_INTERVAL_SECONDS` | `10` |
| `RATE_LIMIT_ATTEMPTS` / `WINDOW` | `5` 次 / `60s`（按 `IP + device_id`） |
| 默认容量 | `8` |
| UDP 公告间隔 | `2s` |

## 三、实测环境

| 项 | 值 |
|---|---|
| 系统 | macOS 26.6.2 |
| Python | 3.9.6（仅标准库） |
| 网络 | 本机回环 `127.0.0.1`，HTTP/UDP 均使用随机空闲端口 |
| 影响面 | 每次用例结束即终止子进程，不触碰任何常驻服务 |

## 四、用例与结果

### 4.1 自动化验收

命令：

```bash
python3 scripts/lan_formation_acceptance.py
```

本次连续执行 3 次，结果均为：

```text
[PASS] O1-O13 开放阵：入阵/心跳/踢人/封禁/离阵/散阵
[PASS] C1-C8 闭阵：阵符/容量/协议/限速
[PASS] D1 UDP 发现：discover → room

结果：3/3 通过
```

各分组实际断言：

| 分组 | 覆盖内容 | 结果 |
|---|---|---|
| O（开放阵） | `health` / `room`；无阵主令牌与错误令牌访问成员列表均 403；开放阵入阵 200；心跳 `players=1`；阵主令牌可读成员；踢人 200；被踢后心跳 401 `invalid_token`；同设备重入 403 `banned`；第二台设备入阵/离阵；离阵后心跳 401；协议不兼容 409；阵号不存在 404；设备字段非法 400；同一设备第 6 次入阵 429；散阵 200 且服务在 4s 内退出 | 通过 |
| C（闭阵） | 缺阵符 401 `passcode_required`；错阵符 403 `passcode_invalid`；正确阵符直接入阵 200；容量满 409 `room_full`；协议不兼容 409；踢人释放容量；连续 5 次错阵符后第 6 次 429 | 通过 |
| D（UDP 发现） | 发送 `{"t":"discover","v":1}` 后收到 `t=room`，且 `port` 与 HTTP 端口一致、`protocol=1` | 通过 |

限速响应确认带 `Retry-After: 60`；开放阵与闭阵都验证了“校验通过即入阵，无阵主确认”。

### 4.2 契约用例映射

| 契约编号 | 场景 | 结果 | 证据 / 说明 |
|---|---|---|---|
| L1 | 同网段发现 | 通过 | D1：4s 内收到 `room` 公告 |
| L2 | 开放阵入阵 | 通过 | O：200 + `session_token`，无需确认 |
| L3 | 闭阵错误阵符 | 通过 | C：403 `passcode_invalid` |
| L4 | 闭阵正确阵符 | 通过 | C：200 直接入阵 |
| L5 | 阵满 / 协议不兼容 / 阵已散 | 部分通过 | 前两项通过；**阵已散后入阵为连接拒绝，未返回 `room_closed`**（见 5.1） |
| L6 | 心跳停止 30 秒 | 通过（补充验证） | 36s 后成员 `offline`，SSE 收到 `member.offline`；再次心跳恢复 `active` |
| L7 | 阵主踢人 | 通过 | O：被踢令牌 401，本次阵内重入 403 `banned` |
| L8 | 阵主散阵 | 通过 | O：`closed=true`，服务退出；SSE 下发 `room.closed` |
| L9 | 入阵高频尝试 | 通过 | O/C：429 `rate_limited` + `Retry-After: 60` |
| L10 | 非私网源地址拒绝 | **未实现** | 服务端无源地址校验，默认绑定 `0.0.0.0` |
| L11 | 无 mDNS 时 UDP 兜底 | 通过 | D1；mDNS 本身未实现 |
| L12 | 阵主异常退出 | 服务端无法自证 | 属客户端心跳失败回单机行为，待 desktop S5 / mobile 实现 |

### 4.3 补充验证：租约离线与 SSE

验收脚本不等待 30s 租约，故单独验证：

- 入阵后停止心跳，等待 36s；
- `GET /lan/v1/members` 返回该成员 `status=offline`；
- 期间 SSE 依次收到 `member.joined`、`member.offline`；
- 再次 `POST /lan/v1/heartbeat` 返回 200，成员状态恢复 `active`。

可复现步骤（无需改代码）：

```bash
python3 scripts/lan_formation_server.py --mode open --no-discovery --port 4032 --host-token demo &
ROOM=$(curl -s localhost:4032/lan/v1/room | python3 -c 'import sys,json;print(json.load(sys.stdin)["room"]["room_id"])')
TOKEN=$(curl -s -X POST localhost:4032/lan/v1/join -H 'Content-Type: application/json' \
  -d "{\"protocol\":1,\"room_id\":\"$ROOM\",\"device\":{\"device_id\":\"demo-1\",\"name\":\"demo\",\"platform\":\"android\",\"app_version\":\"0.1.0\"}}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["session_token"])')
sleep 36
curl -s localhost:4032/lan/v1/members -H 'X-Biyou-Host-Token: demo'
# 期望该成员 status=offline；随后带 TOKEN 心跳可恢复为 active
```

## 五、实测发现与偏差

### 5.1 散阵后的入阵语义与契约不一致

契约要求阵已散时返回 409 `room_closed`；最小服务散阵后会直接关闭进程（`shutdown_later(0.5)`），
后续入阵收到的是连接拒绝。

- 影响：验收脚本按“散阵即退出”验证通过，但客户端无法据此区分“阵已散”和“网络不可达”。
- 建议：正式 Electron 服务保留进程，散阵只置 `room.active=false`，入阵返回 `room_closed`；
  或明确把“散阵即退出”写进契约。

### 5.2 非私网源地址拒绝（L10）未实现

契约第十节要求“拒绝非私有源地址”，参考实现没有校验来源 IP，且默认绑定 `0.0.0.0`。
当前仅靠局域网边界防护。正式客户端需要补源地址白名单（RFC1918 / 链路本地 / 回环）。

### 5.3 阵符哈希用 PBKDF2 代替 Argon2id

实现使用 PBKDF2-HMAC-SHA256（20 万次迭代、16 字节盐），契约要求 Argon2id。
已知偏差，正式实现需替换；接口与存储格式可保持不变。

### 5.4 mDNS 未实现，仅 UDP 兜底

发现只实现 UDP 广播（`4021/udp`，2s 公告），mDNS `_biyou._tcp.local.` 未实现（契约 P1）。
UDP 绑定失败时仅打印 `WARN`，不影响 HTTP 服务启动。

### 5.5 已实现且值得保留的安全约束

- 阵主接口（成员列表、踢人、散阵）**仅回环 + `X-Biyou-Host-Token`**，非回环直接 403；
- 会话令牌 32 字节随机，服务端只存 SHA-256 哈希，比较使用常量时间；
- 阵符不写日志；错误响应统一 `{ok:false,error:{code,message}}`；
- 踢人后同 `device_id` 在本次阵内禁止再加入。

### 5.6 范围外

- 不包含任务派发与回禀（属于 `docs/orchestration-contract.md` 与 V2 Agent 契约）；
- 不包含 mDNS、TLS、多网卡、IPv6、二维码配对（契约列为后续项）。

## 六、验收结论

- 服务端 P0 协议链路（发现、入阵、心跳、租约、离阵、踢人、封禁、散阵、限速、SSE）已用可重复脚本验证，
  **连续 3 次 3/3 通过**；
- 契约 L1–L4、L7–L9、L11 通过；L6 经补充验证通过；L5 部分通过（散阵语义偏差）；L10 未实现；
  L12 属客户端侧行为，待桌面端/移动端实现后验收；
- 该服务定位为“协议基准与联调替身”，不是生产实现；正式 Electron/Android 实现需补齐上面 5.1–5.4 的偏差。

## 七、复现命令

```bash
python3 -m py_compile scripts/lan_formation_server.py scripts/lan_formation_acceptance.py
python3 scripts/lan_formation_acceptance.py
```

手动联调：

```bash
python3 scripts/lan_formation_server.py --mode open --port 4022
python3 scripts/lan_formation_server.py --mode closed --passcode 1234 --port 4022
```
