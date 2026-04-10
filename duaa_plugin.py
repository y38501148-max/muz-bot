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
# iClass 在 WebVPN 中的通用标识（不区分端口）
ICLASS_VPN_ID = "77726476706e69737468656265737421"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
PORTS = ["8347", "8346"]

def get_network_urls(use_vpn, port="8347"):
    if use_vpn:
        # 基于端口动态构建 WebVPN 代理地址
        # 形式通常为 https://d.buaa.edu.cn/https-端口/777...
        port_suffix = f"-{port}" if port != "443" else ""
        base = f"https://d.buaa.edu.cn/https{port_suffix}/{VPN_SERVICE_ID}"
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

async def sso_login(client: httpx.AsyncClient, username, password):
    """
    修复后的 SSO 登录逻辑：
    先访问 WebVPN 入口，跟随重定向到真实的 SSO，提取真实 execution，
    最后将表单提交到真实的 SSO 地址，并跟随重定向带回 VPN Cookie。
    """
    vpn_entry_url = "https://d.buaa.edu.cn/login"
    
    try:
        # 1. 访问 WebVPN 登录入口，触发重定向，获取真实的 SSO 登录页 URL
        res = await client.get(vpn_entry_url, timeout=10)
        res.raise_for_status()
        
        # 记录此时真正的 SSO 页面地址（也是我们 POST 的目标地址）
        real_sso_url = str(res.url)
        
        # 2. 从真实的页面中提取防伪造令牌 execution
        execution_match = re.search(r'name="execution"\s+value="([^"]+)"', res.text)
        if not execution_match:
            raise ValueError("无法在 SSO 页面解析 execution 参数。")
        execution = execution_match.group(1)

        # 3. 组装提交表单，并向真实的 SSO 地址发起 POST 请求
        # 注意：此处让 follow_redirects=True，CAS 验证成功后会自动 302 跳回 d.buaa.edu.cn
        post_data = {
            "username": username,
            "password": password,  # ⚠️ 注意看下方说明
            "submit": "登录",
            "type": "username_password",
            "execution": execution,
            "_eventId": "submit",
        }

        login_res = await client.post(
            real_sso_url,
            data=post_data,
            headers={"Referer": real_sso_url},
            timeout=15,
            follow_redirects=True 
        )
        
    except Exception as e:
        raise Exception(f"SSO 登录过程网络异常: {e}")

    final_url = str(login_res.url)
    
    # 4. 结果校验
    if login_res.status_code == 401 or "密码错误" in login_res.text:
        raise ValueError("SSO 认证失败：学号或密码错误，或触发了前端加密校验。")
    
    # 检查是否成功回到了 WebVPN 的域下，或者是否拿到了 Wengine 的核心 Cookie
    # 拿到 TWINKLE 或 wengine_vpn_ticket 代表隧道打通
    vpn_cookies = [c.name for k, c in client.cookies.jar._cookies.items() for _, c in c.items() for _, c in c.items()]
    is_in_vpn = "d.buaa.edu.cn" in final_url or any("wengine" in name.lower() or "twinkle" in name.lower() for name in vpn_cookies)

    if is_in_vpn:
        # 嗅探探测：仅作为隧道预热，不再作为判定生死的前提
        probe_urls = get_network_urls(True, "8347")
        try:
            probe_res = await client.get(probe_urls["service_home"] + "/", timeout=8)
            logger.info(f"VPN 隧道嗅探预热完成，状态码: {probe_res.status_code}")
        except Exception as e:
            logger.warning(f"隧道打通但嗅探预热异常: {e}")
            
        # 只要最终 URL 在 WebVPN 域下，直接认定 SSO 穿透成功！
        return True
    
    # 如果没进 is_in_vpn，提取页面上的具体报错（比如密码错误）
    error_msg = "未知错误"
    msg_match = re.search(r'class="msg.*?>(.*?)<', login_res.text, re.S)
    if msg_match: error_msg = msg_match.group(1).strip()
    
    raise ValueError(f"SSO 穿透失败。最终停留地址: {final_url}, 提示: {error_msg}")
    
    # 提取页面上的具体报错（如果还在 SSO 页面）
    error_msg = "未知错误"
    msg_match = re.search(r'class="msg.*?>(.*?)<', login_res.text, re.S)
    if msg_match: error_msg = msg_match.group(1).strip()
    
    raise ValueError(f"SSO 穿透失败。最终停留地址: {final_url}, 提示: {error_msg}")

async def perform_duaa_login(target_student_id, personal_password=None):
    vpn_user, vpn_pass = (target_student_id, personal_password) if personal_password else get_shared_vpn()
    use_vpn = bool(vpn_pass)
    
    # 设置 verify=False 时关闭警告（可选）
    async with httpx.AsyncClient(verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
        if use_vpn:
            logger.info(f"使用凭据 {vpn_user} 尝试 SSO 穿透...")
            await sso_login(client, vpn_user, vpn_pass)
            
            # 隧道已打通，开始请求教务 API
            last_err = None
            for port in PORTS:
                try:
                    urls = get_network_urls(True, port)
                    res = await client.get(urls["user_login"], params={"phone": target_student_id, "password": "", "verificationType": "2", "userLevel": "1"}, timeout=10)
                    res.raise_for_status()
                    json_data = res.json()
                    if json_data.get("STATUS") == "0":
                        results = json_data.get("result", {})
                        return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
                except Exception as e:
                    last_err = e; continue
            raise Exception(f"VPN 隧道已打通，但无法通过隧道登录教务: {last_err}")
        else:
            # 直连逻辑不变
            last_err = None
            for port in PORTS:
                try:
                    urls = get_network_urls(False, port)
                    res = await client.get(urls["user_login"], params={"phone": target_student_id, "password": "", "verificationType": "2", "userLevel": "1"}, timeout=10)
                    res.raise_for_status()
                    json_data = res.json()
                    if json_data.get("STATUS") == "0":
                        results = json_data.get("result", {})
                        return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
                except Exception as e:
                    last_err = e; continue
            raise Exception(f"直连失败: {last_err} (外网用户请提供密码或配置全局账号)")

async def call_api(use_vpn, session_id, cookies, path_key, params, is_post=False):
    ports_to_try = PORTS
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
    
    action, qq_id = sub_cmd[0], str(event.get_user_id())
    data = load_user_data(qq_id); accounts = data.get("accounts", {})

    if action == "全局账号":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 全局账号 [学号] [密码]")
        set_shared_vpn(sub_cmd[1], sub_cmd[2]); await duaa_cmd.finish("✅ 全局共享凭据更新成功。")

    elif action == "绑定":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 绑定 [学号] [自定义ID] [密码（选填）]")
        sid, alias, password = sub_cmd[1], sub_cmd[2], (sub_cmd[3] if len(sub_cmd) > 3 else None)
        try:
            uid, sess, real_name, cookies = await perform_duaa_login(sid, password)
            accounts[alias] = {"student_id": sid, "password": password, "real_name": real_name, "cookies": cookies}
            data["accounts"] = accounts; save_user_data(qq_id, data)
            await duaa_cmd.finish(f"✅ 绑定成功：{real_name} ({sid})")
        except Exception as e: await duaa_cmd.finish(str(e))

    elif action == "课表":
        alias = sub_cmd[1] if len(sub_cmd) > 1 else (list(accounts.keys())[0] if len(accounts) == 1 else None)
        if not alias or alias not in accounts: await duaa_cmd.finish("❓ 未找到该账号")
        acc = accounts[alias]
        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
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
        except Exception as e: await duaa_cmd.finish(f"❌ 查课表失败: {e}")

    elif action == "签到":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号]")
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        acc = accounts[alias]; idx, sched = int(idx_str) - 1, acc.get("today_schedule", [])
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
        except Exception as e: await duaa_cmd.finish(f"❌ 签到发生错误: {e}")

    elif action == "解绑":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请输入要解绑的 ID")
        alias = sub_cmd[1]
        if alias in accounts:
            info = accounts.pop(alias); data["accounts"] = accounts; save_user_data(qq_id, data)
            await duaa_cmd.finish(f"🗑️ 已成功解绑：{info['real_name']}")
        else: await duaa_cmd.finish("❌ 找不到该 ID")
