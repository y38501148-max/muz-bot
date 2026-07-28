# AstrBot 后端部署

该部署只把 AstrBot 当作 `muz-bot` 的本机 LLM/Agent 后端，不给
AstrBot 配置 OneBot，避免它和 NoneBot 同时消费同一个 QQ 消息。

## 私有文件

以下文件都不提交到 Git：

- `deploy/astrbot/.env`：三个模型 API Key。
- `deploy/astrbot/providers.json`：三个 API URL、模型名和优先级。
- `data/astrbot_bridge/config.json`：AstrBot OpenAPI Key。
- `data/astrbot_bridge/sessions.json` 与 `sessions.key`：单群/私聊会话映射，
  会话身份使用部署专属 HMAC 索引，文件及密钥均为 `0600`。
- `data/astrbot_bridge/member_memories.sqlite3`：按群和成员隔离的人格记忆，
  使用部署专属 HMAC 成员键和增量 SQLite 存储，数据库及密钥均为
  `0600`。
- AstrBot 数据目录，服务器默认 `/root/astrbot/data`。

Compose 默认固定到已验证的官方多架构镜像 digest，避免 `latest`
在无人知情时漂移；升级 AstrBot 时应重新完成测试并更新 digest。

先复制示例并填写：

```bash
cp deploy/astrbot/.env.example deploy/astrbot/.env
cp deploy/astrbot/providers.example.json deploy/astrbot/providers.json
cp data/astrbot_bridge/config.example.json data/astrbot_bridge/config.json
chmod 600 deploy/astrbot/.env deploy/astrbot/providers.json \
  data/astrbot_bridge/config.json
```

## 启动与配置

```bash
docker compose -f deploy/astrbot/compose.yml up -d
python3 deploy/astrbot/configure.py \
  --config /root/astrbot/data/cmd_config.json \
  --providers deploy/astrbot/providers.json
docker compose -f deploy/astrbot/compose.yml restart astrbot
```

在 AstrBot WebUI 的设置中创建仅含 `chat` scope 的 API Key，填入
`data/astrbot_bridge/config.json`，并将 `ENABLED` 改为 `true`。
部署脚本还会关闭 AstrBot 原生 Web Search、文件提取、Computer Use、
Cron 工具和 Subagent；Gateway 在最终 LLM hook 中把模型可调用工具集
强制清空，只把当前消息对应的受控网络资料和媒体作为本轮临时输入。

三 Provider 的调用顺序固定为：

```text
muz-primary -> muz-secondary -> muz-tertiary
```

配置器将 `request_max_retries` 设为 1，避免第一路故障时长时间阻塞；
每个 Provider 可以使用不同 `api_base`、模型、Key 和可选 `proxy`。
`proxy` 支持 HTTP(S) 与 SOCKS5 地址；留空时保持直连。`api_base`
既可填写顶级 URL，也可填写 `/v1`、`/v1/chat/completions` 或
`/v1/responses` 完整地址；配置器会统一规范为 OpenAI SDK 所需的
`/v1` 基础地址。当前三个已验证 Provider 均支持 Chat Completions，
因此 AstrBot 会自动补全 `/chat/completions`；`/responses` 在这里
作为可识别的输入端点，用于反推出同一个基础 URL，并不切换
AstrBot 的 Provider 类型。

## 群成员人格记忆与消息队列

群聊仍使用“单群单对话”，同时为每个“群＋成员”维护独立的本地人格
记忆区。机器人采集群内全部普通文字消息，不论该消息是否触发 AI；
命令、密码、验证码、手机号、邮箱等敏感格式和明显提示注入内容不写入。
每位成员最多保留最近 24 条并自动在 30 天后过期，每次最多向模型临时
提供此前 10 条、总计 1,800 字符的引用样本。样本以 `_no_save` 的临时、
不可信 user context 提供，不进入高优先级 system prompt，也不写入
单群共享对话历史；模型不会收到群号或 QQ 号。群成员发送“忘掉我”
可立即清除其在当前群的专属记忆。

普通负载下，未触发 AI 的群消息也会先进入成员记忆。采集等待最多保留
100 条，单条争用超过 50 ms 会安全丢弃，避免消息洪泛形成无界后台任务；
空文本、停用状态和配置错误状态不会采集。

同一群或私聊的模型请求使用 FIFO 队列，后到消息等待前一条完成，不再
因为已有请求正在处理而直接拒绝。不同对话最多并行 3 条；为防止群消息
洪泛形成无限付费积压，单对话最多保留 20 条、全局最多保留 60 条待处理
请求。排队超过 120 秒会过期；默认调用预算为每成员 20 次/小时、
每群 120 次/小时、全局 300 次/小时，避免持续补队造成无限付费调用。
Agent 单次请求最多执行 1 步，不向模型开放可调用工具。

## 联网搜索与多模态

Gateway 在调用模型之前处理联网资料，不给模型开放可调用工具。它只会
读取当前消息中实际出现且不含敏感查询参数的最多 2 个链接；只有用户
以“搜索、查一下、联网查询”等显式表达提出联网意图时，才会用经过脱敏和截断的
当前问题搜索。结果作为 `_no_save` 的本轮不可信 user context，不进入
单群共享历史，也不会进入 AstrBot 的工具结果 INFO 日志。

联网请求通过 `ASTRBOT_WEB_PROXY` 使用既有代理，限制响应大小、跳转
次数和端口，并拒绝本机、内网和链路本地地址。Gateway 先解析并固定
已验证的公网 IP，再让 curl 通过代理按该 IP 建立连接，同时保留原域名
完成 TLS 证书校验；每次跳转都会重新验证，避免 DNS 重绑定变成内网
探测。Shell、任意文件访问、Computer Use、Cron、MCP 与子代理均禁用。

QQ 图片和视频只接受受信任的腾讯/QQ CDN 主机，并经过公网地址校验。
图片在解码前限制为单帧、最多约 1,677 万像素，并全局串行压缩。
视频最多接收一条、25 MiB，并用 ffmpeg 抽取最多 4 张关键帧，因此属于
关键帧分析而非完整音视频理解。视频还限制为 120 秒、最高 4K，媒体
子进程限制 512 MiB/单线程且同一时间只处理一条；AstrBot 容器限制为
2 GiB、2 CPU、256 PIDs。链接正文最多读取 2 MiB/12,000 字符。

## 提示词位置

AstrBot 首次加载插件后会用青雀人格模板创建：

```text
/root/astrbot/data/plugin_data/astrbot_plugin_muz_gateway/system_prompt.txt
```

后续通过 SSH 直接覆盖此文件即可，下一次请求会热加载，不需要重启。

## 50k 上下文说明

AstrBot 原生本地压缩器固定在 Provider 窗口的 82% 触发。因此配置器
把技术窗口设为 `60,975`，使原生保护线约等于 `49,999.5` tokens；
AstrBot 插件还会在请求进入 runner 前独立执行一次 50,000 tokens
硬上限检查，超过后本地删除最旧完整轮次并压到约 45,000。整个
compact 过程不会调用任何模型 API。
