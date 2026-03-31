import httpx
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from datetime import datetime, timezone, timedelta

# 功能1： 查分
cf_user_cmd = on_command("cf", aliases={"查cf"},
priority=10, block=True)

@cf_user_cmd.handle()
async def handle_cf_user(event: MessageEvent, args: Message = CommandArg()):
    #args 包含了你发送指令后的文字
    handle = args.extract_plain_text().strip()
    if not handle:
        await cf_user_cmd.finish("请输入要查询的cf handle, 例如/cf ExplodingKonjac")
    api_url = f"https://codeforces.com/api/user.info?handles={handle}"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(api_url, timeout=10.0)
            data = response.json()
            if data["status"] == "OK":
                info = data["result"][0]
                rating = info.get("rating", "Unrated")
                max_rating = info.get("maxRating", "Unrated")
                rank = info.get("rank", "Unrated")
                
                msg = (f"🔍 User: {handle}\n"
                f"📊 Rating: {rating}\n"
                f"🏆 Max Rating: {max_rating}\n"
                f"🏅 Rank: {rank}\n")
                #回复
                await cf_user_cmd.finish(msg)
            else:
                await cf_user_cmd.finish(f"查无此人或查询失败:{data.get('comment', '未知错误')}")
        except httpx.RequestError:
            await cf_user_cmd.finish("网络请求超时,也许是Codeforces抽风了?")

# 功能2: 查比赛
cf_contest_cmd = on_command("cfc", priority=10, block=True)

@cf_contest_cmd.handle()
async def handle_cf_contest():
    api_url = "https://codeforces.com/api/contest.list"
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(api_url, timeout=10.0)
            data = response.json()
            if data["status"] == "OK":
                contests = data["result"]
                #列表推导式 使用lambda函数排序
                upcoming = [c for c in contests if c["phase"] == "BEFORE"]
                upcoming.sort(key=lambda x:x["startTimeSeconds"])
                if not upcoming:
                    await cf_contest_cmd.finish("近期没有即将到来的cf比赛")
                msg = "📅近期即将到来的cf比赛 \n"
                # 定义东八区时区 (UTC+8)
                tz_bjt = timezone(timedelta(hours=8))
                
                for c in upcoming[:3]:
                    # 传入 tz 参数，强制转换为北京时间
                    start_time = datetime.fromtimestamp(c["startTimeSeconds"], tz=tz_bjt)
                    time_str = start_time.strftime("%Y-%m-%d %H:%M")
                    msg += f"\n🏆.{c['name']}\n⏰时间:{time_str}\n"
                await cf_contest_cmd.finish(msg)
            else:
                await cf_contest_cmd.finish("获取比赛列表失败.")
        except httpx.RequestError:
            await cf_contest_cmd.finish("网络请求超时")