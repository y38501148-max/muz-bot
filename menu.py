from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message

#priority=5 优先级越高，越容易响应
help_cmd = on_command("help", aliases={"帮助","菜单","menu"},
priority=5, block=True)

@help_cmd.handle()
async def handle_help():
    help_msg = (
       "🤖 欢迎使用 muz-bot v0.1！\n"
        "=====================\n"
        "📌 目前支持的指令列表：\n"
        "1️⃣ /cf [handle] \n"
        "   👉 查询 Codeforces 账号的分数和段位\n"
        "   📍 示例：/cf tourist\n"
        "2️⃣ /cfc \n"
        "   👉 查询近期即将举办的 CF 比赛列表\n"
        "3️⃣ /help \n"
        "   👉 呼出本帮助菜单\n"
        "=====================\n"
        "💡 提示：指令开头的斜杠 / 为必须输入哦！"
    )
    await help_cmd.finish(help_msg)
