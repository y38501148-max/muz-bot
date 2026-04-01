import os
import json
import random
from datetime import date
from nonebot import on_command
from pathlib import Path
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment

DATA_DIR = Path("data/signin/users")
if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

MES_FILE = Path("data/signin/messages.txt")
def load_messages():
    if not MES_FILE.exists():
        DEF_MESSAGES = [
    "今天也是非常喜欢你的一天呢~",
    "早起的鸟儿有虫吃~",
    "我爱你,我比全世界任何一个人都爱你",
    "签到成功，好运值+1!",
    "让我们永远在一起吧！"
    ]
        MES_FILE.write_text("\n".join(DEF_MESSAGES), encoding=utf-8)
        return DEF_MESSAGES
    with open(MES_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]
    return lines

MESSAGES = load_messages()

def get_user_file(user_id):
    return DATA_DIR / f"{user_id}.json"

def load_user_data(user_id):
    file_path = get_user_file(user_id)
    if file_path.exists():
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"points":0, "last_signin": ""}

def save_user_data(user_id, data):
    file_path = get_user_file(user_id)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

signin_cmd = on_command("签到", priority=5, block=True)

@signin_cmd.handle()
async def handle_signin(event: MessageEvent):
    user_id = str(event.get_user_id())
    user_data = load_user_data(user_id)

    today = str(date.today())
    if (user_data["last_signin"] == today):
        await signin_cmd.finish("你今天已经签到过了哦")
    
    added = random.randint(1, 100)
    user_data["points"] += added
    user_data["last_signin"] = today
    save_user_data(user_id, user_data)

    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
    MESSAGES = load_messages()
    msg_text = random.choice(MESSAGES)
    
    response = MessageSegment.image(avatar_url) + \
        f"\n用户{user_id}签到成功！\n" + \
        f"获得{added}积分\n" + \
        f"当前总积分：{user_data['points']}\n" + \
        f"今日评语：{msg_text}"

    await signin_cmd.finish(response)

points_cmd = on_command("积分", priority=5, block=True)

@points_cmd.handle()
async def handle_points(event: MessageEvent):
    user_id = str(event.get_user_id())
    user_data = load_user_data(user_id)

    avatar_url = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"

    response = MessageSegment.image(avatar_url) + \
        f"\n用户{user_id}的积分信息：\n" + \
        f"当前总积分：{user_data['points']}\n"
    
    await points_cmd.finish(response)

rank_cmd = on_command("积分榜", priority=5, block=True)
@rank_cmd.handle()
async def handle_rank(event: MessageEvent):
    all_users = []
    for file in DATA_DIR.glob("*.json"):
        with open(file, "r", encoding=utf-8) as f:
            data = json.load(f)
            all_users.append((file.stem, data["points"]))
    
    all_users.sort(key=lambda x: x[1], reverse=True)
    if not all_users:
        await rank_cmd.finish("还没有人签到过哦")
    msg = "🏆 积分排行榜 🏆\n"
    for i, (user_id, points) in enumerate(all_users[:10], 1):
        msg += f"{i}. {user_id} ———— 积分: {points}\n"
    
    await rank_cmd.finish(msg.strip())