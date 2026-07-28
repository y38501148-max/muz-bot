# muz-bot AstrBot Gateway

该插件在 AstrBot 请求模型之前执行以下操作：

1. 从 `data/plugin_data/astrbot_plugin_muz_gateway/system_prompt.txt`
   热加载系统提示词。首次运行时从随插件发布的青雀模板初始化；
   修改后无需重启。已有运行时文件不会被自动覆盖，部署升级时应
   显式复制新版模板。
2. 使用 AstrBot 同源的保守 token 估算规则检查上下文。超过
   50,000 tokens 时在本地删除最旧的完整对话轮次，并压到约
   45,000 tokens；不会为 compact 额外调用任何模型 API。
3. 解开 muz-bot 的可信请求封装，把成员记忆作为 `_no_save` 的临时
   不可信 user context，避免进入 system prompt 或单群共享历史；
   同时接入经过校验的 QQ 图片和视频关键帧。
4. 只预取当前消息中的链接，或在明确搜索意图下使用脱敏后的当前问题
   搜索；资料以 `_no_save` 临时上下文提供。模型本身不保留任何工具，
   Shell、文件、Computer Use、Cron、MCP 和子代理均被移除。

Provider 的优先级和降级由 AstrBot 原生
`fallback_chat_models` 执行，配置脚本位于
`deploy/astrbot/configure.py`。
