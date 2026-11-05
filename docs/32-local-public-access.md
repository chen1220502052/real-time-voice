# 通过香港 ECS 开放本机语音服务

公网入口使用 `https://local.aimaoren.com`。浏览器通过 HTTPS 和 WSS 连接香港 ECS 上的
Nginx。Nginx 把请求送到 ECS 的 `127.0.0.1:18765`，由 Mac 主动建立的 SSH 反向隧道
转到 Mac 的 `127.0.0.1:8765`。网站后端、模型推理、账号和学习记录都由 Mac 上的服务处理。
已有主站使用独立入口和数据库，只有在本机数据库里已有账号的人才能直接登录试用站。

## 准备

1. 在阿里云 ECS 控制台选择中国香港，启动实例 `i-j6ci77fwjtiopeqqxctu`。确认实际 SSH 端口
   可从 Mac 当前网络访问。按 [ECS 按需开机](16-ecs-on-demand.md) 配置独立 RAM 身份后，
   部署工具可按需开机。公网访问无法唤醒已停机的 ECS。
2. 在 Mac 上用本地编辑器填写权限为 `0600` 的 `.env.deploy`。使用
   `DEPLOY_HOST=47.83.186.164`，并填写实际的 `DEPLOY_USER`、`DEPLOY_PORT` 和
   `DEPLOY_PASSWORD`。通过可信控制台读取 ECS 的 SSH 主机密钥指纹，例如在 ECS 上运行
   `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`。在 Mac 上用
   `ssh-keyscan -t ed25519 -p 22 47.83.186.164` 获取待核对的公钥（按实际 SSH 端口调整），
   用 `ssh-keygen -lf 公钥文件` 比对指纹，一致后再将该记录加入 `~/.ssh/known_hosts`。
   管理员密码和 SSH 私钥只留在本机。
3. 在阿里云 DNS 的 `aimaoren.com` 区域添加一条主机记录 `local`，类型 `A`，记录值
   `47.83.186.164`。

## 配置 ECS

从仓库目录操作。若部署虚拟环境尚未建立，用 `python3.11 -m venv .local/deploy-venv`
创建并用其中的 pip 安装 Paramiko。用第一个终端开启维护保护，部署及公网验收完成后
按 Ctrl-D 释放。

```sh
.local/deploy-venv/bin/python scripts/ecs_use.py -- /bin/cat
```

第二个终端通过 `.local/deploy-venv/bin/python scripts/deploy_ssh.py exec '...'` 执行
云端命令，用同一工具的 `upload 本地路径 远程路径` 上传文件。先确认在线 Nginx
配置的加载路径、sshd 的生效配置、活动日志和 `18765` 端口空闲，再依次部署。

1. 用 `ssh-keygen -t ed25519 -f ~/.ssh/voice-tunnel` 生成隧道专用密钥。在 ECS 建立
   无管理权限的 `voice-tunnel` 系统用户，将 `voice-tunnel.pub` 放入该用户的
   `~/.ssh/authorized_keys`。令用户拥有该目录和文件，权限分别为 `0700` 和 `0600`。
   将 `deploy/sshd-local-tunnel.conf` 从 `sshd_config` 末尾引入。备份现有配置，
   执行 `sshd -t`，并用 `sshd -T -C user=voice-tunnel,host=localhost,addr=127.0.0.1`
   核对远程转发、监听地址和会话限制，再重新加载 SSH 服务。用新的连接核验后再关闭原连接。
2. 确认空闲守护脚本的线上版本后，将本次 `scripts/ecs_idle_guard.py` 中的端口过滤更新
   合入 `/opt/real-time-voice/scripts/ecs_idle_guard.py`。Nginx 中合入
   `deploy/ecs-activity.conf` 的业务路由匹配。更新后，隧道里的活跃请求和 WebSocket
   会阻止空闲停机，空闲的 SSH 隧道本身不会续期。
3. 加载 `deploy/nginx-local-tunnel-http.conf`。用 `nginx -t` 校验并 reload，确认 DNS
   生效后在 ECS 执行 `certbot certonly --webroot -w /var/www/voice-acme -d local.aimaoren.com`。
   证书续期可沿用仓库的 Nginx reload hook。先建立下面的隧道，再加载
   `deploy/nginx-local-tunnel-https.conf`，通过 `nginx -t` 后 reload。

## 启动和验收

本机服务需要监听 `127.0.0.1:8765`，保持账户认证开启。已运行时直接复用，未运行时用
`bash scripts/run_learning_local.sh` 启动。在 Mac 的另一终端启动隧道。若管理 SSH 端口
有调整，在命令中加 `-p 实际端口`。

```sh
ssh -NT -o StrictHostKeyChecking=yes -o IdentitiesOnly=yes \
  -o ExitOnForwardFailure=yes -o ServerAliveInterval=20 -o ServerAliveCountMax=3 \
  -i ~/.ssh/voice-tunnel -R 127.0.0.1:18765:127.0.0.1:8765 voice-tunnel@47.83.186.164
```

在 ECS 确认 `ss -lnt '( sport = :18765 )'` 只显示 `127.0.0.1:18765`，再从 ECS
请求 `http://127.0.0.1:18765/health`，确认 `ok`、`voiceEnabled` 和 `browserAuth`
对应本机的健康响应。公网验收检查 HTTPS 证书、登录、带登录 Cookie 的 WSS、麦克风输入和
语音回复。使用完毕后关闭 SSH 隧道。启用 HTTPS 入口后如需撤回，移除新增的 HTTPS/HTTP
站点配置，执行 `nginx -t` 并 reload。

对外服务需要 ECS 开机、Mac 保持唤醒、本机应用与 SSH 隧道持续运行。断网后需要重新连接
隧道。ECS 继续按现有策略在空闲约 30 分钟时停机，21:00（北京时间）的停机任务也会中断连接。
