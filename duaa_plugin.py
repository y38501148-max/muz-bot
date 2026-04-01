import httpx
import json
from datetime import date
from pathlib import Path
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg

DATA_DIR = Path("data/duaa")
USER_DIR = DATA_DIR / "users"
USER_DIR.mkdir(parents=True, exist_ok=True)

LOGIN_URL = "https://iclass.buaa.edu.cn:8347/app/user/login.action"
SCHEDULE_URL = "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action"
CHECKIN_URL = "http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action"
UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230901.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36"

def get_user_file(user_id):
    return USER_DIR / f"{user_id}.json"

def load_user_data(qq_id):
    file_path = get_user_file(qq_id)
    if file_path.exists():
        return json.loads(file_path.read_text(encoding="utf-8"))
    return {}

def save_user_data(qq_id, data):
    file_path = get_user_file(qq_id)
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")

async def duaa_login(student_id):
    async with httpx.AsyncClient(verify=False) as client:
        params = {"phone": student_id, "password":"", "verificationType": 2, "userLevel": "1"}
        try:
            res = await client.get(LOGIN_URL, params=params, headers={"User-Agent":UA}, timeout=10)
            data = res.json()
            if data.get("STATUS") == "0":
                return data["result"]["id"], data["result"]["sessionId"], data["result"].get("userName","未知姓名")
        except Exception as e:
            print(f"DEBUG:iclass 登录逻辑失败,返回数据:{data}")
    return None, None, None

async def get_schedule(user_id, session_id):
    date_str = date.today().strftime("%Y%m%d")
    async with httpx.AsyncClient(verify=False) as client:
        try:
            res = await client.post(f"{SCHEDULE_URL}?id={user_id}",params={"dateStr":date_str},headers={"Sessionid":session_id, "User-Agent":UA},timeout=10)
            return res.json().get("result",[])
        except Exception:pass
    return []

duaa_cmd = on_command("duaa", priority=5, block=True)
@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("指令格式错误,请输入/help duaa")

    action = sub_cmd[0]
    qq_id = str(event.get_user_id())
    user_data = load_user_data(qq_id)

    # 绑定
    if action == "绑定":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请输入学号")
        sid = sub_cmd[1]
        uid, sess, real_name = await duaa_login(sid)
        if not uid: await duaa_cmd.finish("登录失败，请确认你的学号是否正确")
        user_data["student_id"] = sid
        user_data["real_name"] = real_name
        save_user_data(qq_id, user_data)
        await duaa_cmd.finish(f"绑定成功!欢迎使用duaa插件,{real_name}\n你的学号为:{sid}")
        # 课表
    elif action == "课表":
        if "student_id" not in user_data: await duaa_cmd.finish("请先绑定学号")
        sid = user_data["student_id"]
        uid, sess, _ = await duaa_login(sid)
        if not uid: await duaa_cmd.finish("登录失败,可能是后端网络波动?")
        schedule = await get_schedule(uid, sess)
        user_data["today_schedule"] = schedule
        save_user_data(qq_id, user_data)
        if not schedule: await duaa_cmd.finish("获取课表失败或你今日没有课表")
        msg = f"📅 {user_data.get('real_name', '同学')} 的今日课表:\n"
        for i, c in enumerate(schedule, 1):
            status = "✅已签" if c.get("signStatus") == "1" else "⏳未签"
            msg += f"\n[{i}] 📖 {c['courseName']}\n    ⏰ {c['classBeginTime'][-8:-3]} | {status}"
        await duaa_cmd.finish(msg)
    # 签到
    elif action == "签到":
        if len(sub_cmd) < 2:await duaa_cmd.finish("请指定序号")
        try:
            idx = int(sub_cmd[1]) - 1
        except:await duaa_cmd.finish("序号无效")
        schedule = user_data.get("today_schedule", [])
        if not schedule or idx < 0 or idx >= len(schedule):
            await duaa_cmd.finish("请先发送[/duaa 课表]来刷新课表序号")
        schedule_id = schedule[idx]["id"]
        course_name = schedule[idx]["courseName"]
        sid = user_data["student_id"]
        uid, sess, _ = await duaa_login(sid)
        ts = int(datetime.now().timestamp() * 1000) + 36000
        async with httpx.AsyncClient(verify=False) as client:
            res = await client.post(f"{CHECKIN_URL}?id={uid}", params={"courseSchedId":schedule_id, "timestamp": ts}, headers={"SessionId":sess, "User-Agent":UA})
            if res.json().get("STATUS") == "0":
                await duaa_cmd.finish(f"✅ 恭喜你已成功签到: {course_name}")
            else:
                await duaa_cmd.finish(f"❌ 签到失败: {res.json().get('result', '未知错误')}")
    
    