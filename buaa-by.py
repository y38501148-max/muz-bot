import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Any

from nonebot import on_command, get_bot, require
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent, Bot
from nonebot.params import CommandArg
from nonebot.log import logger

# 引入定时任务插件
require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from boya_utils import BoyaClient

# ==================== 路径与路径配置 ====================
BASE_DIR = Path(__file__).parent / "data" / "boya"
CONFIG_PATH = BASE_DIR / "by.txt"
REMINDER_PATH = BASE_DIR / "reminders.json"
BASE_DIR.mkdir(parents=True, exist_ok=True)

TZ_BEIJING = timezone(timedelta(hours=8))

# ==================== 数据管理工具 ====================
def get_credentials():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text("学号:密码", encoding="utf-8")
        return None, None
    try:
        content = CONFIG_PATH.read_text(encoding="utf-8").strip()
        if ":" in content:
            sid, pwd = content.split(":", 1)
            return sid.strip(), pwd.strip()
    except: pass
    return None, None

def load_reminders() -> Dict:
    if not REMINDER_PATH.exists():
        return {"monitored": {}, "last_results": []}
    try:
        return json.loads(REMINDER_PATH.read_text(encoding="utf-8"))
    except:
        return {"monitored": {}, "last_results": []}

def save_reminders(data: Dict):
    REMINDER_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

# ==================== 后台定时任务逻辑 ====================

async def update_boya_data():
    """后台探测任务：拉取最新列表，同步到监控库"""
    sid, pwd = get_credentials()
    if not sid or not pwd or sid == "学号": return

    client = BoyaClient(sid, pwd)
    res = await client.get_course_list()
    if not res or res.get("status") != "0": return

    courses = res.get("data", {}).get("content", [])
    data = load_reminders()
    monitored = data["monitored"]
    
    now = datetime.now(TZ_BEIJING).replace(tzinfo=None)
    fmt = "%Y-%m-%d %H:%M:%S"

    for c in courses:
        cid = str(c['id'])
        place = c.get("coursePosition") or c.get("roomName") or ""
        if "沙河" in place: continue # 屏蔽沙河

        try:
            start_dt = datetime.strptime(c['courseSelectStartDate'], fmt)
            if start_dt > now: # 只记录还没开始选课的
                if cid not in monitored:
                    monitored[cid] = {
                        "name": c['courseName'],
                        "start_time": c['courseSelectStartDate'],
                        "notified": False,
                        "subscribers": []
                    }
                else:
                    monitored[cid]["start_time"] = c['courseSelectStartDate']
        except: continue
    
    data["monitored"] = monitored
    save_reminders(data)

@scheduler.scheduled_job("cron", minute="*/10", id="boya_fetch_job")
async def boya_fetch_job():
    await update_boya_data()

@scheduler.scheduled_job("cron", minute="*", id="boya_alert_job")
async def boya_alert_job():
    """提醒任务：每分钟扫描，倒计时3分钟时 @ 提醒"""
    data = load_reminders()
    monitored = data.get("monitored", {})
    if not monitored: return

    now = datetime.now(TZ_BEIJING).replace(tzinfo=None)
    fmt = "%Y-%m-%d %H:%M:%S"
    need_save = False
    
    try:
        bot: Bot = get_bot()
    except: return

    for cid, info in monitored.items():
        if info["notified"]: continue
        try:
            start_t = datetime.strptime(info["start_time"], fmt)
            diff = (start_t - now).total_seconds()
            if 0 < diff <= 185: 
                subscribers = info.get("subscribers", [])
                if not subscribers: continue
                group_msgs = {}
                for sub in subscribers:
                    gid = sub.get("group_id")
                    if gid: group_msgs.setdefault(gid, []).append(sub["user_id"])
                for gid, uids in group_msgs.items():
                    mentions = "".join([f"[CQ:at,qq={uid}] " for uid in uids])
                    msg = f"🔔 博雅抢课提醒！\n\n课程：{info['name']}\n时间：{info['start_time']}\n即将于 3 分钟内开启选课！\n\n{mentions}"
                    await bot.send_group_msg(group_id=int(gid), message=Message(msg))
                info["notified"] = True
                need_save = True
        except: pass
    if need_save: save_reminders(data)

# ==================== 指令处理器 ====================
by_cmd = on_command("by", priority=5, block=True)

@by_cmd.handle()
async def handle_boya(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    action = sub_cmd[0] if sub_cmd else "列表"
    qq_id = str(event.get_user_id())

    if action == "列表":
        sid, pwd = get_credentials()
        if not sid or not pwd or sid == "学号":
            await by_cmd.finish(f"❌ 未检测到博雅配置。请填写 {CONFIG_PATH}")

        client = BoyaClient(sid, pwd)
        res = await client.get_course_list()
        if not res or res.get("status") != "0":
            await by_cmd.finish("❌ 获取失败，请确认服务器网络通畅。")

        courses = res.get("data", {}).get("content", [])
        now = datetime.now(TZ_BEIJING).replace(tzinfo=None)
        fmt = "%Y-%m-%d %H:%M:%S"
        
        selectable, upcoming = [], []
        for c in courses:
            try:
                place = c.get("coursePosition") or c.get("roomName") or ""
                if "沙河" in place: continue
                s_start = datetime.strptime(c['courseSelectStartDate'], fmt)
                c_start = datetime.strptime(c['courseStartDate'], fmt)
                if c_start < now: continue 
                if s_start > now: upcoming.append(c)
                elif c.get("courseCurrentCount", 0) < c.get("courseMaxCount", 0):
                    selectable.append(c)
            except: continue

        data = load_reminders()
        data["last_results"] = upcoming 
        save_reminders(data)

        msg = f"📊 北航博雅报告 (已过滤沙河)\n"
        if selectable:
            msg += "\n✨ 【有余位，速抢】"
            for c in selectable:
                left = c['courseMaxCount'] - c['courseCurrentCount']
                kind = c.get("courseNewKind2", {}).get("kindName", "未知")
                s_start = c['courseSelectStartDate'][5:16]
                c_start = c['courseStartDate'][5:16]
                msg += f"\n- {c['courseName']}\n  🔥 剩余:{left} | 类别:{kind}\n  🚀 选课:{s_start} | ⏰ 上课:{c_start}\n  📍 地点:{c.get('coursePosition') or '待定'}"
        
        if upcoming:
            msg += "\n\n🚀 【选课预告】(输入 /by 标记 [序号] 订阅)"
            for i, c in enumerate(upcoming, 1):
                kind = c.get("courseNewKind2", {}).get("kindName", "未知")
                s_start = c['courseSelectStartDate'][5:16]
                c_start = c['courseStartDate'][5:16]
                msg += f"\n[{i}] {c['courseName']}\n  📌 类别:{kind}\n  🚀 选课:{s_start} | ⏰ 上课:{c_start}\n  📍 地点:{c.get('coursePosition') or '待定'}"
        
        if not selectable and not upcoming:
            msg += "\n📅 当前没有合适的博雅课程 (均已开课或无余位)"
        await by_cmd.finish(msg)

    elif action == "标记":
        if not isinstance(event, GroupMessageEvent):
            await by_cmd.finish("❌ 标记功能仅限群聊使用。")
        if len(sub_cmd) < 2: await by_cmd.finish("请输入：/by 标记 [序号]")
        try: idx = int(sub_cmd[1]) - 1
        except: await by_cmd.finish("序号无效")
        data = load_reminders()
        last_results = data.get("last_results", [])
        if not last_results or idx < 0 or idx >= len(last_results):
            await by_cmd.finish("请先发送 [/by] 刷新列表。信号。")
        target = last_results[idx]
        cid, group_id = str(target['id']), str(event.group_id)
        monitored = data.setdefault("monitored", {})
        if cid not in monitored:
            monitored[cid] = {"name": target['courseName'], "start_time": target['courseSelectStartDate'], "notified": False, "subscribers": []}
        subs = monitored[cid]["subscribers"]
        if not any(s['user_id'] == qq_id and s['group_id'] == group_id for s in subs):
            subs.append({"user_id": qq_id, "group_id": group_id})
            save_reminders(data)
            await by_cmd.finish(f"✅ 标记成功！\n课程：{target['courseName']}\n将在选课前 3 分钟在群内 @ 你。")
        else:
            await by_cmd.finish("已经标记过啦！")
    elif action == "重置":
        save_reminders({"monitored": {}, "last_results": []})
        await by_cmd.finish("🗑️ 数据库已清空")
    else:
        await by_cmd.finish("❓ 未知指令，直接发送 /by 即可。")
