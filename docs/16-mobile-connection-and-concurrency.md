# 移动端连接、两路并发与异常断链（2026-10-23）

## 范围与保持不变的约束

默认支持两路真实语音通话，第三路明确忙线，不挤掉任何已有会话。同一试用账户可同时使用两个名额；容量按服务实例而非账户计数。端点维持 VAD/NLS 550ms，预生成、短首段 TTS、媒体栅栏、播放进度、打断策略和 Bot 内容接口不变。本地不启动语音模型或服务。

三路候选已实际验证：两路完成三轮真实 ASR/LLM/TTS，第三路收到 NLS 40000005，候选未通过。阿里云试用版最多两路，且同一账户下同服务的 AppKey 共享并发额度；不通过更换项目绕过限额。用户已确认本轮仅需两路，不代开收费服务。未来提高 `MAX_CONCURRENT_SESSIONS` 前必须先核对供应商配额并重新做并发与打断验证。

## 浏览器鉴权

1. 页面和文本 API 保留 Nginx HTTP Basic 试用访问保护。
2. 浏览器拨通前通过同源 HTTPS `POST /api/browser-session` 获取签名会话 Cookie。Nginx 必须覆盖 `X-Authenticated-User` 为实际认证用户，应用仅信任回环代理；浏览器伪造此头不能获得登录。
3. Cookie 为 HttpOnly、Secure、SameSite=Strict、Path=/，有效期 2 小时。签名密钥保存在 `/var/lib/real-time-voice/browser-auth.key`，至少 32 字节、0600，不进代码包、不传浏览器、不写日志；重启保持有效，缺失/不安全配置启动失败。
4. `/ws` 不再发 Basic challenge，但应用必须验证签名 Cookie 和 Origin。无效、过期、伪造或跨域请求返回 401，不能升级。不使用 URL 凭证或 query token。不能仅把 Nginx `auth_basic` 关闭就视为修复。
5. HTTP bootstrap 对登录失效、服务异常和超时分别提示；WebSocket 等待 15 秒，资源确认与会话建立各 8 秒。无自动重复建会话，重试由用户发起。旧 socket 的迟到回调不能覆盖新尝试或已呈现的错误。

bootstrap 返回实时 `resources`，满额直接提示“当前通话名额已满（2/2）”。它仅是预检，真正的 `prepare` 仍由原子资源门禁裁决，处理同时拨入的竞争；忙线返回 `NO_RESOURCE` 和资源数量。

## 断链与资源释放

- 页面 `pagehide` 尽力发送 `/api/browser-disconnect` beacon，并停止采集、停止播放、关闭 WebSocket；不绑定 `visibilitychange`，不因普通切后台或安静听课主动挂断。
- 离开通知除 Cookie/Origin 外，还需该连接私有 `disconnectKey`；它只随成功 `create_session_result` 下发，不加入导出录音元数据、事件时间线或日志。其他会话不能凭 sessionId 踢人。重复通知及已关闭会话幂等。
- 离开通知触发独立清理任务，HTTP 页面退出不会取消清理。清理以连接级锁串行，取消回复/预生成、关闭识别、释放名额；WS 断开和 beacon 同时到达也只完成一次。
- 未点挂断直接关页、断网或进程被杀时，WS close / heartbeat 负责兜底。心跳间隔 10 秒；未收到 pong 后关闭，再进行有上限的清理。检测时长受事件循环/网络影响，不承诺瞬间释放。
- `prepare` 后没有建立会话的预约有默认 30 秒租期，防止只占号不建会话；租期不作用于已建立通话。正常静默不是断链，不自动告别挂断。

## 可观测性

新增 HTTP `/api/connection-events` 接收会话建立前失败，按 attemptId 关联；只接受错误码、受限标识和有限时长，不接收 URL、任意错误文本、Cookie、登录凭证或对话内容，每 Cookie 限流。后端保留鉴权拒绝、prepare、忙线、阶段完成、连接时长、页面离开、租期释放、清理失败日志。

默认关闭每音频帧的 span start/end 与高频播放进度日志，关闭 FunASR 进度条；需要时可启用 `LOG_AUDIO_SPANS=1`。浏览器媒体/打断时间线不变。服务日志突发额度调整为 2000/30秒，避免关键连接记录被逐帧输出淹没。NLS 40000005 单独映射为 `NLS_CONCURRENCY_LIMIT`，不误报为鉴权失败。

## 发布门禁

必须验证：无 Cookie 拒绝、伪造代理头拒绝、HTTP 登录后 Cookie-only 升级、Cookie 过期/篡改、跨域拒绝、前端超时与旧回调、两路真实三轮聊天、第三路忙线、释放后重拨、打断恢复与幂等、无挂断关闭连接、beacon 幂等、缺失 pong 回收及静默但心跳正常不误踢。

真实 Safari 使用系统 SafariDriver，不以 Chrome UA 或 WebKit 模拟替代；需要用户开启 Safari“允许远程自动化”。发布前同时检查生产空闲、备份代码/配置/SQLite、部署前后文件哈希及健康状态。Nginx 与应用鉴权须一并发布并支持回滚，不开放无保护的 WS 过渡配置。

本轮验证结果：

- 云端两路同时各三轮真实 ASR/LLM/Piper TTS 通过；两路使用同一试用账户，第三路明确 `NO_RESOURCE (2/2)`。结束后名额归零并可再次拨入；两次真实语音打断无旧音频泄漏，Bot 恢复与幂等通过。修复后重复运行该组验收仍通过。
- 代理安全检查通过：伪造认证头、缺失/伪造 Cookie、跨域 WS 拒绝；合法 Cookie-only WS 升级及会话前 HTTP 诊断上报通过。Cookie Secure 属性由单元测试校验，隔离隧道的 HTTP 代理仅在候选进程显式关闭 Secure 以便本机浏览器验证，生产发布配置必须为 Secure=1。
- 强制断开传输但不发挂断命令时，第一次实测约 362ms 回收；无 pong 连接约 15.2s 回收，旁边心跳正常但不说话的通话保持在线。页面离开私钥校验、重复 beacon、未建会话租期回收及清理并发幂等均有回归测试。
- 独立 Chrome 前端实测经过 HTTP bootstrap 后完成开场白、真实语音问答、录音导出，无空 ASR、无丢帧、无抢答；这是实际浏览器 PCM/音频图验证，不是物理扬声器回录。
- SafariDriver 目前明确拒绝创建会话，原因是系统 Safari 的“允许远程自动化”未启用；尚不能标记 Safari 通过。发布门禁保留，**本轮尚未部署，生产仍为旧版本、容量 1**，待用户启用后完成 Safari 验证再发布容量 2。
- 后端连接/鉴权/断链/NLS/部署及双工回归通过；5 组前端连接流程、断链、播放及时间轴测试通过。中途全库运行曾有 4 项资料溯源失败：并行资料整理将原目录移到 `英语/人教版/` 且源文件内容改变，当时构建器仍引用旧路径；未在本任务中擅自改动这些资料。并行更新完成后的最终完整本地回归为 **329 passed、3 个真实模型测试 deselected**；这些新增教材改动依然未纳入本次云端连接候选，不据此宣称它们完成了云端语音验收。

证据在 `.local/connection-release-20261023/`，包括 `three-call-rejected.json`、`multicall-final.json`、`auth-final.json`、`disconnect-final.json`、`chrome-recording-r3.json` 与 `safari-connection.json`。最终断链复测为无挂断传输中断约 344ms 回收，缺失 pong 约 15.3s 回收。隔离语音实例、隔离 Nginx、本地隧道及 SafariDriver 已停止；远端三个临时凭证副本已移除，本地原始凭证未动，测试代码/录音/数据库保留。生产源码 58 文件哈希与测试前一致，健康、空闲，仍是 `rtv-20261022-short-stream-r5`、容量 1。

最终候选包含 60 个 backend/web 文件，仅变更连接鉴权/日志/限额提示及前端连接流程；`duplex.py`、回复流水线、打断控制器、TTS 与预生成模块未变化。最后一次本地检查发现并行新增的 `chapter_practice` 接口、教材入口、`web/text.html` 与四个年级资料包已与候选不同；已保留这些用户改动，不覆盖，也不自动混入本次连接发布。待 Safari 验证后，须从远端已验证快照构建并使用生产基线哈希门禁，不能直接重新打包整个正在改动的本地工作区。

远端已验证快照：`/var/tmp/rtv-connection-lab.0dsvoky7/verified-serving.tar.gz`，SHA256 `29ce8a4cff47e95e8e637dd541f558f3d55c9e6d481710fae55ab39224d13d80`。候选目录、独立数据库及 Nginx 测试配置保留；再次验证时需重新启动临时进程，并重新传输临时测试凭证，不能把停止/清理状态误认为服务仍在监听。

参考：[阿里云并发与 QPS](https://help.aliyun.com/zh/isi/getting-started/faq-about-concurrency-and-monitoring)。
