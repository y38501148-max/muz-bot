import httpx
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from nonebot import on_command, logger
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg

# 1. 路径配置
BASE_DATA_DIR = Path("data/duaa")
USER_DIR = BASE_DATA_DIR / "users"
USER_DIR.mkdir(parents=True, exist_ok=True)

SSO_LOGIN_URL = "https://d.buaa.edu.cn/https/77726476706e69737468656265737421e3e44ed225256951300d8db9d6562d/login"
VPN_SERVICE_ID = "77726476706e69737468656265737421f9f44d9d342326526b0988e29d51367ba018"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
PORTS = ["8347", "8346"]

def get_network_urls(use_vpn, port="8347"):
    if use_vpn:
        base = f"https://d.buaa.edu.cn/https-8347/{VPN_SERVICE_ID}"
        return {
            "service_home": base,
            "user_login": f"{base}/app/user/login.action",
            "course_schedule": f"{base}/app/course/get_stu_course_sched.action",
            "scan_sign": f"{base}/app/course/stu_scan_sign.action"
        }
    else:
        base = f"https://iclass.buaa.edu.cn:{port}"
        return {
            "service_home": base,
            "user_login": f"{base}/app/user/login.action",
            "course_schedule": f"{base}/app/course/get_stu_course_sched.action",
            "scan_sign": "http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action"
        }

# 2. 数据处理与迁移
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

# 3. 核心 API
async def fetch_execution(client: httpx.AsyncClient):
    res = await client.get(SSO_LOGIN_URL, timeout=10)
    res.raise_for_status()
    match = re.search(r'name="execution"\s+value="([^"]+)"', res.text)
    if match: return match.group(1)
    raise ValueError("无法从 SSO 页面解析 execution 参数")

async def sso_login(client: httpx.AsyncClient, username, password):
    execution = await fetch_execution(client)
    res = await client.post(
        SSO_LOGIN_URL,
        data={
            "username": username,
            "password": password,
            "submit": "登录",
            "type": "username_password",
            "execution": execution,
            "_eventId": "submit",
        },
        headers={"Referer": SSO_LOGIN_URL},
        timeout=15,
        follow_redirects=True
    )
    final_url = str(res.url)
    if "iclass.buaa.edu.cn" in final_url or "https-834" in final_url: return True
    if "d.buaa.edu.cn" in final_url and "/login" not in final_url:
        urls = get_network_urls(True)
        probe_res = await client.get(urls["service_home"] + "/", timeout=10)
        if "iclass.buaa.edu.cn" in str(probe_res.url) or "https-834" in str(probe_res.url): return True
        raise ValueError("建立 VPN 隧道失败")
    raise ValueError("SSO 登录失败（账号/密码错误或隧道异常）")

async def perform_duaa_login(student_id, password=None):
    use_vpn = bool(password)
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        if use_vpn:
            await sso_login(client, student_id, password)
            urls = get_network_urls(True)
            res = await client.get(urls["user_login"], params={"phone": student_id, "password": "", "verificationType": "2", "userLevel": "1"}, headers={"User-Agent": UA}, timeout=10)
            res.raise_for_status()
            json_data = res.json()
            if json_data.get("STATUS") == "0":
                results = json_data.get("result", {})
                return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
            raise Exception(json_data.get("ERRMSG", "VPN 登录状态异常"))
        else:
            last_err = None
            for port in PORTS:
                try:
                    urls = get_network_urls(False, port)
                    res = await client.get(urls["user_login"], params={"phone": student_id, "password": "", "verificationType": "2", "userLevel": "1"}, headers={"User-Agent": UA}, timeout=10)
                    res.raise_for_status()
                    json_data = res.json()
                    if json_data.get("STATUS") == "0":
                        results = json_data.get("result", {})
                        return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
                    raise Exception(json_data.get("ERRMSG", "登录状态异常"))
                except Exception as e:
                    last_err = e
                    continue
            raise Exception(f"直连登录失败, 所有端口尝试完毕: {last_err}")

async def call_api(use_vpn, session_id, cookies, path_key, params, is_post=False):
    # 如果是直连，自动双端口重试
    ports_to_try = ["8347"] if use_vpn else PORTS
    last_err = None
    
    async with httpx.AsyncClient(verify=False, follow_redirects=True, cookies=cookies or {}) as client:
        for port in ports_to_try:
            urls = get_network_urls(use_vpn, port)
            req_url = urls[path_key]
            try:
                headers = {"Sessionid": session_id, "User-Agent": UA}
                if is_post:
                    res = await client.post(req_url, params=params, headers=headers, timeout=10)
                else:
                    res = await client.get(req_url, params=params, headers=headers, timeout=10)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                last_err = e
                continue
        raise Exception(f"请求失败 ({path_key}): {last_err}")

# 4. 指令处理器
duaa_cmd = on_command("duaa", priority=5, block=True)

@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("🚀 Duaa 助手：\n/duaa 绑定 [学号] [ID] [密码（选填，用于校外）]\n/duaa 课表 [ID]\n/duaa 签到 [ID] [序号] [-su]")
    
    action = sub_cmd[0]
    qq_id = str(event.get_user_id())
    data = load_user_data(qq_id)
    accounts = data.get("accounts", {})

    if action == "绑定":
        if len(sub_cmd) < 3: await duaa_cmd.finish("请输入：/duaa 绑定 [学号] [自定义ID] [密码（外网使用需提供）]")
        sid, alias = sub_cmd[1], sub_cmd[2]
        password = sub_cmd[3] if len(sub_cmd) > 3 else None
        try:
            uid, sess, real_name, cookies = await perform_duaa_login(sid, password)
        except Exception as e:
            await duaa_cmd.finish(f"❌ 绑定失败: {e}")
        
        accounts[alias] = {"student_id": sid, "password": password, "real_name": real_name, "cookies": cookies}
        data["accounts"] = accounts
        save_user_data(qq_id, data)
        mode = "VPN (校外自动)" if password else "直连 (校内专线)"
        await duaa_cmd.finish(f"✅ 绑定成功！\n网络模式：{mode}\nID：{alias}\n姓名：{real_name}\n学号：{sid}")

    elif action == "课表":
        alias = sub_cmd[1] if len(sub_cmd) > 1 else (list(accounts.keys())[0] if len(accounts) == 1 else None)
        if not alias or alias not in accounts:
            await duaa_cmd.finish(f"❓ 请指定预览哪个账号的课表。\n当前可选 ID：{', '.join(accounts.keys())}")
        
        acc = accounts[alias]
        date_str = datetime.now().strftime("%Y%m%d")
        
        async def fetch_sched():
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            acc["cookies"] = cookies
            json_data = await call_api(bool(acc.get('password')), sess, cookies, "course_schedule", {"id": uid, "dateStr": date_str})
            if json_data.get("STATUS") == "0": return json_data.get("result", [])
            raise Exception(json_data.get("ERRMSG", "获取异常"))
            
        try:
            sched = await fetch_sched()
        except Exception as e:
            await duaa_cmd.finish(f"❌ 课表刷新失败: {e}")
            
        acc["today_schedule"] = sched
        save_user_data(qq_id, data)

        if not sched: await duaa_cmd.finish(f"📅 {acc['real_name']} 今日无课")
        
        msg = f"📅 {acc['real_name']} ({alias}) 的今日课表:\n"
        for i, c in enumerate(sched, 1):
            status = "✅已签" if str(c.get("signStatus")) == "1" else "⏳未签"
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
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号] [-su]")
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        
        if alias not in accounts: await duaa_cmd.finish(f"❌ 找不到 ID 为 {alias} 的账号")
        acc = accounts[alias]
        
        try: idx = int(idx_str) - 1
        except: await duaa_cmd.finish("序号无效")
        
        force_mode = "-su" in sub_cmd
        sched = acc.get("today_schedule", [])
        if not sched or idx < 0 or idx >= len(sched): await duaa_cmd.finish(f"请先发送 [/duaa 课表 {alias}] 刷新序号")
        
        target = sched[idx]
        if str(target.get("signStatus")) == "1": await duaa_cmd.finish("已经签过啦")

        if not force_mode:
            now = datetime.now()
            fmt = "%Y-%m-%d %H:%M:%S"
            begin_t = datetime.strptime(target["classBeginTime"], fmt)
            end_t = datetime.strptime(target["classEndTime"], fmt)
            if now < begin_t - timedelta(minutes=10): await duaa_cmd.finish("⏰ 还没到时候")
            if now > end_t - timedelta(minutes=1): await duaa_cmd.finish("🚫 窗口已关闭")

        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            acc["cookies"] = cookies
            save_user_data(qq_id, data)
            
            ts = int(datetime.now().timestamp() * 1000) + 36000
            json_data = await call_api(bool(acc.get('password')), sess, cookies, "scan_sign", {"id": uid, "courseSchedId": target["id"], "timestamp": ts}, is_post=True)
            
            status_val = str(json_data.get("STATUS", json_data.get("status", "-1")))
            if status_val == "0":
                await duaa_cmd.finish(f"🎯 {acc['real_name']} - 《{target['courseName']}》签到成功！")
            else:
                err_msg = json_data.get('ERRMSG', json_data.get('ERRORMSG', '未知'))
                if "已签到" in err_msg:
                    await duaa_cmd.finish(f"🎯 {acc['real_name']} - 《{target['courseName']}》已签过到。")
                else:
                    await duaa_cmd.finish(f"❌ 失败：{err_msg}")
        except Exception as e:
            await duaa_cmd.finish(f"❌ 请求失败: {e}")
