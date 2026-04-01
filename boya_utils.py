#!/usr/bin/env python3
import base64
import json
import time
import random
import string
import hashlib
import re
import httpx
import asyncio
from datetime import datetime
from Crypto.PublicKey import RSA
from Crypto.Cipher import AES, PKCS1_v1_5

# ==================== 核心加密库 ====================
BOYA_RSA_KEY = """-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDlHMQ3B5GsWnCe7Nlo1YiG/YmH
dlOiKOST5aRm4iaqYSvhvWmwcigoyWTM+8bv2+sf6nQBRDWTY4KmNV7DBk1eDnTI
Qo6ENA31k5/tYCLEXgjPbEjCK9spiyB62fCT6cqOhbamJB0lcDJRO6Vo1m3dy+fD
0jbxfDVBBNtyltIsDQIDAQAB
-----END PUBLIC KEY-----"""

class BoyaCrypto:
    def __init__(self):
        self.rsa_key = RSA.import_key(BOYA_RSA_KEY)
        self.rsa_cipher = PKCS1_v1_5.new(self.rsa_key)

    def _gen_rand_str(self, length=16):
        return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

    def encrypt_request(self, payload_dict):
        """返回 (body, headers, aes_key)"""
        aes_key = self._gen_rand_str(16).encode()
        ak = base64.b64encode(self.rsa_cipher.encrypt(aes_key)).decode()
        data_bytes = json.dumps(payload_dict, separators=(',', ':')).encode('utf-8')
        sha1_hash = hashlib.sha1(data_bytes).hexdigest().encode()
        sk = base64.b64encode(self.rsa_cipher.encrypt(sha1_hash)).decode()
        
        def pad(s):
            return s + (16 - len(s) % 16) * bytes([16 - len(s) % 16])
        
        aes_cipher = AES.new(aes_key, AES.MODE_ECB)
        body = base64.b64encode(aes_cipher.encrypt(pad(data_bytes))).decode()
        ts = str(int(time.time() * 1000))
        
        return body, {"Ak": ak, "Sk": sk, "Ts": ts}, aes_key

    def decrypt_response(self, encrypted_content, aes_key):
        raw_b64 = encrypted_content.strip(b'"')
        encrypted_data = base64.b64decode(raw_b64)
        aes_cipher = AES.new(aes_key, AES.MODE_ECB)
        decrypted_data = aes_cipher.decrypt(encrypted_data)
        padding_len = decrypted_data[-1]
        return decrypted_data[:-padding_len].decode('utf-8')

# ==================== 博雅客户端 ====================
class BoyaClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(follow_redirects=True, verify=False, timeout=20)
        self.crypto = BoyaCrypto()
        self.token = None

    async def login(self):
        print("正在获取 SSO 令牌...")
        res = await self.client.get("https://sso.buaa.edu.cn/login")
        execution = re.search(r'name="execution" value="(.+?)"', res.text).group(1)
        
        form = {
            "username": self.username, "password": self.password,
            "submit": "登录", "type": "username_password",
            "execution": execution, "_eventId": "submit"
        }
        await self.client.post("https://sso.buaa.edu.cn/login", data=form)

        boya_login_url = "https://sso.buaa.edu.cn/login?noAutoRedirect=true&service=https%3A%2F%2Fbykc.buaa.edu.cn%2Fsscv%2Fcas%2Flogin"
        res = await self.client.get(boya_login_url)
        if "token=" in str(res.url):
            self.token = str(res.url).split("token=")[1]
            return True
        return False

    async def get_course_list(self):
        if not self.token:
            if not await self.login(): return None
        
        url = "https://bykc.buaa.edu.cn/sscv/queryStudentSemesterCourseByPage"
        payload = {"pageNumber": 1, "pageSize": 50}  # 获取前50条
        
        body, extra_headers, aes_key = self.crypto.encrypt_request(payload)
        headers = {"Authtoken": self.token, **extra_headers}
        
        res = await self.client.post(url, json=body, headers=headers)
        
        try:
            raw_text = self.crypto.decrypt_response(res.content, aes_key)
            return json.loads(raw_text)
        except:
            return None

# ==================== 运行测试 ====================
async def main():
    STU_ID = input("学号: ").strip()
    PASSWORD = input("密码: ").strip()
    if not STU_ID or not PASSWORD: return

    client = BoyaClient(STU_ID, PASSWORD)
    data = await client.get_course_list()
    
    if data and data.get("status") == "0":
        courses = data.get("data", {}).get("content", [])
        now = datetime.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        
        selectable_courses = []  # 情况1：选课中且有余位
        upcoming_courses = []    # 情况2：选课还未开始

        for c in courses:
            # 1. 转换相关时间
            sel_start_str = c.get("courseSelectStartDate")
            sel_end_str = c.get("courseSelectEndDate")
            if not sel_start_str or not sel_end_str: continue
            
            sel_start_dt = datetime.strptime(sel_start_str, fmt)
            sel_end_dt = datetime.strptime(sel_end_str, fmt)
            
            # 2. 提取余位信息
            current = c.get("courseCurrentCount", 0)
            total = c.get("courseMaxCount", 0)
            has_slots = current < total

            # 3. 分类判定
            if sel_start_dt > now:
                upcoming_courses.append(c)
            elif sel_start_dt <= now <= sel_end_dt and has_slots:
                selectable_courses.append(c)

        print(f"\n✅ 成功扫描 {len(courses)} 门全校博雅课程记录:")

        if selectable_courses:
            print(f"\n✨ 【捡漏提醒】发现 {len(selectable_courses)} 门【有余位】且【选课中】的课程：")
            for c in selectable_courses:
                left = c['courseMaxCount'] - c['courseCurrentCount']
                print(f" - 【{c['courseName']}】")
                print(f"   🚩 状态:剩余 {left} 位 | 教室:{c.get('coursePosition')} | 截止时刻:{c['courseSelectEndDate'][5:16]}")
        else:
            print("\n📅 目前没有正在进行中的余位课程。")

        if upcoming_courses:
            print(f"\n🚀 【选课预告】发现 {len(upcoming_courses)} 门【还未开始】的课程：")
            for c in upcoming_courses:
                print(f" - 【{c['courseName']}】")
                print(f"   ⏳ 开启时刻: {c['courseSelectStartDate'][5:16]} | 总名额: {c['courseMaxCount']}")
    else:
        print(f"❌ 拉取数据失败")

if __name__ == "__main__":
    asyncio.run(main())
