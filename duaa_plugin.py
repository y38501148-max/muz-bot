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
CONFIG_FILE = BASE_DATA_DIR / "config.json"
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

# 3. 核心 API
async def fetch_execution(client: httpx.AsyncClient):
    try:
        res = await client.get(SSO_LOGIN_URL, timeout=10)
        res.raise_for_status()
        match = re.search(r'name="execution"\s+value="([^"]+)"', res.text)
        if match: return match.group(1)
        raise ValueError("无法从 SSO 页面解析到 execution 参数，请确认网络环境。")
    except Exception as e:
        raise Exception(f"获取 SSO 认证页面失败: {e}")

async def sso_login(client: httpx.AsyncClient, username, password):
    execution = await fetch_execution(client)
    logger.debug(f"SSO 认证正在执行 (Execution: {execution[:10]}...)")
    try:
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
    except Exception as e:
        raise Exception(f"SSO 网络请求超时或中断: {e}")

    final_url = str(res.url)
    # 检查状态码与重定向结果
    if res.status_code == 401:
        raise ValueError("SSO 认证失败：学号或密码不正确。")
    
    # 成功跳转标识
    if "iclass.buaa.edu.cn" in final_url or "https-834" in final_url:
        return True
    
    # VPN 门户
    if "d.buaa.edu.cn" in final_url and "/login" not in final_url:
        urls = get_network_urls(True)
        try:
            probe_res = await client.get(urls["service_home"] + "/", timeout=10)
            if "iclass.buaa.edu.cn" in str(probe_res.url) or "https-834" in str(probe_res.url):
                return True
        except Exception as e:
            raise Exception(f"VPN 隧道探测失败 (d.buaa.edu.cn 已登入但无法访问 iClass): {e}")

    # 解析错误提示
    error_msg = "未知错误"
    # 尝试在页面中查找特定的错误提示（针对 CAS 常见的错误 div）
    msg_match = re.search(r'class="msg.*?>(.*?)<', res.text, re.S)
    if msg_match:
        error_msg = msg_match.group(1).strip()
    elif "密码错误" in res.text:
        error_msg = "密码错误"
    elif "验证码" in res.text:
        error_msg = "检测到验证码挑战，需要手动处理或等待。"
    
    raise ValueError(f"SSO 登录未成功。状态码: {res.status_code}, 提示: {error_msg}, 最终地址: {final_url}")

async def perform_duaa_login(target_student_id, personal_password=None):
    vpn_user, vpn_pass = None, None
    if personal_password:
        vpn_user, vpn_pass = target_student_id, personal_password
    else:
        vpn_user, vpn_pass = get_shared_vpn()

    use_vpn = bool(vpn_pass)
    async with httpx.AsyncClient(verify=False, follow_redirects=True) as client:
        if use_vpn:
            logger.info(f"正在穿透 VPN: 凭据 {vpn_user}")
            try:
                await sso_login(client, vpn_user, vpn_pass)
            except Exception as e:
                # 抛出详细异常
                raise Exception(f"SSO 穿透失败: {str(e)}")
            
            urls = get_network_urls(True)
            try:
                res = await client.get(urls["user_login"], params={"phone": target_student_id, "password": "", "verificationType": "2", "userLevel": "1"}, headers={"User-Agent": UA}, timeout=10)
                res.raise_for_status()
                json_data = res.json()
                if json_data.get("STATUS") == "0":
                    results = json_data.get("result", {})
                    return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
                raise Exception(json_data.get("ERRMSG", "教务接口返回异常"))
            except Exception as e:
                raise Exception(f"VPN 隧道内获取教务 Session 失败: {e}")
        else:
            last_err = None
            for port in PORTS:
                try:
                    urls = get_network_urls(False, port)
                    res = await client.get(urls["user_login"], params={"phone": target_student_id, "password": "", "verificationType": "2", "userLevel": "1"}, headers={"User-Agent": UA}, timeout=10)
                    res.raise_for_status()
                    json_data = res.json()
                    if json_data.get("STATUS") == "0":
                        results = json_data.get("result", {})
                        return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
                    raise Exception(json_data.get("ERRMSG", "直连拒绝"))
                except Exception as e:
                    last_err = e; continue
            raise Exception(f"直连失败: {last_err} (外网环境下请先提供密码或配置全局账号)")

async def call_api(use_vpn, session_id, cookies, path_key, params, is_post=False):
    ports_to_try = ["8347"] if use_vpn else PORTS
    last_err = None
    async with httpx.AsyncClient(verify=False, follow_redirects=True, cookies=cookies or {}) as client:
        for port in ports_to_try:
            urls = get_network_urls(use_vpn, port)
            try:
                headers = {"Sessionid": session_id, "User-Agent": UA}
                if is_post:
                    res = await client.post(urls[path_key], params=params, headers=headers, timeout=10)
                else:
                    res = await client.get(urls[path_key], params=params, headers=headers, timeout=10)
                res.raise_for_status()
                return res.json()
            except Exception as e:
                last_err = e; continue
        raise Exception(f"接口调用失败: {last_err}")

# 4. 指令处理器
duaa_cmd = on_command("duaa", priority=5, block=True)

@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("🚀 Duaa 助手：\n/duaa 绑定 [学号] [ID] [可选密码]\n/duaa 课表 [ID]\n/duaa 签到 [ID] [序号] [-su]\n/duaa 全局账号 [学号] [密码]")
    
    action = sub_cmd[0]
    qq_id = str(event.get_user_id())
    data = load_user_data(qq_id)
    accounts = data.get("accounts", {})

    if action == "全局账号":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 全局账号 [学号] [密码]")
        set_shared_vpn(sub_cmd[1], sub_cmd[2])
        await duaa_cmd.finish("✅ 全局共享凭据更新成功。尝试执行指令以验证。")

    elif action == "绑定":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 绑定 [学号] [自定义ID] [密码（选填）]")
        sid, alias = sub_cmd[1], sub_cmd[2]
        password = sub_cmd[3] if len(sub_cmd) > 3 else None
        try:
            uid, sess, real_name, cookies = await perform_duaa_login(sid, password)
            accounts[alias] = {"student_id": sid, "password": password, "real_name": real_name, "cookies": cookies}
            data["accounts"] = accounts
            save_user_data(qq_id, data)
            await duaa_cmd.finish(f"✅ 绑定成功：{real_name} ({sid})")
        except Exception as e:
            await duaa_cmd.finish(str(e))

    elif action == "课表":
        alias = sub_cmd[1] if len(sub_cmd) > 1 else (list(accounts.keys())[0] if len(accounts) == 1 else None)
        if not alias or alias not in accounts: await duaa_cmd.finish("❓ 未找到该账号")
        acc = accounts[alias]
        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            # 这里的 use_vpn 判断改为读取当前是否有任何 VPN 凭据可用
            has_vpn = bool(acc.get('password') or get_shared_vpn()[1])
            res = await call_api(has_vpn, sess, cookies, "course_schedule", {"id": uid, "dateStr": datetime.now().strftime("%Y%m%d")})
            if res.get("STATUS") != "0": raise Exception(res.get("ERRMSG", "接口返回错误"))
            sched = res.get("result", [])
            acc["today_schedule"] = sched; save_user_data(qq_id, data)
            if not sched: await duaa_cmd.finish(f"📅 {acc['real_name']} 今日无课")
            msg = f"📅 {acc['real_name']} 的今日课表:\n"
            for i, c in enumerate(sched, 1):
                status = "✅已签" if str(c.get("signStatus")) == "1" else "⏳未签"
                room = c.get("roomName") or c.get("classroomName") or "未知"
                msg += f"\n[{i}] 📖 {c['courseName']}\n    📍 {room} | {status}"
            await duaa_cmd.finish(msg)
        except Exception as e:
            await duaa_cmd.finish(f"❌ 查课表失败: {e}")

    elif action == "签到":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号]")
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        acc = accounts[alias]; idx = int(idx_str) - 1
        sched = acc.get("today_schedule", [])
        if not sched or idx < 0 or idx >= len(sched): await duaa_cmd.finish("请刷新课表")
        target = sched[idx]
        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            ts = int(datetime.now().timestamp() * 1000) + 36000
            has_vpn = bool(acc.get('password') or get_shared_vpn()[1])
            res = await call_api(has_vpn, sess, cookies, "scan_sign", {"id": uid, "courseSchedId": target["id"], "timestamp": ts}, is_post=True)
            status = str(res.get("STATUS", res.get("status", "-1")))
            if status == "0": await duaa_cmd.finish(f"🎯 《{target['courseName']}》签到成功！")
            else: await duaa_cmd.finish(f"❌ 失败：{res.get('ERRMSG', '未知')}")
        except Exception as e:
            await duaa_cmd.finish(f"❌ 签到发生错误: {e}")

    elif action == "解绑":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请输入要解绑的 ID")
        alias = sub_cmd[1]
        if alias in accounts:
            info = accounts.pop(alias); data["accounts"] = accounts; save_user_data(qq_id, data)
            await duaa_cmd.finish(f"🗑️ 已成功解绑：{info['real_name']}")
        else: await duaa_cmd.finish("❌ 找不到该 ID")
