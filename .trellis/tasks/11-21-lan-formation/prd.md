# 碧游宫 V2 · 联阵协议与最小验证服务（开阵/寻阵/入阵）

## Goal

完成局域网开阵、寻阵、入阵、心跳、离阵、踢人、散阵协议，并用桌面端最小 HTTP 服务验证；为 Android 入阵与 Electron 开阵提供可执行基准。

## Scope

- P0：UDP 广播发现、开放阵/闭阵、免阵主确认入阵、心跳租约、离阵/踢人/散阵、SSE 事件、限速、会话令牌。
- P0：桌面端最小验证服务 `scripts/lan_formation_server.py`。
- P1：mDNS、TLS、多网卡、IPv6、二维码补充。

## Requirements

1. 遵循 `docs/lan-formation-contract.md` v1.0。
2. 桌面端默认单机，不广播；用户主动开阵后才成为阵主/阵眼。
3. 开阵二选一：开放阵（无阵符）或闭阵（有阵符）；两者都无阵主确认，校验通过即入阵。
4. 入阵校验顺序：限速 → 协议 → 阵号/阵状态 → 容量 → 设备 → 封禁 → 阵符 → 签发令牌。
5. 心跳 10s、租约 30s；连续 3 次未续租标记 offline。
6. 踢人撤销令牌并对本次阵封禁 `device_id`；散阵广播 `room.closed` 并停止公告。
7. 不依赖云大厅、账号、公网；mDNS 未实现时 UDP 兜底仍可发现。

## Acceptance Criteria

- [ ] 契约 L1–L12 用例可通过 curl/脚本复现。
- [ ] 开放阵无需阵符入阵；闭阵错误阵符拒绝，正确阵符直接入阵。
- [ ] 阵满、协议不兼容、阵已散、封禁设备均返回明确错误码。
- [ ] 心跳停止 30s 后标记 offline；踢人后设备回单机且本次阵禁止再加入。
- [ ] 阵主散阵后各端回单机，本地数据不丢。
- [ ] UDP 发现可在同网段发现阵；无 mDNS 时仍可用。
- [ ] `python3 -m py_compile scripts/lan_formation_server.py` 通过。

## Known Deviations

- 最小服务用 PBKDF2 代替约定的 Argon2id；生产实现需换 Argon2id。
- 最小服务只实现 UDP 发现，mDNS 列为 P1。
- 最小服务不包含任务派发，任务仍走编排契约。

## Dependencies

- 无（协议可独立推进）；Android 入阵依赖 `11-21-mobile-client`。

## Out of Scope

- Electron 开阵 UI（见 `11-21-desktop-client`）。
- Android 寻阵/入阵 App（见 `11-21-mobile-client`）。
