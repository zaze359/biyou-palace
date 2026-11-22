# 联阵协议契约（LAN Formation Contract）

> 版本：v1.0 · 2026-11-21  
> 状态：V2.0 草案  
> 范围：局域网「开阵 / 寻阵 / 入阵 / 离阵 / 踢人 / 散阵」的发现、会话与准入协议。  
> 不包含：模型推理（见 `node-agent-contract.md`）、任务编排与道果（见 `orchestration-contract.md`）。

---

## 一、角色与术语

| 术语 | 说明 |
|---|---|
| **阵** | 一个局域网联阵会话（Room） |
| **阵主 / 阵眼** | 创建阵的桌面端；负责发现广播、准入校验、成员与租约 |
| **仙徒** | 入阵的移动端或其他客户端；默认角色为外门仙徒 |
| **寻阵端** | 正在浏览局域网阵列表的客户端 |
| **阵符** | 闭阵的入阵口令；不是账号密码 |
| **开放阵** | 无阵符；校验通过即入阵 |
| **闭阵** | 有阵符；阵符校验通过即入阵 |
| **阵散** | 阵主退出，联阵结束，各端回单机 |

关键原则：

- **无阵主确认**：开放阵与闭阵都不需要阵主逐次点“同意”；校验通过即入阵。
- **阵主可踢人**：入阵后阵主可撤销会话。
- **默认单机**：桌面端启动不自动开阵；移动端启动不自动寻阵。
- **仅局域网**：无云大厅、无账号、无公网发现。

---

## 二、传输与端口

| 用途 | 协议 | 默认 |
|---|---|---|
| 服务发现 | mDNS | `_biyou._tcp.local.` |
| 服务发现兜底 | UDP 广播 | `4021/udp`（可配置） |
| 会话 API | HTTP/JSON | 阵主动态端口，发现报文携带 |
| 事件流 | SSE | `GET /lan/v1/events` |

约定：

- 阵主启动开阵时选择一个可用的 HTTP 端口（默认从 `4022` 起，冲突则递增），并在 mDNS TXT / UDP 报文中公布。
- 发现只使用局域网接口；不跨路由、不上公网。
- 所有 JSON 使用 UTF-8；时间使用 RFC 3339 UTC。
- 未知字段必须忽略；新增字段必须可选。

---

## 三、发现协议

### 3.1 mDNS

服务类型：

```text
_biyou._tcp.local.
```

实例名建议：

```text
<阵名>._biyou._tcp.local.
```

TXT 记录：

| Key | 必选 | 示例 | 说明 |
|---|---|---|---|
| `v` | 是 | `1` | 协议主版本 |
| `room_id` | 是 | `550e8400-...` | 阵的唯一 ID |
| `name` | 是 | `碧游宫` | 阵名，UTF-8 |
| `mode` | 是 | `open` / `closed` | 开放阵 / 闭阵 |
| `protocol` | 是 | `1` | 与 `v` 一致，便于读取 |
| `capacity` | 是 | `8` | 最大仙徒数，不含阵主 |
| `players` | 是 | `2` | 当前仙徒数 |
| `host` | 是 | `书房的 Mac` | 阵主显示名 |
| `app` | 是 | `0.1.0` | 客户端版本 |
| `port` | 是 | `4022` | 会话 HTTP 端口 |

> TXT 中不得包含阵符、令牌或任何秘密。

### 3.2 UDP 广播兜底

用于 mDNS 被屏蔽或平台不支持的情况。

- 端口：`4021/udp`（可配置）。
- 广播地址：`255.255.255.255` 与各局域网接口的定向广播地址。
- TTL：`1`。
- 报文为单行 JSON，UTF-8，建议小于 1200 字节。

阵主公告（每 2 秒一次）：

```json
{
  "t": "room",
  "v": 1,
  "room_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "碧游宫",
  "mode": "open",
  "protocol": 1,
  "capacity": 8,
  "players": 2,
  "host": "书房的 Mac",
  "app": "0.1.0",
  "port": 4022,
  "started_at": "2026-11-21T10:00:00Z"
}
```

寻阵端查询（可选，用于立即刷新）：

```json
{ "t": "discover", "v": 1 }
```

阵主收到查询后，单播回复一条 `room` 报文。

### 3.3 列表合并与过期

- 寻阵端按 `room_id` 合并 mDNS 与 UDP 结果。
- 超过 `6s` 未收到公告，从列表移除。
- 协议不兼容的阵仍显示，但标记为不可入阵。
- 排序建议：最近公告优先，其次阵名。

---

## 四、阵模型

```json
{
  "protocol": 1,
  "room_id": "550e8400-e29b-41d4-a716-446655440000",
  "name": "碧游宫",
  "mode": "open",
  "host": {
    "name": "书房的 Mac",
    "platform": "macos",
    "app_version": "0.1.0"
  },
  "capacity": 8,
  "players": 2,
  "started_at": "2026-11-21T10:00:00Z",
  "lease": {
    "ttl_seconds": 30,
    "heartbeat_interval_seconds": 10
  }
}
```

- `capacity` 只统计仙徒，不含阵主。
- `mode`：`open` 或 `closed`。
- `players` 为当前在线仙徒数。

---

## 五、HTTP API

基础路径：`/lan/v1`。

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `GET` | `/health` | 无 | 存活探测 |
| `GET` | `/room` | 无 | 阵信息（不含秘密） |
| `POST` | `/join` | 无 | 入阵 |
| `POST` | `/heartbeat` | Bearer | 续租 |
| `POST` | `/leave` | Bearer | 主动离阵 |
| `GET` | `/events` | Bearer | SSE 事件流 |
| `GET` | `/members` | 阵主 | 成员列表 |
| `POST` | `/members/{member_id}/kick` | 阵主 | 踢人 |
| `POST` | `/disband` | 阵主 | 散阵 |

阵主接口鉴权：

- 仅接受来自回环地址的请求；
- 并要求 `X-Biyou-Host-Token: <host_token>`，该 token 只保存在阵主 UI 与本地进程内。

仙徒接口鉴权：

```text
Authorization: Bearer <session_token>
```

---

## 六、入阵

### 6.1 请求

```http
POST /lan/v1/join
Content-Type: application/json
```

```json
{
  "protocol": 1,
  "room_id": "550e8400-e29b-41d4-a716-446655440000",
  "passcode": "1234",
  "device": {
    "device_id": "android-8f3a...",
    "name": "客厅的手机",
    "platform": "android",
    "app_version": "0.1.0"
  },
  "agent": {
    "root": "outer",
    "skills": ["chat", "ocr", "embedding"],
    "models": ["qwen3-0.6b"],
    "max_concurrency": 1
  }
}
```

- 开放阵省略 `passcode`。
- 闭阵必须提供 `passcode`。
- `device_id` 由客户端生成并持久化；同一设备重装后可变化。
- `agent` 为能力自述，用于阵主派差；不得包含秘密。

### 6.2 校验顺序

1. 限速：同一 `IP + device_id` 每分钟最多 5 次入阵尝试；
2. 协议版本兼容；
3. 阵仍在开阵状态；
4. 容量未满（`players < capacity`）；
5. `device` 字段合法；
6. 是否在本次阵的封禁列表；
7. 闭阵阵符校验；
8. 签发会话。

> 先限速再校验阵符，避免暴力尝试。

### 6.3 成功响应

```json
{
  "ok": true,
  "member_id": "7b1c...",
  "session_token": "eyJ...",
  "role": "outer",
  "room": {
    "protocol": 1,
    "room_id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "碧游宫",
    "mode": "open",
    "capacity": 8,
    "players": 3
  },
  "lease": {
    "ttl_seconds": 30,
    "heartbeat_interval_seconds": 10
  }
}
```

- `session_token` 只返回一次，客户端必须安全保存。
- 阵主保存 token 的哈希，不保存明文。

### 6.4 失败响应

统一结构：

```json
{
  "ok": false,
  "error": {
    "code": "passcode_invalid",
    "message": "阵符错误"
  }
}
```

错误码：

| code | HTTP | 说明 |
|---|---|---|
| `rate_limited` | 429 | 入阵尝试过于频繁；带 `Retry-After` |
| `protocol_mismatch` | 409 | 协议版本不兼容 |
| `room_not_found` | 404 | 阵不存在或已更换 |
| `room_closed` | 409 | 阵已散 |
| `invalid_request` | 400 | 请求体不是 JSON 对象 |
| `invalid_token` | 401 | 会话不存在或已失效 |
| `member_not_found` | 404 | 踢人时成员不存在 |
| `room_full` | 409 | 阵已满 |
| `invalid_device` | 400 | 设备字段缺失或非法 |
| `banned` | 403 | 本次阵已封禁该设备 |
| `passcode_required` | 401 | 闭阵未提供阵符 |
| `passcode_invalid` | 403 | 阵符错误 |
| `internal_error` | 500 | 阵主内部错误 |

---

## 七、心跳与租约

### 7.1 心跳

```http
POST /lan/v1/heartbeat
Authorization: Bearer <session_token>
```

响应：

```json
{
  "ok": true,
  "server_time": "2026-11-21T10:00:10Z",
  "lease_expires_at": "2026-11-21T10:00:40Z",
  "players": 3
}
```

- 默认每 10 秒一次。
- 租约 TTL 30 秒。
- 连续 3 次未续租，阵主标记该仙徒 `offline`，回收其未完成任务并重派。
- 网络抖动时客户端指数退避重试；恢复后重新入阵或续租。

### 7.2 离线判定

```text
active → offline  （租约过期）
offline → active  （重新心跳/重入阵）
active → left     （主动离阵）
active → kicked   （被阵主踢出）
```

---

## 八、离阵、踢人与散阵

### 8.1 主动离阵

```http
POST /lan/v1/leave
Authorization: Bearer <session_token>
```

```json
{ "ok": true }
```

阵主收到后：

- 撤销 token；
- 标记 `left`；
- 回收未完成任务；
- 广播 `member.left`。

### 8.2 踢人

```http
POST /lan/v1/members/{member_id}/kick
X-Biyou-Host-Token: <host_token>
```

```json
{ "ok": true }
```

- 撤销 token；
- 将 `device_id` 加入本次阵的封禁列表；
- 广播 `member.kicked`；
- 被踢端收到事件后清理会话，回单机。
- 封禁只对本次阵有效；阵散后失效。

### 8.3 散阵

阵主退出或点击「散阵」：

1. 广播 `room.closed`；
2. 停止 mDNS/UDP 公告；
3. 关闭 SSE；
4. 撤销所有 token；
5. 关闭 HTTP 服务。

客户端行为：

- 收到 `room.closed` 或连续心跳失败：清理 token，回单机；
- 本地任务与产物不删除；
- 未完成任务标记为“阵散中断”，等待用户重新开阵或单机执行。

---

## 九、事件流

```http
GET /lan/v1/events
Authorization: Bearer <session_token>
Accept: text/event-stream
```

事件格式：

```text
event: member.joined
data: {"member_id":"7b1c...","name":"客厅的手机","role":"outer"}
```

| event | 说明 |
|---|---|
| `room.state` | 人数/容量变化 |
| `member.joined` | 新仙徒入阵 |
| `member.left` | 主动离阵 |
| `member.offline` | 租约过期，标记离线 |
| `member.kicked` | 被阵主踢出 |
| `room.closed` | 阵主散阵 |
| `task.assigned` | 可选；任务派发提示，具体任务见编排契约 |

约定：

- 每 15 秒发送一次 `: keepalive` 注释。
- `member.kicked` / `room.closed` 发送后关闭该 SSE 连接。
- 事件只用于通知；任务状态以编排契约为准。

---

## 十、安全

1. **仅局域网**：只绑定局域网接口；拒绝非私有源地址。
2. **开放阵风险提示**：开阵 UI 明确提示“同局域网任何人都可能加入”。
3. **闭阵阵符**：4–64 字符；只存 Argon2id 哈希；常量时间比较；不写日志。
4. **限速**：入阵 5 次/分钟/IP+device；超限返回 429。
5. **令牌**：32 字节随机；只存哈希；不写日志；踢人/离阵/散阵即撤销。
6. **阵主接口**：仅回环 + `X-Biyou-Host-Token`。
7. **无公网**：不做 NAT 穿透、不做云大厅、不上传设备信息。
8. **V1 不启用 TLS**：仅在可信局域网使用；后续可加 TLS 与指纹校验。
9. **封禁**：踢人后本次阵内禁止同 `device_id` 再加入。

---

## 十一、状态机

### 11.1 客户端

```text
idle
  → discovering     点击「寻阵」
  → joining         选择阵并提交
  → joined          校验通过
  → leaving         主动离阵
  → idle

joined
  → disconnected    心跳失败 / SSE 断开
  → idle            回单机

joined
  → kicked          收到 member.kicked
  → idle

joined
  → room_closed     收到 room.closed
  → idle
```

### 11.2 阵主

```text
standalone
  → hosting         点击「开阵」
  → disbanding      点击「散阵」/退出
  → standalone
```

---

## 十二、时序

### 12.1 开放阵入阵

```text
桌面端                     移动端
  │ 开阵(open)                │
  │── mDNS/UDP room ────────▶ │
  │                           │ 寻阵，选择阵
  │ ◀──── POST /join ─────────│
  │ 校验：协议/容量/设备/限速  │
  │ ───── 200 token ─────────▶│
  │ ◀──── heartbeat ──────────│
  │ ───── SSE events ────────▶│
```

### 12.2 闭阵入阵

```text
移动端：选择闭阵 → 输入阵符 → POST /join
阵主：限速 → 协议 → 容量 → 设备 → 封禁 → 阵符
通过：200 + token；失败：passcode_invalid
无阵主确认步骤
```

### 12.3 踢人与散阵

```text
阵主：kick → 撤销 token → 加封禁 → SSE member.kicked
移动端：收到事件 → 清理会话 → 回单机

阵主：disband → SSE room.closed → 停止公告 → 关闭服务
移动端：收到事件/心跳失败 → 回单机，保留本地数据
```

---

## 十三、与现有契约的关系

| 契约 | 负责 |
|---|---|
| 本契约 | 发现、入阵、心跳、离阵、踢人、散阵 |
| `node-agent-contract.md` | 节点推理端点 `/v1/health`、`/v1/models`、`/v1/chat/completions`、`/v1/embeddings` |
| `orchestration-contract.md` | 师门任务、派差、回禀、道果、事件 |

入阵成功后：

- 阵主为仙徒分配 `member_id`；
- 仙徒的 `agent` 能力写入阵主玉册；
- 任务派发使用编排契约，`member_id` 作为 `agent_id` 使用；
- 本契约不定义任务内容。

---

## 十四、验收用例

| 编号 | 场景 | 期望 |
|---|---|---|
| L1 | 桌面开阵，Android 同网段寻阵 | 5 秒内出现在阵列表 |
| L2 | 开放阵入阵 | 无阵符，校验通过即入阵 |
| L3 | 闭阵错误阵符 | `passcode_invalid`，不入阵 |
| L4 | 闭阵正确阵符 | 直接入阵，无阵主确认 |
| L5 | 阵满 / 协议不兼容 / 阵已散 | 分别返回明确错误码 |
| L6 | 心跳停止 30 秒 | 标记 offline，任务回收/重派 |
| L7 | 阵主踢人 | 被踢端收到事件，回单机，本次阵禁止再加入 |
| L8 | 阵主散阵 | 各端回单机，本地数据不丢 |
| L9 | 入阵高频尝试 | 429 + `Retry-After` |
| L10 | 公网/非私有地址访问 | 拒绝 |
| L11 | Android 无 mDNS | UDP 兜底仍能发现 |
| L12 | 阵主异常退出 | 客户端心跳失败后回单机，不崩溃 |

---

## 十五、参考实现与验收

- 最小服务：[`scripts/lan_formation_server.py`](../scripts/lan_formation_server.py)
- 可重复验收：[`scripts/lan_formation_acceptance.py`](../scripts/lan_formation_acceptance.py)
- 桌面端正式实现：[`desktop/electron/lan-host/`](../desktop/electron/lan-host/)（Electron 主进程；UDP + HTTP/SSE，闭阵阵符用 Argon2id；mDNS 仍为 P1）
- 桌面端集成测试：`cd desktop && npm run test:lan`

```bash
python3 scripts/lan_formation_acceptance.py
```

覆盖：

- 开放阵入阵、心跳、成员列表、踢人、封禁、离阵、散阵；
- 闭阵缺阵符、错阵符、正确阵符、阵满、协议不兼容、限速；
- UDP `discover` → `room` 发现。

Python 参考实现用 PBKDF2 代替 Argon2id，且只实现 UDP 发现；桌面端已换 Argon2id，mDNS 仍在 P1 补齐。

## 十六、未决与后续

1. UDP 发现端口 `4021` 是否固定，还是允许用户配置。
2. V1 是否启用 TLS；若启用，证书与指纹如何展示。
3. 阵主迁移（Host Migration）：V1 不支持，阵主退出即散阵。
4. 多网卡/多 VLAN 时绑定哪个接口。
5. IPv6 发现是否纳入 V1。
6. 二维码配对是否作为阵列表之外的补充方式。
