from pathlib import Path
from datetime import datetime
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from boya_utils import BoyaClient

# 配置路径
CONFIG_PATH = Path("data/boya/by.txt")

def get_credentials():
    """从本地文件读取账号密码"""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        # 如果文件不存在，可以先创建一个空的示例
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

by_cmd = on_command("by", priority=5, block=True)

@by_cmd.handle()
async def handle_boya(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    action = sub_cmd[0] if sub_cmd else "列表"

    if action == "列表":
        # 1. 获取账号密码
        sid, pwd = get_credentials()
        if not sid or not pwd or sid == "学号":
            await by_cmd.finish(f"❌ 未检测到配置。请在项目根目录的 {CONFIG_PATH} 中写入 [学号:密码] 后重试。")

        # 2. 初始化并拉取
        client = BoyaClient(sid, pwd)
        data = await client.get_course_list()
        
        if not data or data.get("status") != "0":
            await by_cmd.finish("❌ 博雅数据获取失败，请确认 by.txt 中的账号密码是否正确。")

        courses = data.get("data", {}).get("content", [])
        now = datetime.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        
        selectable_courses = []
        upcoming_courses = []

        for c in courses:
            try:
                sel_start_dt = datetime.strptime(c['courseSelectStartDate'], fmt)
                sel_end_dt = datetime.strptime(c['courseSelectEndDate'], fmt)
                current, total = c.get("courseCurrentCount", 0), c.get("courseMaxCount", 0)

                if sel_start_dt > now:
                    upcoming_courses.append(c)
                elif sel_start_dt <= now <= sel_end_dt and current < total:
                    selectable_courses.append(c)
            except: continue

        # 3. 构造消息
        msg = f"📊 全校博雅实时监测报告\n"
        if selectable_courses:
            msg += "\n✨ 【当前有余位，建议抢课】"
            for c in selectable_courses:
                left = c['courseMaxCount'] - c['courseCurrentCount']
                msg += f"\n- {c['courseName']}\n  🔥 剩余:{left}位 | 教室:{c.get('coursePosition') or '待定'}"
        
        if upcoming_courses:
            msg += "\n\n🚀 【即将选课预告】"
            for c in upcoming_courses:
                msg += f"\n- {c['courseName']}\n  ⏳ 开启:{c['courseSelectStartDate'][5:16]}"
        
        if not selectable_courses and not upcoming_courses:
            msg += "\n📅 暂无推荐课程 (请稍后再试)"
            
        await by_cmd.finish(msg)
    else:
        await by_cmd.finish("❓ 未知指令。直接输入 /by 即可查询博雅列表。")
