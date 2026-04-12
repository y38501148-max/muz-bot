import httpx
import json
import re
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from nonebot import on_command, logger, require, get_bots
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

# 引入定时任务插件
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

# 1. 路径与基础配置
BASE_DATA_DIR = Path("data/duaa")
USER_DIR = BASE_DATA_DIR / "users"
CONFIG_FILE = BASE_DATA_DIR / "config.json"
USER_DIR.mkdir(parents=True, exist_ok=True)

# 强制北京时区，防止境外服务器时区问题导致自动签到失效
TZ_BEIJING = timezone(timedelta(hours=8))

# 伪装 UA 与 VPN 配置
UA = "Mozilla/5.0 (Linux; Android 13; M2012K11AC Build/TKQ1.220829.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 wxwork/4.1.22 MicroMessenger/7.0.1 NetType/WIFI Language/zh ColorScheme/Light"
VPN_SERVICE_ID = "77726476706e69737468656265737421f9f44d9d342326526b0988e29d51367ba018"

def get_network_urls(use_vpn):
    """
    严格按照 UBAA 源码重构的 URL 路由
    - 登录和课表: HTTPS :8347
    - 时间戳和签到: HTTP :8081
    """
    if use_vpn:
        # WebVPN 映射规则
        base_8347 = f"https://d.buaa.edu.cn/https-8347/{VPN_SERVICE_ID}"
        base_8081 = f"https://d.buaa.edu.cn/http-8081/{VPN_SERVICE_ID}" 
        return {
            "login": f"{base_8347}/app/user/login.action",
            "schedule": f"{base_8347}/app/course/get_stu_course_sched.action",
            "timestamp": f"{base_8081}/app/common/get_timestamp.action",
            "sign": f"{base_8081}/app/course/stu_scan_sign.action"
        }
    else:
        # 校园网直连
        return {
            "login": "https://iclass.buaa.edu.cn:8347/app/user/login.action",
            "schedule": "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action",
            "timestamp": "http://iclass.buaa.edu.cn:8081/app/common/get_timestamp.action",
            "sign": "http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action"
        }

# 2. 数据处理与配置管理
def load_user_data(qq_id):
    file_path = USER_DIR / f"{qq_id}.json"
    if not file_path.exists(): return {"accounts": {}}
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if "student_id" in data and "accounts" not in data:
        old_sid = data.pop("student_id")
        old_name = data.pop("real_name", "本人")
        data["accounts"] = {old_name: {"student_id": old_sid, "real_name": old_name}}
        save_user_data(qq_id, data)
    return data

def save_user_data(qq_id, data):
    file_path = USER_DIR / f"{qq_id}.json"
    file_path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")

def get_shared_vpn():
    if not CONFIG_FILE.exists(): return None, None
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return cfg.get("vpn_username"), cfg.get("vpn_password")
    except: return None, None

def set_shared_vpn(username, password):
    CONFIG_FILE.write_text(json.dumps({"vpn_username": username, "vpn_password": password}), encoding="utf-8")

# 3. 核心 API 客户端
async def sso_login(client: httpx.AsyncClient, username, password):
    vpn_entry_url = "https://d.buaa.edu.cn/login"
    try:
        res = await client.get(vpn_entry_url, timeout=10)
        res.raise_for_status()
        real_sso_url = str(res.url)
        
        execution_match = re.search(r'name="execution"\s+value="([^"]+)"', res.text)
        if not execution_match:
            raise ValueError("无法在 SSO 页面解析 execution 参数。")
        
        post_data = {
            "username": username,
            "password": password,
            "submit": "登录",
            "type": "username_password",
            "execution": execution_match.group(1),
            "_eventId": "submit",
        }

        login_res = await client.post(real_sso_url, data=post_data, headers={"Referer": real_sso_url}, timeout=15, follow_redirects=True)
    except Exception as e:
        raise Exception(f"SSO 登录过程网络异常: {e}")

    final_url = str(login_res.url)
    if login_res.status_code == 401 or "密码错误" in login_res.text:
        raise ValueError("SSO 认证失败：学号或密码错误。")
    
    vpn_cookies = [c.name for k, c in client.cookies.jar._cookies.items() for _, c in c.items() for _, c in c.items()]
    if "d.buaa.edu.cn" in final_url or any("wengine" in name.lower() for name in vpn_cookies):
        return True
    
    raise ValueError(f"SSO 穿透失败，最终停留地址: {final_url}")

async def perform_duaa_login(target_student_id, personal_password=None):
    vpn_user, vpn_pass = (target_student_id, personal_password) if personal_password else get_shared_vpn()
    use_vpn = bool(vpn_pass)
    urls = get_network_urls(use_vpn)
    
    async with httpx.AsyncClient(verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
        if use_vpn:
            await sso_login(client, vpn_user, vpn_pass)
            
        try:
            # 严格对应 UBAA 源码的登录参数
            login_params = {
                "phone": target_student_id,
                "password": "",
                "userLevel": "1",
                "verificationType": "2",
                "verificationUrl": ""
            }
            res = await client.get(urls["login"], params=login_params, timeout=15)
            res.raise_for_status()
            json_data = res.json()
            
            if json_data.get("STATUS") == 0 or json_data.get("STATUS") == "0":
                results = json_data.get("result", {})
                return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
            else:
                raise Exception(json_data.get("ERRMSG", "登录鉴权失败"))
        except Exception as e:
            raise Exception(f"教务登录接口请求失败: {e}")

async def fetch_server_timestamp(use_vpn, cookies):
    """【核心修复】调用专用接口获取服务器时间戳"""
    urls = get_network_urls(use_vpn)
    async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
        res = await client.get(urls["timestamp"], headers={"User-Agent": UA}, timeout=10)
        res.raise_for_status()
        return res.json().get("timestamp")

async def execute_sign_in(use_vpn, session_id, cookies, uid, course_sched_id):
    """【核心修复】分离 Query 参数与 Body (FormData) 参数"""
    urls = get_network_urls(use_vpn)
    
    # 1. 获取服务器时间戳
    server_ts = await fetch_server_timestamp(use_vpn, cookies)
    if not server_ts: raise Exception("获取服务器时间戳失败")

    # 2. 执行签到
    async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
        headers = {"Sessionid": session_id, "User-Agent": UA}
        # Params 进 URL, Data 进 Body (Form-Data)
        res = await client.post(
            urls["sign"], 
            params={"courseSchedId": course_sched_id, "timestamp": str(server_ts)},
            data={"id": uid}, 
            headers=headers, 
            timeout=10
        )
        res.raise_for_status()
        return res.json()


# 4. 指令处理器
duaa_cmd = on_command("duaa", priority=5, block=True)

@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("🚀 Duaa 助手：\n/duaa 绑定 [学号] [ID] [可选密码]\n/duaa 解绑 [ID]\n/duaa 课表 [ID]\n/duaa 签到 [ID] [序号] [-su]\n/duaa 刷新任务 (重置今日分配)\n/duaa 设签到 [ID] [序号] [HH:MM]\n/duaa 开启自动签到\n/duaa 全局账号 [学号] [密码]")
    
    action, qq_id = sub_cmd[0], str(event.get_user_id())
    data = load_user_data(qq_id); accounts = data.get("accounts", {})

    # --- [全局账号、开启自动签到、解绑 逻辑重构补全] ---
    if action == "全局账号":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 全局账号 [学号] [密码]")
        set_shared_vpn(sub_cmd[1], sub_cmd[2]); await duaa_cmd.finish("✅ 全局共享凭据更新成功。")

    elif action == "开启自动签到":
        group_id = getattr(event, "group_id", None)
        if not group_id: await duaa_cmd.finish("⚠️ 请在你想开启自动签到的【群聊】中使用此指令！私聊无效。")
        data["notify_group"] = group_id
        save_user_data(qq_id, data)
        await duaa_cmd.send("✅ 自动签到已开启！\n每天 7:00 自动分配课前时间点进行嗅探。")
        return

    elif action == "解绑":
        if len(sub_cmd) < 2: await duaa_cmd.finish("用法：/duaa 解绑 [自定义ID]")
        alias = sub_cmd[1]
        if alias in accounts:
            del accounts[alias]
            save_user_data(qq_id, data)
            await duaa_cmd.finish(f"✅ 已成功解绑账号：{alias}")
        else:
            await duaa_cmd.finish(f"❓ 未找到名为 [{alias}] 的账号。")

    elif action == "刷新任务":
        today_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
        count_all = 0
        for alias, acc in accounts.items():
            try:
                uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
                has_vpn = bool(acc.get('password') or get_shared_vpn()[1])
                urls = get_network_urls(has_vpn)
                async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
                    res = await client.get(urls["schedule"], params={"id": uid, "dateStr": today_str}, headers={"Sessionid": sess, "User-Agent": UA})
                    if res.status_code == 200 and str(res.json().get("STATUS")) == "0":
                        sched = res.json().get("result", [])
                        for course in sched:
                            begin_str = course.get("classBeginTime", "")
                            if begin_str:
                                dt = datetime.strptime(begin_str.split(" ")[-1][:5], "%H:%M")
                                course["auto_sign_trigger_hm"] = (dt - timedelta(minutes=random.randint(5, 12))).strftime("%H:%M")
                                course["retries"] = 0
                                count_all += 1
                        acc["today_schedule"] = sched; acc["schedule_date"] = today_str
            except: pass
        save_user_data(qq_id, data); await duaa_cmd.finish(f"✅ 任务刷新完毕，今日共检测到 {count_all} 节课。")

    elif action == "设签到":
        if len(sub_cmd) < 4: await duaa_cmd.finish("用法：/duaa 设签到 [ID] [序号] [HH:MM]")
        alias, idx_str, time_str = sub_cmd[1], sub_cmd[2], sub_cmd[3]
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        try:
            idx = int(idx_str) - 1
            sched = accounts[alias].get("today_schedule", [])
            sched[idx]["auto_sign_trigger_hm"] = time_str
            sched[idx]["retries"] = 0
            save_user_data(qq_id, data)
            await duaa_cmd.send(f"✅ 已将《{sched[idx]['courseName']}》签到时间设为 {time_str}")
        except: await duaa_cmd.finish("❌ 设定失败，请检查序号和格式。")

    # --- [基础逻辑：绑定、课表、签到] ---
    elif action == "绑定":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 绑定 [学号] [自定义ID] [密码（选填）]")
        sid, alias, password = sub_cmd[1], sub_cmd[2], (sub_cmd[3] if len(sub_cmd) > 3 else None)
        try:
            uid, sess, real_name, cookies = await perform_duaa_login(sid, password)
            accounts[alias] = {"student_id": sid, "password": password, "real_name": real_name, "cookies": cookies}
            data["accounts"] = accounts; save_user_data(qq_id, data)
            await duaa_cmd.send(f"✅ 绑定成功：{real_name} ({sid})")
        except Exception as e: 
            await duaa_cmd.finish(str(e))

    elif action == "课表":
        alias = sub_cmd[1] if len(sub_cmd) > 1 else (list(accounts.keys())[0] if len(accounts) == 1 else None)
        if not alias or alias not in accounts: await duaa_cmd.finish("❓ 未找到该账号")
        acc = accounts[alias]
        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            has_vpn = bool(acc.get('password') or get_shared_vpn()[1])
            date_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
            urls = get_network_urls(has_vpn)
            
            async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
                res = await client.get(urls["schedule"], params={"id": uid, "dateStr": date_str}, headers={"Sessionid": sess, "User-Agent": UA})
                res.raise_for_status()
                json_res = res.json()
            
            if str(json_res.get("STATUS")) != "0": raise Exception(json_res.get("ERRMSG", "接口返回错误"))
            
            sched = json_res.get("result", [])
            # 合并触发时间
            old_times = {c["id"]: c.get("auto_sign_trigger_hm") for c in acc.get("today_schedule", [])}
            for c in sched: c["auto_sign_trigger_hm"] = old_times.get(c["id"])
            
            acc["today_schedule"] = sched; acc["schedule_date"] = date_str 
            save_user_data(qq_id, data)
            
            if not sched: await duaa_cmd.finish(f"📅 {acc['real_name']} 今日无课")
            
            msg = f"📅 {acc['real_name']} 的今日课表:\n"
            for i, c in enumerate(sched, 1):
                status = "✅已签" if str(c.get("signStatus")) == "1" else "⏳未签"
                trig = f" | ⏰打卡: {c.get('auto_sign_trigger_hm')}" if c.get("auto_sign_trigger_hm") and status == "⏳未签" else ""
                # 恢复 classroomName 的备用读取逻辑
                room = c.get("roomName") or c.get("classroomName") or "未知"
                msg += f"\n[{i}] 📖 {c['courseName']}\n    📍 {room} | {status}{trig}"
            await duaa_cmd.send(msg)
        except Exception as e: await duaa_cmd.finish(f"❌ 查课表失败: {e}")

    elif action == "签到":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号] [-su]")
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        is_su = "-su" in sub_cmd  
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        acc = accounts[alias]
        try: idx = int(idx_str) - 1
        except: await duaa_cmd.finish("❌ 序号错误")
            
        sched = acc.get("today_schedule", [])
        if idx < 0 or idx >= len(sched): await duaa_cmd.finish("⚠️ 找不到该课程")
        target = sched[idx]

        if not is_su:
            now = datetime.now(TZ_BEIJING)
            try:
                t = datetime.strptime(target.get("classBeginTime", "").split(" ")[-1][:5], "%H:%M")
                if now < now.replace(hour=t.hour, minute=t.minute) - timedelta(minutes=15):
                    await duaa_cmd.finish("⚠️ 尚未开放签到。")
            except: pass

        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            res_data = await execute_sign_in(bool(acc.get('password') or get_shared_vpn()[1]), sess, cookies, uid, target["id"])
            if (str(res_data.get("STATUS")) == "0" and str(res_data.get("result", {}).get("stuSignStatus")) == "1"):
                target["signStatus"] = "1"; save_user_data(qq_id, data)
                await duaa_cmd.send(f"🎯 《{target['courseName']}》签到成功！")
            else:
                await duaa_cmd.send(f"❌ 签到失败：{res_data.get('ERRMSG', '未知错误')}")
        except Exception as e: await duaa_cmd.finish(f"❌ 执行错误: {e}")


# ==========================================
# 5. 自动签到定时任务模块 (全功能完整版)
# ==========================================

@scheduler.scheduled_job("cron", hour=0, minute=0, id="duaa_midnight")
async def midnight_sleep_reminder():
    """每天半夜12点提醒全体成员睡觉"""
    bots = get_bots(); bot = list(bots.values())[0] if bots else None
    if not bot: return
    groups = {load_user_data(f.stem).get("notify_group") for f in USER_DIR.glob("*.json") if load_user_data(f.stem).get("notify_group")}
    for gid in groups:
        try: await bot.send_group_msg(group_id=gid, message="[CQ:at,qq=all] 🌙 滴滴！现在是半夜12点。宝宝们快去睡觉吧！晚安~")
        except: pass

@scheduler.scheduled_job("cron", hour=7, minute=0, id="duaa_daily_sync")
async def daily_sync():
    """早上7点自动同步并发送 @ 通知"""
    bots = get_bots(); bot = list(bots.values())[0] if bots else None
    today_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
    for file in USER_DIR.glob("*.json"):
        qq_id = file.stem; data = load_user_data(qq_id); group_id = data.get("notify_group")
        if not group_id: continue
        changed, count = False, 0
        for alias, acc in data.get("accounts", {}).items():
            try:
                uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
                urls = get_network_urls(bool(acc.get('password') or get_shared_vpn()[1]))
                async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
                    res = await client.get(urls["schedule"], params={"id": uid, "dateStr": today_str}, headers={"Sessionid": sess, "User-Agent": UA})
                    if res.status_code == 200 and str(res.json().get("STATUS")) == "0":
                        sched = res.json().get("result", [])
                        for course in sched:
                            t_str = course.get("classBeginTime", "")
                            if t_str:
                                dt = datetime.strptime(t_str.split(" ")[-1][:5], "%H:%M")
                                course["auto_sign_trigger_hm"] = (dt - timedelta(minutes=random.randint(5, 12))).strftime("%H:%M")
                                course["retries"] = 0; count += 1
                        acc["today_schedule"] = sched; acc["schedule_date"] = today_str; changed = True
            except: pass
        if changed:
            save_user_data(qq_id, data)
            if bot and count > 0:
                await bot.send_group_msg(group_id=group_id, message=f"[CQ:at,qq={qq_id}] 🌅 早上好！今日检测到 {count} 节课，已为你安排好自动打卡。")

@scheduler.scheduled_job("cron", minute="*", id="duaa_auto_checkin_executor")
async def auto_checkin_executor():
    """每分钟轮询自动打卡"""
    bots = get_bots(); bot = list(bots.values())[0] if bots else None
    if not bot: return
    now_hm, today_str = datetime.now(TZ_BEIJING).strftime("%H:%M"), datetime.now(TZ_BEIJING).strftime("%Y%m%d")
    for file in USER_DIR.glob("*.json"):
        qq_id = file.stem; data = load_user_data(qq_id); group_id = data.get("notify_group")
        if not group_id: continue
        changed = False
        for alias, acc in data.get("accounts", {}).items():
            if acc.get("schedule_date") != today_str: continue
            for course in acc.get("today_schedule", []):
                trig = course.get("auto_sign_trigger_hm")
                if trig and now_hm >= trig and str(course.get("signStatus")) != "1" and course.get("retries", 0) < 10:
                    course["retries"] = course.get("retries", 0) + 1; changed = True
                    try:
                        uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
                        res = await execute_sign_in(bool(acc.get('password') or get_shared_vpn()[1]), sess, cookies, uid, course["id"])
                        suc = (str(res.get("STATUS")) == "0" and str(res.get("result", {}).get("stuSignStatus")) == "1")
                        msg_err = res.get('ERRMSG', '')
                        if suc or "已签到" in msg_err:
                            course["signStatus"] = "1"
                            await bot.send_group_msg(group_id=group_id, message=f"[CQ:at,qq={qq_id}] 🤖 自动签到成功！\n账号：[{alias}]\n课程：《{course.get('courseName')}》")
                        elif "结束" in msg_err or "不存在" in msg_err: course["retries"] = 99
                    except: pass
        if changed: save_user_data(qq_id, data)