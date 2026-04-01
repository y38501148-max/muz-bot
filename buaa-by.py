from pathlib import Path
from datetime import datetime
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from .boya_utils import BoyaClient  # 确保 boya_utils.py 在同一目录下

# ==================== 预设账号配置 ====================
DEFAULT_SID = ""
DEFAULT_PWD = ""
# ====================================================

by_cmd = on_command("by", priority=5, block=True)

@by_cmd.handle()
async def handle_boya(event: MessageEvent, args: Message = CommandArg()):
    # 解析指令 (其实现在只需要输入 /by 即可直接触发列表)
    sub_cmd = args.extract_plain_text().strip().split()
    
    # 默认直接执行列表拉取
    action = sub_cmd[0] if sub_cmd else "列表"

    if action == "列表":
        # 1. 初始化客户端
        client = BoyaClient(DEFAULT_SID, DEFAULT_PWD)
        
        # 2. 拉取数据 (内部会自动尝试 SSO 登录)
        data = await client.get_course_list()
        
        if not data or data.get("status") != "0":
            await by_cmd.finish("❌ 博雅数据获取失败，通常是 SSO 登录验证码或账号异常。")

        courses = data.get("data", {}).get("content", [])
        now = datetime.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        
        selectable_courses = []  # 分类1：有余位
        upcoming_courses = []    # 分类2：即将开始

        for c in courses:
            try:
                sel_start_str = c.get("courseSelectStartDate")
                sel_end_str = c.get("courseSelectEndDate")
                if not sel_start_str or not sel_end_str: continue
                
                sel_start_dt = datetime.strptime(sel_start_str, fmt)
                sel_end_dt = datetime.strptime(sel_end_str, fmt)
                
                current = c.get("courseCurrentCount", 0)
                total = c.get("courseMaxCount", 0)
                has_slots = current < total

                if sel_start_dt > now:
                    upcoming_courses.append(c)
                elif sel_start_dt <= now <= sel_end_dt and has_slots:
                    selectable_courses.append(c)
            except:
                continue

        # 3. 构造 QQ 消息
        msg = f"📊 全校博雅扫描报告 (实时监测中)\n"
        
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
