# AstrBot 后端部署

该部署只把 AstrBot 当作 `muz-bot` 的本机 LLM/Agent 后端，不给
AstrBot 配置 OneBot，避免它和 NoneBot 同时消费同一个 QQ 消息。

## 私有文件

以下文件都不提交到 Git：

- `deploy/astrbot/.env`：三个模型 API Key。
- `deploy/astrbot/providers.json`：三个 API URL、模型名和优先级。
- `data/astrbot_bridge/config.json`：AstrBot OpenAPI Key。
- AstrBot 数据目录，服务器默认 `/root/astrbot/data`。

Compose 默认固定到已验证的官方多架构镜像 digest，避免 `latest`
在无人知情时漂移；升级 AstrBot 时应重新完成测试并更新 digest。

先复制示例并填写：

```bash
cp deploy/astrbot/.env.example deploy/astrbot/.env
cp deploy/astrbot/providers.example.json deploy/astrbot/providers.json
cp data/astrbot_bridge/config.example.json data/astrbot_bridge/config.json
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

三 Provider 的调用顺序固定为：

```text
muz-primary -> muz-secondary -> muz-tertiary
```

配置器将 `request_max_retries` 设为 1，避免第一路故障时长时间阻塞；
每个 Provider 可以使用不同 `api_base`、模型和 Key。

## 提示词位置

AstrBot 首次加载插件后会自动创建空文件：

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
