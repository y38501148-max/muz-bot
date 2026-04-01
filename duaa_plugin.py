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

# BUAA iclass API 地址
LOGIN_URL = "https://iclass.buaa.edu.cn:8347/app/user/login.action"
SCHEDULE_URL = "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action"
CHECKIN_URL = "http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action"

UA = "Mozilla/5.0 (Linux; Android 13; Pixel 7 Build/TQ3A.230901.001; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36"

# 修复后的代理配置：统一指向本地 VPN 容器
# 如果你的 httpx 版本较低，建议直接指定一个全局 proxy
PROXY_URL = "http://localhost:1080"

# 2. 单个用户文件操作
def get_user_file(qq_id):
    return USER_DIR / f"{qq_id}.json"

def load_user_data(qq_id):
    file_path = get_user_file(qq_id)
    if file_path.exists():
        return json.loads(file_path.read_text(encoding="utf-8"))
    return {}

def save_user_data(qq_id, data):
    file_path = get_user_file(qq_id)
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")

# 3. 核心 API
async def duaa_login(student_id):
    # 修改：使用 proxy 代替 proxies 以保证兼容性
    async with httpx.AsyncClient(verify=False, proxy=PROXY_URL) as client:
        params = {"phone": student_id, "password": "", "verificationType": "2", "userLevel": "1"}
        try:
            res = await client.get(LOGIN_URL, params=params, headers={"User-Agent": UA}, timeout=10)
            data = res.json()
            if data.get("STATUS") == "0":
                results = data.get("result", {})
                return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名")
            else:
                print(f"DEBUG: iclass 登录业务失败，返回: {data}")
        except Exception as e:
            print(f"DEBUG: iclass 登录网络请求异常: {str(e)}")
    return None, None, None

async def get_schedule(user_id, session_id):
    date_str = datetime.now().strftime("%Y%m%d")
    # 修改：使用 proxy 代替 proxies
    async with httpx.AsyncClient(verify=False, proxy=PROXY_URL) as client:
        try:
            res = await client.get(
                SCHEDULE_URL, 
                params={"id": user_id, "dateStr": date_str},
                headers={"Sessionid": session_id, "User-Agent": UA}, 
                timeout=10
            )
            data = res.json()
            if data.get("STATUS") == "0":
                return data.get("result", [])
            else:
                print(f"DEBUG: 课表查询业务失败，返回：{data}")
        except Exception as e:
            print(f"DEBUG: 课表查询网络异常：{str(e)}")
    return []

# 4. 指令处理器
duaa_cmd = on_command("duaa", priority=5, block=True)

@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("🚀 Duaa 助手：\n/duaa 绑定 [学号]\n/duaa 课表\n/duaa 签到 [序号] [-su]")
    
    action = sub_cmd[0]
    qq_id = str(event.get_user_id())
    user_data = load_user_data(qq_id)

    if action == "绑定":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请输入学号")
        sid = sub_cmd[1]
        uid, sess, real_name = await duaa_login(sid)
        if not uid or not sess: 
            await duaa_cmd.finish("❌ 绑定失败，可能服务器连不上校内网或学号错误")
        
        user_data["student_id"] = sid
        user_data["real_name"] = real_name
        save_user_data(qq_id, user_data)
        await duaa_cmd.finish(f"✅ 绑定成功！\n姓名：{real_name}\n学号：{sid}")

    elif action == "课表":
        if "student_id" not in user_data: await duaa_cmd.finish("请先绑定学号")
        sid = user_data["student_id"]
        uid, sess, _ = await duaa_login(sid)
        if not uid or not sess: 
            await duaa_cmd.finish("❌ 登录失效，服务器无法访问 iclass")
        
        sched = await get_schedule(uid, sess)
        user_data["today_schedule"] = sched
        save_user_data(qq_id, user_data)

        if not sched: await duaa_cmd.finish("📅 今日你目前暂无课程")
        
        msg = f"📅 {user_data.get('real_name', '同学')} 的今日课表:\n"
        for i, c in enumerate(sched, 1):
            status = "✅已签" if c.get("signStatus") == "1" else "⏳未签"
            msg += f"\n[{i}] 📖 {c['courseName']}\n    ⏰ {c['classBeginTime'][-8:-3]} | {status}"
        await duaa_cmd.finish(msg)

    elif action == "签到":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请指定序号")
        try:
            idx = int(sub_cmd[1]) - 1
        except: await duaa_cmd.finish("序号无效")
        
        force_mode = "-su" in sub_cmd
        sched = user_data.get("today_schedule", [])
        if not sched or idx < 0 or idx >= len(sched):
            await duaa_cmd.finish("请先发送 [/duaa 课表] 刷新最新序号")
        
        target_course = sched[idx]
        
        if target_course.get("signStatus") == "1":
            await duaa_cmd.finish("你已经签到过了哦")

        if not force_mode:
            valid_start = valid_end = None
            try:
                now = datetime.now()
                fmt = "%Y-%m-%d %H:%M:%S"
                begin_t = datetime.strptime(target_course["classBeginTime"], fmt)
                end_t = datetime.strptime(target_course["classEndTime"], fmt)
                valid_start = begin_t - timedelta(minutes=10)
                valid_end = end_t - timedelta(minutes=1)
            except Exception as e:
                print(f"DEBUG: 时间解析故障: {e}")
            
            if valid_start and now < valid_start:
                await duaa_cmd.finish(
                    f"⏰ 还没到时候呢！\n《{target_course['courseName']}》的有效窗口为：\n{valid_start.strftime('%H:%M')} ~ {valid_end.strftime('%H:%M')}\n(如需强制开启请加参数 -su)"
                )
            if valid_end and now > valid_end:
                await duaa_cmd.finish(f"🚫 太晚啦！签到窗口已关闭。")

        sched_id = target_course["id"]
        course_name = target_course["courseName"]
        sid = user_data["student_id"]
        uid, sess, _ = await duaa_login(sid)
        if not uid or not sess:
            await duaa_cmd.finish("❌ 签到失败：无法建立校内连接")
        
        ts = int(datetime.now().timestamp() * 1000) + 36000
        # 修改：使用 proxy 代替 proxies
        async with httpx.AsyncClient(verify=False, proxy=PROXY_URL) as client:
            headers = {
                "Sessionid": sess,
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded"
            }
            res = await client.post(
                CHECKIN_URL,
                params={"id": uid, "courseSchedId": sched_id, "timestamp": ts},
                headers=headers
            )
            res_data = res.json()
            if res_data.get("STATUS") == "0":
                await duaa_cmd.finish(f"🎯 《{course_name}》 签到成功！" + (" (强制模式)" if force_mode else ""))
            else:
                await duaa_cmd.finish(f"❌ 签到失败：{res_data.get('ERRMSG', '未知原因')}")

    else:
        await duaa_cmd.finish("未知的子指令")
