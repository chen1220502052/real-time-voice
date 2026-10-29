# ECS 按需开机与空闲停机

2026-10-26 配置。用户要求：部署或测试时按需开机，30分钟无业务访问且无部署测试时节省停机，21:00固定停机兜底，不再每天9点开机。

## 已启用

- ECS 上 `ecs-idle-guard.timer` 每分钟检查一次。
- `ecs_idle_guard.py` 以最近成功业务访问、开机时间、部署测试保护时间中较新者计时。连续1800秒空闲后通过 OOS 请求 `StopCharging`；实际检查周期会带来约一分钟偏差。
- nginx 只记录成功业务请求的时间戳到 `/var/log/nginx/ecs-activity.log`，不记录账号、正文、Cookie或查询串；登录页、静态文件、健康检查及后台诊断轮询不续期。
- 活跃语音会话、8765端口未结束的连接、部署测试共享锁都会阻止空闲停机。检查失败时保守保持开机。
- 新的21:00只停机任务：`exec-0211776a5a84458ab11d`。参数在 `deploy/ecs-stop-only-schedule.json`，明确 `StopCharging`，没有启动动作。
- `deploy_ssh.py` 与兼容入口 `ssh_deploy.py` 会在整个远程命令或SFTP操作期间持有保护锁，结束时重新开始30分钟计时。

## 尚待完成（不可声称已完全切换）

1. 旧每日开关机任务 `exec-3f406ef813bb47f08d8f` 仍为 Waiting，仍可能每天9点启动实例。用户希望保留旧任务。核对原模板v9和UpdateExecution API后，确认该API只能修改参数，不能删除模板中的启动动作。可通过修改旧任务的 `triggerEndDate` 让其到期，保留历史记录，由新的21点只停机任务接替。2026-10-26实测 `UpdateExecution` 与 `CancelExecution` 均无权限。下一步将 `deploy/ecs-update-old-schedule-policy.json` 授权给 CatVoiceNlsRole 后，修改结束时间并核验；修改前还需核对是否有尚未结束的旧子执行，避免仍在等待9点启动。不要将两个任务并存称为已取消每天9点开机。
2. 本机缺少独立的阿里云启动身份。ECS停止时不能从实例元数据获取临时凭据，因此不能依赖该实例角色给自己开机。启动脚本已实现，但在独立身份配置之前只能控制台开机。

## 控制台开机

进入阿里云 ECS 控制台，地域选中国香港，搜索 `i-j6ci77fwjtiopeqqxctu`，选择启动。等待运行中和健康检查正常后访问 https://aimaoren.com/。EIP保持 `47.83.186.164`。
网站本身位于这台ECS，关机时不能打开网页来唤醒服务器。

## 本机自动开机身份

创建专用RAM用户，仅授予 `deploy/ecs-local-start-policy.json`：只允许启动这台ECS，以及查询实例状态。将凭据保存在本机 `~/.config/aliyun-ecs.env`，权限0600：

```dotenv
ALIYUN_ACCESS_KEY_ID=由用户本机填写
ALIYUN_ACCESS_KEY_SECRET=由用户本机填写
```

不要将密钥粘贴到对话、写入仓库或保存实例角色临时凭据作为长期身份。配置该独立身份后，部署脚本会先尝试SSH；若连接失败，调用ECS启动接口，等待运行和SSH可用后继续。

手动调用启动脚本：

```sh
.local/deploy-venv/bin/python scripts/ecs_power.py start
```

## 部署与测试保护

远程操作继续用 `scripts/deploy_ssh.py`。跨多步的部署或本地测试用：

```sh
.local/deploy-venv/bin/python scripts/ecs_use.py -- COMMAND ARGUMENTS
```

需要跨浏览器工具操作时，开启持久PTY运行上述脚本加 `/bin/cat`，操作结束发送EOF释放保护。不要为规避停机伪造健康请求。保护只针对30分钟空闲停机，21:00兜底仍会按计划停止。

## 验证与回退

安全测试覆盖未满30分钟、活跃连接/会话、健康失败、维护锁、两次检查之间新增访问、日志异常等分支。服务器 dry-run 已验证最近活动/维护锁保持开机。
仅 reload nginx 并安装独立systemd timer，不更改应用代码或重启业务服务。
暂时停用空闲守护可运行 `systemctl disable --now ecs-idle-guard.timer`，这不会改变OOS云端调度。
