import json
from pathlib import Path
from datetime import datetime
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from boya_utils import BoyaClient  # 确保与 boya_utils.py 在同一目录下

# ==================== 配置强制绝对路径 ====================
# 无论是在本地还是云端运行，都会锁定在插件所在目录下的 data/boya/by.txt
CONFIG_PATH = Path(__file__).parent / "data" / "boya" / "by.txt"

def get_credentials():
    """从本地文件读取账号密码 [学号:密码]"""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text("学号:密码", encoding="utf-8")
        return None, None
    
    try:
        content = CONFIG_PATH.read_text(encoding="utf-8").strip()
        if ":" in content:
            sid, pwd = content.split(":", 1)
            return sid.strip(), pwd.strip()
    except:
        pass
    return None, None

# 定义指令
by_cmd = on_command("by", priority=5, block=True)

@by_cmd.handle()
async def handle_boya(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    action = sub_cmd[0] if sub_cmd else "列表"

    if action == "列表":
        # 1. 读取云端配置
        sid, pwd = get_credentials()
        if not sid or not pwd or sid == "学号":
            await by_cmd.finish(f"❌ 未检测到博雅配置。\n请在云端服务器的 {CONFIG_PATH} 中填入 [学号:密码] 后重启。")

        # 2. 模拟登录并拉取
        client = BoyaClient(sid, pwd)
        data = await client.get_course_list()
        
        if not data or data.get("status") != "0":
            await by_cmd.finish("❌ 博雅获取失败，请确认 by.txt 里的账号密码并确认校网通畅。")

        # 3. 核心解析与过滤
        courses = data.get("data", {}).get("content", [])
        now = datetime.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        
        selectable_courses = []  # 情况1：捡漏中
        upcoming_courses = []    # 情况2：预告中

        for c in courses:
            try:
                # 转换各阶段时刻
                sel_start_dt = datetime.strptime(c['courseSelectStartDate'], fmt)
                sel_end_dt = datetime.strptime(c['courseSelectEndDate'], fmt)
                course_start_dt = datetime.strptime(c['courseStartDate'], fmt)
                
                # 提取状态
                current = c.get("courseCurrentCount", 0)
                total = c.get("courseMaxCount", 0)
                
                # --- 过滤器 ---
                # A. 绝对过滤：如果课程已经开始上课，直接跳过
                if course_start_dt < now:
                    continue
                
                # B. 情况一：选课还没开始的 (预告)
                if sel_start_dt > now:
                    upcoming_courses.append(c)
                
                # C. 情况二：选课进行中 + 有余位 + 还在选课期 (捡漏位)
                elif sel_start_dt <= now <= sel_end_dt and current < total:
                    selectable_courses.append(c)
            except:
                continue

        # 4. 构造消息推送
        msg = f"📊 北航博雅·实时动态报告 (扫描前50门)\n"
        
        if selectable_courses:
            msg += "\n✨ 【当前有余位，建议速抢】"
            for c in selectable_courses:
                left = c['courseMaxCount'] - c['courseCurrentCount']
                kind = c.get("courseNewKind2", {}).get("kindName", "互动")
                start = c.get("courseStartDate", "待定")[5:16] # 04-01 19:00
                msg += f"\n- {c['courseName']}\n  🔥 剩余:{left} | 类别:{kind} | ⏰:{start}\n  📍 教室:{c.get('coursePosition') or '待定'}"
        
        if upcoming_courses:
            msg += "\n\n🚀 【即将开启选课预告】"
            for c in upcoming_courses:
                kind = c.get("courseNewKind2", {}).get("kindName", "互动")
                msg += f"\n- {c['courseName']}\n  ⏳ 开启:{c['courseSelectStartDate'][5:16]} | 类别:{kind}"
        
        if not selectable_courses and not upcoming_courses:
            msg += "\n📅 暂无推荐课程 (请过后再来)"
            
        await by_cmd.finish(msg)
    else:
        await by_cmd.finish("❓ 未知指令，直接发送 /by 即可查询。")
