# ECS 夜间节省停机

2026-10-22 已启用阿里云 OOS 云端定时任务。

- ECS：`i-j6ci77fwjtiopeqqxctu`，地域 `cn-hongkong`。
- 每天北京时间 21:00 发起节省停机（`StopCharging`），次日 09:00 发起开机，包含周末。
- OOS 执行 ID：`exec-3f406ef813bb47f08d8f`。
- 官方模板：`ACS-ECS-ScheduleToStartAndStopOrStopAndStartInstances`，版本 `v9`，`StopFirst`。
- API 使用 UTC：停止 `13:00:00Z`，启动 `01:00:00Z`。
- 执行角色：`ecs-night-scheduler-role`。
- 实测状态：`Waiting`，首次触发时间 `2026-10-22T13:00:00Z`，即北京时间当日 21:00。
- 参数及云端返回分别保存在 `deploy/ecs-night-schedule.json`、`deploy/ecs-night-schedule-receipt.json`。

## 公网地址和计费

原公网地址已转换为 EIP `eip-j6crgkufpnacybyxsayj4`，仍绑定原 ECS。
IP 保持 `47.83.186.164`，计费方式 `PayByTraffic`，带宽峰值 5 Mbps。
用户控制台截图显示当前流量单价为 1 元/GB、EIP 配额为 20。
由香港按流量配置和官方规则判断为普通 BGP，ECS API 的 ISP 字段未返回值。
按当前官方规则，BGP EIP 直接绑定 VPC ECS 且账号 EIP 配额不超过 2000 时免 IP 保有费；实际账单仍应在首次出账后核对。
云盘、快照等继续计费；节省停机释放计算资源，再开机仍有库存不足的可能。

## 旧任务清理与恢复

已删除服务器 root crontab 中的旧 ECS 停机项，其余条目保留。
备份在服务器 `/root/ecs-crontab-before-oos-*.bak`。
Mac 的 `com.real-time-voice.ecs-start` 已卸载，plist 移至本仓库 `.local/ecs-scheduler-backup/`。
nginx 与 real-time-voice systemd 服务均已启用开机自启。
本次没有立即停机测试；首次实际停机将在当日 21:00 发生。

## 查询

在阿里云 OOS 控制台选择中国香港地域，按上述执行 ID 查询定时任务。
接口可用 `ListExecutions` 按 `ExecutionId` 过滤查看状态与下次触发时间。
不要重复创建相同任务。修改/停用任务前先确认已有执行状态。

## 2026-10-23 首夜检查（北京时间 09:21 左右）

- OOS 本轮执行 `exec-a9fa14d638bf4e05973a` 成功。
- 停机子执行：2026-10-22 21:00:13 开始，21:00:49 成功；参数为 `StopCharging`。
- 开机子执行：2026-10-23 09:00:54 开始，09:01:07 成功。
- 实际系统启动时间 09:01:15；nginx 与 real-time-voice 于 09:01:27 启动，检查时均 active/running，NRestarts=0。
- 本机 `http://127.0.0.1:8765/health` 返回 `ok=true`；80/443/8765 正常监听，应用本次启动以来无 error 级别 systemd 日志。
- 公网 HTTPS 健康路径返回预期的鉴权 401，连接目标 IP 仍为 `47.83.186.164`。未执行完整语音对话测试。
- 账单 QueryInstanceBill 仍返回 `NotAuthorized`，不能确认夜间计算资源实际计费为零。停机成功与账单零费用分开记录；需给 CatVoiceNlsRole 添加 `deploy/ecs-billing-read-policy.json` 中的账单只读权限后再核对。
- 一次性 Codex 跟进任务已删除，阿里云 OOS 每日开关机任务保持不变，下一次停机为 2026-10-23 21:00。

## 2026-10-23 首夜账单核查补充

用户修正并授权账单只读策略后，QueryInstanceBill 查询成功。
原始日汇总证据（仅保留当前实例和计费字段）在本机 `.local/ecs-billing/20261023-check.json`。
2026-10-22 云服务器配置计费 75637 秒（21 小时 37 秒），原始金额/应付金额均为 17.2977618774 元。
2026-10-23 当前已出账日汇总覆盖到约 10:00（系统盘计费 36000 秒），云服务器配置仅计费 3539 秒（58 分 59 秒），原始金额/应付金额均为 0.8093500048 元。
以上计算项优惠和抵扣为零，与昨晚21点后停止计算计费、今早9点后恢复吻合；不是因优惠导致金额为零。
数据为日汇总，不应把推算的计费边界秒数声称为逐小时账单直接观测。
以计算资源单价0.8233元/小时估算，夜间停止12小时可节省约9.88元/晚；系统盘继续收费。
