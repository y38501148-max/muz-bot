import httpx
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

# 1. 路径配置
BASE_DATA_DIR = Path("data/duaa")
USER_DIR = BASE_DATA_DIR / "users"
USER_DIR.mkdir(parents=True, exist_ok=True)

LOGIN_URL = "https://iclass.buaa.edu.cn:8347/app/user/login.action"
SCHEDULE_URL = "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action"
CHECKIN_URL = "http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action"
UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230901.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36"

# 2. 数据处理与迁移
def load_user_data(qq_id):
    file_path = USER_DIR / f"{qq_id}.json"
    if not file_path.exists(): return {"accounts": {}}
    
    data = json.loads(file_path.read_text(encoding="utf-8"))
    
    # 迁移逻辑：如果发现是旧格式，自动转为 ID 为 "本人" 的多账号格式
    if "student_id" in data and "accounts" not in data:
        old_sid = data.pop("student_id")
        old_name = data.pop("real_name", "本人")
        data["accounts"] = {old_name: {"student_id": old_sid, "real_name": old_name}}
        save_user_data(qq_id, data)
    
    return data

def save_user_data(qq_id, data):
    file_path = USER_DIR / f"{qq_id}.json"
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")

# 3. 核心 API
async def duaa_login(student_id):
    async with httpx.AsyncClient(verify=False) as client:
        params = {"phone": student_id, "password": "", "verificationType": "2", "userLevel": "1"}
        try:
            res = await client.get(LOGIN_URL, params=params, headers={"User-Agent": UA}, timeout=10)
            json_data = res.json()
            if json_data.get("STATUS") == "0":
                results = json_data.get("result", {})
                return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名")
        except Exception as e:
            logger.error(f"Duaa 登录失败: {e}")
    return None, None, None

async def get_schedule(user_id, session_id):
    date_str = datetime.now().strftime("%Y%m%d")
    async with httpx.AsyncClient(verify=False) as client:
        try:
            res = await client.get(SCHEDULE_URL, params={"id": user_id, "dateStr": date_str},
                                 headers={"Sessionid": session_id, "User-Agent": UA}, timeout=10)
            json_data = res.json()
            return json_data.get("result", []) if json_data.get("STATUS") == "0" else []
        except Exception as e:
            logger.error(f"Duaa 获取课表失败: {e}")
            return []

# 4. 指令处理器
duaa_cmd = on_command("duaa", priority=5, block=True)

@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("🚀 Duaa 助手：\n/duaa 绑定 [学号] [ID]\n/duaa 课表 [ID]\n/duaa 签到 [ID] [序号] [-su]")
    
    action = sub_cmd[0]
    qq_id = str(event.get_user_id())
    data = load_user_data(qq_id)
    accounts = data.get("accounts", {})

    if action == "绑定":
        if len(sub_cmd) < 3: await duaa_cmd.finish("请输入：/duaa 绑定 [学号] [自定义ID]")
        sid, alias = sub_cmd[1], sub_cmd[2]
        uid, sess, real_name = await duaa_login(sid)
        if not uid or not sess: await duaa_cmd.finish("❌ 登录失败，请检查学号或网络")
        
        accounts[alias] = {"student_id": sid, "real_name": real_name}
        data["accounts"] = accounts
        save_user_data(qq_id, data)
        await duaa_cmd.finish(f"✅ 绑定成功！\nID：{alias}\n姓名：{real_name}\n学号：{sid}")

    elif action == "课表":
        # 确定使用哪个账号
        alias = sub_cmd[1] if len(sub_cmd) > 1 else (list(accounts.keys())[0] if len(accounts) == 1 else None)
        if not alias or alias not in accounts:
            await duaa_cmd.finish(f"❓ 请指定预览哪个账号的课表。\n当前可选 ID：{', '.join(accounts.keys())}")
        
        acc = accounts[alias]
        uid, sess, _ = await duaa_login(acc['student_id'])
        if not uid or not sess: await duaa_cmd.finish("❌ 登录失效")
        
        sched = await get_schedule(uid, sess)
        acc["today_schedule"] = sched # 更新该账号的缓存课表
        save_user_data(qq_id, data)

        if not sched: await duaa_cmd.finish(f"📅 {acc['real_name']} 今日无课")
        
        msg = f"📅 {acc['real_name']} ({alias}) 的今日课表:\n"
        for i, c in enumerate(sched, 1):
            status = "✅已签" if c.get("signStatus") == "1" else "⏳未签"
            room = c.get("roomName") or c.get("classroomName") or c.get("placeName") or "未知地点"
            msg += f"\n[{i}] 📖 {c['courseName']}\n    📍 {room}\n    ⏰ {c['classBeginTime'][-8:-3]} | {status}"
        await duaa_cmd.finish(msg)

    elif action == "解绑":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请输入要解绑的 ID")
        alias = sub_cmd[1]
        if alias not in accounts: await duaa_cmd.finish(f"❌ 找不到 ID 为 {alias} 的账号")
        
        info = accounts.pop(alias)
        data["accounts"] = accounts
        save_user_data(qq_id, data)
        await duaa_cmd.finish(f"🗑️ 已成功解绑账号：{info['real_name']} ({alias})")

    elif action == "签到":
        # 解析参数：/duaa 签到 [ID] [序号] [-su]
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号] [-su]")
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        
        if alias not in accounts: await duaa_cmd.finish(f"❌ 找不到 ID 为 {alias} 的账号")
        acc = accounts[alias]
        
        try:
            idx = int(idx_str) - 1
        except: await duaa_cmd.finish("序号无效")
        
        force_mode = "-su" in sub_cmd
        sched = acc.get("today_schedule", [])
        if not sched or idx < 0 or idx >= len(sched):
            await duaa_cmd.finish(f"请先发送 [/duaa 课表 {alias}] 刷新序号")
        
        target = sched[idx]
        if target.get("signStatus") == "1": await duaa_cmd.finish("已经签过啦")

        # 时间检查
        if not force_mode:
            now = datetime.now()
            fmt = "%Y-%m-%d %H:%M:%S"
            begin_t = datetime.strptime(target["classBeginTime"], fmt)
            end_t = datetime.strptime(target["classEndTime"], fmt)
            if now < begin_t - timedelta(minutes=10): await duaa_cmd.finish("⏰ 还没到时候")
            if now > end_t - timedelta(minutes=1): await duaa_cmd.finish("🚫 窗口已关闭")

        # 执行请求
        uid, sess, _ = await duaa_login(acc['student_id'])
        ts = int(datetime.now().timestamp() * 1000) + 36000
        async with httpx.AsyncClient(verify=False) as client:
            res = await client.post(CHECKIN_URL, params={"id": uid, "courseSchedId": target["id"], "timestamp": ts},
                                 headers={"Sessionid": sess, "User-Agent": UA})
            if res.json().get("STATUS") == "0":
                await duaa_cmd.finish(f"🎯 {acc['real_name']} - 《{target['courseName']}》签到成功！")
            else:
                await duaa_cmd.finish(f"❌ 失败：{res.json().get('ERRMSG', '未知')}")
