from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message
from nonebot.params import CommandArg

#priority=5 优先级越高，越容易响应
help_cmd = on_command("help", aliases={"帮助","菜单","menu"},
priority=5, block=True)

HELP_DB = {
    "cf": (
        "📈 Codeforces 插件帮助：\n"
        "=====================\n"
        "1️⃣ /cf [handle] : 查询账号分数\n"
        "   📍 示例：/cf tourist\n"
        "2️⃣ /cfc : 查看近期比赛列表\n"
        "====================="
    ),
    "签到": (
        "💰 积分系统帮助：\n"
        "=====================\n"
        "1️⃣ /签到 : 获取每日随机奖励\n"
        "2️⃣ /积分 : 查看你的剩余资产\n"
        "3️⃣ /积分榜 : 看看谁最富有\n"
        "====================="
    ),
    "duaa": (
        "🚀 Duaa 助手\n",
        "1️⃣ /duaa 绑定 [学号] [id]\n"
        "2️⃣ /duaa 课表 [id]\n",
        "3️⃣ /duaa 签到 [id] [序号] [-su]\n"
    ),
    "by": (
        "博雅查询:\n",
        "/by 查询可用的博雅课程\n" 
    )
}

@help_cmd.handle()
async def handle_help(args: Message = CommandArg()):
    plugin_name = args.extract_plain_text().strip().lower()

    if not plugin_name:
        VERSION = 0.2
        help_msg = (
        f"🤖 欢迎使用 muz-bot -v{VERSION}\n"
            "=====================\n"
            "请输入 [/help plugin_name] 获取详细说明：\n"
        "👉 cf  : Codeforces相关\n"
        "👉 签到 : 积分与活跃度\n"
        "👉 duaa : 智慧教室相关\n"
        "👉 by : 博雅查询\n"
        "=====================\n"
        "💡 提示：指令开头的斜杠 / 为必须输入哦！"
    )
        await help_cmd.finish(help_msg)
    
    if plugin_name in HELP_DB:
        await help_cmd.finish(HELP_DB[plugin_name])
    else:
        await help_cmd.finish(f"插件{plugin_name}不存在")
