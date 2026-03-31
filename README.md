### muz-bot

这是一个基于nonebot2的个人机器人
目前版本v0.1

使用方式:
首先，运行bot.py
第一步：跑起 NapCatQQ 容器
由于你的 bot.py 占用了一个终端窗口，请在 Linux 的终端里 新建一个标签页（或新窗口），然后把这串命令复制进去敲回车：
```bash
sudo docker run -d --network host --name napcat -e WEBUI_TOKEN=123456 mlikiowa/napcat-docker:latest
```
> 这行指令会去拉取一个最新版的免安装无头 QQ，对外暴露 6099 端口作为网页控制台，并且设置登录密码为 123456

第二步：去网页后台上号！
当指令执行完并打印出一长串哈希值后，说明它跑起来了。 请打开你的浏览器，访问： 👉 http://服务器公网ip:6099/webui

你会进入 NapCatQQ 的高颜值控制台：
1. 密码：输入刚才设定的 123456。
2. 扫码登录：在页面正中央应该会刷出一个 QQ 登录二维码，请用你准备当作机器人的小号在手机上扫码登录。

第三步：打通任督二脉（连接 Python）
当你扫码登录成功后，在网页的左侧侧边栏，找到 【网络配置】(Network)。

往下滚动，找到 反向 WebSocket (Reverse WebSocket) 这一栏。
点击新增，在 URL 地址框里填入这个极其重要的地址： ws://127.0.0.1:8080/onebot/v11/ws
保存并启用！

更新介绍:
v0.1
(2026-03-31)
加入Codeforces查分以及近期比赛查询功能


