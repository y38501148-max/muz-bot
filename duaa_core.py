import httpx
import json
import re
import random
import asyncio
import fcntl
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DATA_DIR = Path("data/duaa")
USER_DIR = BASE_DATA_DIR / "users"
USER_DIR.mkdir(parents=True, exist_ok=True)

TZ_BEIJING = timezone(timedelta(hours=8))

UA = "Mozilla/5.0 (Linux; Android 13; M2012K11AC Build/TKQ1.220829.002; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 wxwork/4.1.22 MicroMessenger/7.0.1 NetType/WIFI Language/zh ColorScheme/Light"
VPN_SERVICE_ID = "77726476706e69737468656265737421f9f44d9d342326526b0988e29d51367ba018"

def get_network_urls(use_vpn):
    if use_vpn:
        base_8347 = f"https://d.buaa.edu.cn/https-8347/{VPN_SERVICE_ID}"
        base_8081 = f"https://d.buaa.edu.cn/http-8081/{VPN_SERVICE_ID}" 
        return {
            "login": f"{base_8347}/app/user/login.action",
            "schedule": f"{base_8347}/app/course/get_stu_course_sched.action",
            "timestamp": f"{base_8081}/app/common/get_timestamp.action",
            "sign": f"{base_8081}/app/course/stu_scan_sign.action"
        }
    else:
        return {
            "login": "https://iclass.buaa.edu.cn:8347/app/user/login.action",
            "schedule": "https://iclass.buaa.edu.cn:8347/app/course/get_stu_course_sched.action",
            "timestamp": "http://iclass.buaa.edu.cn:8081/app/common/get_timestamp.action",
            "sign": "http://iclass.buaa.edu.cn:8081/app/course/stu_scan_sign.action"
        }

def _locked_read(file_path: Path):
    if not file_path.exists():
        return {"accounts": {}}
    with open(file_path, "r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {"accounts": {}}
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

def _locked_write(file_path: Path, data: dict):
    with open(file_path, "w", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(data, f, ensure_ascii=False, indent=4)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

async def load_user_data(qq_id):
    file_path = USER_DIR / f"{qq_id}.json"
    data = await asyncio.to_thread(_locked_read, file_path)
    if "student_id" in data and "accounts" not in data:
        old_sid = data.pop("student_id")
        old_name = data.pop("real_name", "本人")
        data["accounts"] = {old_name: {"student_id": old_sid, "real_name": old_name}}
        await save_user_data(qq_id, data)
    return data

async def save_user_data(qq_id, data):
    file_path = USER_DIR / f"{qq_id}.json"
    await asyncio.to_thread(_locked_write, file_path, data)

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
    use_vpn = bool(personal_password)
    urls = get_network_urls(use_vpn)
    
    async with httpx.AsyncClient(verify=False, follow_redirects=True, headers={"User-Agent": UA}) as client:
        if use_vpn:
            await sso_login(client, target_student_id, personal_password)
            
        try:
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
            
            if str(json_data.get("STATUS")) == "0":
                results = json_data.get("result", {})
                return results.get("id"), results.get("sessionId"), results.get("userName", "未知姓名"), dict(client.cookies)
            else:
                raise Exception(json_data.get("ERRMSG", "登录鉴权失败"))
        except Exception as e:
            raise Exception(f"教务登录接口请求失败: {e}")

async def fetch_server_timestamp(use_vpn, cookies, fake_time_str=None):
    if fake_time_str:
        try:
            dt = datetime.strptime(fake_time_str, "%Y-%m-%d %H:%M:%S")
            fake_ts = dt - timedelta(minutes=5)
            return int(fake_ts.timestamp() * 1000)
        except Exception:
            pass

    urls = get_network_urls(use_vpn)
    async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
        res = await client.get(urls["timestamp"], headers={"User-Agent": UA}, timeout=10)
        res.raise_for_status()
        return res.json().get("timestamp")

async def execute_sign_in(use_vpn, session_id, cookies, uid, course_sched_id, fake_time_str=None):
    urls = get_network_urls(use_vpn)
    server_ts = await fetch_server_timestamp(use_vpn, cookies, fake_time_str)
    if not server_ts: raise Exception("获取服务器时间戳失败")

    async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
        res = await client.post(
            urls["sign"],
            params={"courseSchedId": course_sched_id, "timestamp": str(server_ts)},
            data={"id": uid},
            timeout=10
        )
        res.raise_for_status()
        return res.json()

async def safe_fetch_schedule(acc, today_str):
    has_vpn = bool(acc.get('password'))
    uid, sess, cookies = acc.get('uid'), acc.get('session_id'), acc.get('cookies')
    urls = get_network_urls(has_vpn)
    auth_updated = False

    async def _fetch():
        async with httpx.AsyncClient(verify=False, cookies=cookies or {}) as client:
            res = await client.get(urls["schedule"], params={"id": uid, "dateStr": today_str}, headers={"Sessionid": sess, "User-Agent": UA})
            res.raise_for_status()
            data = res.json()
            if str(data.get("STATUS")) != "0": raise ValueError(data.get("ERRMSG", "Error"))
            return data.get("result", [])

    if not uid or not sess:
        uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
        acc.update({"uid": uid, "session_id": sess, "cookies": cookies})
        auth_updated = True

    try:
        sched = await _fetch()
        return sched, auth_updated
    except Exception:
        uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
        acc.update({"uid": uid, "session_id": sess, "cookies": cookies})
        sched = await _fetch()
        return sched, True

async def safe_execute_sign_in(acc, course_id, fake_time_str=None):
    has_vpn = bool(acc.get('password'))
    uid, sess, cookies = acc.get('uid'), acc.get('session_id'), acc.get('cookies')
    auth_updated = False
    
    if not uid or not sess:
        uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
        acc.update({"uid": uid, "session_id": sess, "cookies": cookies})
        auth_updated = True
        
    try:
        res_data = await execute_sign_in(has_vpn, sess, cookies, uid, course_id, fake_time_str)
        if str(res_data.get("STATUS")) != "0" and ("登录" in res_data.get("ERRMSG", "") or "session" in res_data.get("ERRMSG", "").lower()):
            raise ValueError("Session expired")
        return res_data, auth_updated
    except Exception:
        uid, sess, _, cookies = await perform_duaa_login(acc['student_id'], acc.get('password'))
        acc.update({"uid": uid, "session_id": sess, "cookies": cookies})
        res_data = await execute_sign_in(has_vpn, sess, cookies, uid, course_id, fake_time_str)
        return res_data, True
