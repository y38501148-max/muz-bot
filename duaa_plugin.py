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
# 伪装成安卓微信客户端
UA = "Mozilla/5.0 (Linux; Android 13; M2012K11AC Build/TKQ1.220829.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 wxwork/4.1.22 MicroMessenger/7.0.1 NetType/WIFI Language/zh ColorScheme/Light"
PORTS = ["8347", "8346"]

def get_network_urls(use_vpn, port="8347"):
    if use_vpn:
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
    vpn_entry_url = "https://d.buaa.edu.cn/login"
    try:
        res = await client.get(vpn_entry_url, timeout=10)
        res.raise_for_status()
        real_sso_url = str(res.url)
        
        execution_match = re.search(r'name="execution"\s+value="([^"]+)"', res.text)
        if not execution_match:
            raise ValueError("无法在 SSO 页面解析 execution 参数。")
        execution = execution_match.group(1)

        post_data = {
            "username": username,
            "password": password,
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
    
    if login_res.status_code == 401 or "密码错误" in login_res.text:
        raise ValueError("SSO 认证失败：学号或密码错误，或触发了前端加密校验。")
    
    vpn_cookies = [c.name for k, c in client.cookies.jar._cookies.items() for _, c in c.items() for _, c in c.items()]
    is_in_vpn = "d.buaa.edu.cn" in final_url or any("wengine" in name.lower() or "twinkle" in name.lower() for name in vpn_cookies)

    if is_in_vpn:
        probe_urls = get_network_urls(True, "8347")
        try:
            probe_res = await client.get(probe_urls["service_home"] + "/", timeout=8)
            logger.info(f"VPN 隧道嗅探预热完成，状态码: {probe_res.status_code}")
        except Exception as e:
            logger.warning(f"隧道打通但嗅探预热异常: {e}")
        return True
    
    error_msg = "未知错误"
    msg_match = re.search(r'class="msg.*?>(.*?)<', login_res.text, re.S)
    if msg_match: error_msg = msg_match.group(1).strip()
    
    raise ValueError(f"SSO 穿透失败。最终停留地址: {final_url}, 提示: {error_msg}")

async def perform_duaa_login(target_student_id, personal_password=None):
    vpn_user, vpn_pass = (target_student_id, personal_password) if personal_password else get_shared_vpn()
    use_vpn = bool(vpn_pass)
    
    async with httpx.AsyncClient(verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
        if use_vpn:
            logger.info(f"使用凭据 {vpn_user} 尝试 SSO 穿透...")
            await sso_login(client, vpn_user, vpn_pass)
            
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
                # 【改回 params=params】
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
            await duaa_cmd.send(f"✅ 绑定成功：{real_name} ({sid})")
            return
        except Exception as e: 
            await duaa_cmd.finish(str(e))

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
            if not sched: 
                await duaa_cmd.send(f"📅 {acc['real_name']} 今日无课")
                return
            
            msg = f"📅 {acc['real_name']} 的今日课表:\n"
            for i, c in enumerate(sched, 1):
                status = "✅已签" if str(c.get("signStatus")) == "1" else "⏳未签"
                room = c.get("roomName") or c.get("classroomName") or "未知"
                msg += f"\n[{i}] 📖 {c['courseName']}\n    📍 {room} | {status}"
            
            await duaa_cmd.send(msg)
            return
        except Exception as e: 
            await duaa_cmd.finish(f"❌ 查课表失败: {e}")

    elif action == "签到":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号] [-su]")
        
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        is_su = "-su" in sub_cmd  # 提取是否包含超级用户模式参数
        
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        acc = accounts[alias]
        
        try:
            idx = int(idx_str) - 1
        except ValueError:
            await duaa_cmd.finish("❌ 序号必须是数字")
            
        sched = acc.get("today_schedule", [])
        if not sched or idx < 0 or idx >= len(sched): 
            await duaa_cmd.finish("⚠️ 找不到该课程，请先发送 /duaa 课表 刷新今日课程信息。")
            
        target = sched[idx]

        # 【新增】本地时间校验逻辑
        if not is_su:
            now = datetime.now()
            begin_str = target.get("classBeginTime", "00:00")
            end_str = target.get("classEndTime", "23:59")
            
            try:
                # 补全格式以兼容 datetime 解析 (可能为 HH:MM 或 HH:MM:SS)
                if len(begin_str) == 5: begin_str += ":00"
                if len(end_str) == 5: end_str += ":00"
                
                start_dt = datetime.strptime(begin_str, "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day)
                end_dt = datetime.strptime(end_str, "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day)
                
                # 设定可签到区间：课前10分钟 ~ 下课前1分钟
                valid_start = start_dt - timedelta(minutes=10)
                valid_end = end_dt - timedelta(minutes=1)
                
                if not (valid_start <= now <= valid_end):
                    await duaa_cmd.send(
                        f"⚠️ 拦截：当前不在《{target['courseName']}》的正常签到时间内！\n"
                        f"允许时段：{valid_start.strftime('%H:%M')} ~ {valid_end.strftime('%H:%M')}\n"
                        f"💡 如需强制签到，请加上 -su 参数，例如：\n/duaa 签到 {alias} {idx_str} -su"
                    )
                    return
            except Exception as e:
                logger.warning(f"课表时间解析失败，默认放行。错误信息: {e}")

        try:
            uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
            
            # 【关键修复】减去10秒，防止未来时间戳被拦截，并转为字符串
            ts_str = str(int(datetime.now().timestamp() * 1000) - 10000)
            has_vpn = bool(acc.get('password') or get_shared_vpn()[1])
            
            payload = {"id": uid, "courseSchedId": target["id"], "timestamp": ts_str}
            
            res = await call_api(has_vpn, sess, cookies, "scan_sign", payload, is_post=True)
            status = str(res.get("STATUS", res.get("status", "-1")))
            
            if status == "0": 
                su_tip = " (☢️强制模式)" if is_su else ""
                await duaa_cmd.send(f"🎯 《{target['courseName']}》签到成功！{su_tip}")
            else: 
                await duaa_cmd.send(f"❌ 失败：{res.get('ERRMSG', '未知')}")
            return
        except Exception as e: 
            await duaa_cmd.finish(f"❌ 签到发生错误: {e}")

    elif action == "解绑":
        if len(sub_cmd) < 2: await duaa_cmd.finish("请输入要解绑的 ID")
        alias = sub_cmd[1]
        if alias in accounts:
            info = accounts.pop(alias); data["accounts"] = accounts; save_user_data(qq_id, data)
            await duaa_cmd.finish(f"🗑️ 已成功解绑：{info['real_name']}")
        else: 
            await duaa_cmd.finish("❌ 找不到该 ID")