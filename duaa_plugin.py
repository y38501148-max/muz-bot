import random
from datetime import datetime, timedelta
from nonebot import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent
from nonebot.params import CommandArg
from pathlib import Path

from duaa_core import (
    load_user_data,
    save_user_data,
    perform_duaa_login,
    safe_fetch_schedule,
    safe_execute_sign_in,
    TZ_BEIJING
)

duaa_cmd = on_command("duaa", priority=5, block=True)

@duaa_cmd.handle()
async def handle_duaa(event: MessageEvent, args: Message = CommandArg()):
    sub_cmd = args.extract_plain_text().strip().split()
    if not sub_cmd:
        await duaa_cmd.finish("🚀 Duaa 助手：\n/duaa 绑定 [学号] [自定义ID] [密码]\n/duaa 解绑 [ID]\n/duaa 课表 [ID]\n/duaa 签到 [ID] [序号] [-su]\n/duaa 刷新任务 (重置今日分配)\n/duaa 设签到 [ID] [序号] [HH:MM]\n/duaa 开启自动签到\n/duaa 关闭自动签到")
    
    action, qq_id = sub_cmd[0], str(event.get_user_id())
    data = await load_user_data(qq_id)
    accounts = data.get("accounts", {})

    if action == "开启自动签到":
        group_id = getattr(event, "group_id", None)
        if not group_id: await duaa_cmd.finish("⚠️ 请在你想开启自动签到的【群聊】中使用此指令！私聊无效。")
        data["notify_group"] = group_id
        await save_user_data(qq_id, data)
        await duaa_cmd.send("✅ 自动签到已开启！\n每天 7:00 自动分配课前时间点进行嗅探。")
        return
        
    elif action == "关闭自动签到":
        if "notify_group" in data:
            del data["notify_group"]
            await save_user_data(qq_id, data)
            await duaa_cmd.finish("🛑 自动签到已成功关闭！将不再为你自动执行每日打卡任务。")
        else:
            await duaa_cmd.finish("⚠️ 你当前并未开启自动签到。")

    elif action == "解绑":
        if len(sub_cmd) < 2: await duaa_cmd.finish("用法：/duaa 解绑 [自定义ID]")
        alias = sub_cmd[1]
        if alias in accounts:
            del accounts[alias]
            await save_user_data(qq_id, data)
            await duaa_cmd.finish(f"✅ 已成功解绑账号：{alias}")
        else:
            await duaa_cmd.finish(f"❓ 未找到名为 [{alias}] 的账号。")

    elif action == "刷新任务":
        today_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
        count_all = 0
        for alias, acc in accounts.items():
            try:
                sched, auth_updated = await safe_fetch_schedule(acc, today_str)
                for course in sched:
                    begin_str = course.get("classBeginTime", "")
                    if begin_str:
                        dt = datetime.strptime(begin_str.split(" ")[-1][:5], "%H:%M")
                        course["auto_sign_trigger_hm"] = (dt - timedelta(minutes=random.randint(3, 9))).strftime("%H:%M")
                        course["retries"] = 0
                        count_all += 1
                acc["today_schedule"] = sched
                acc["schedule_date"] = today_str
            except: pass
        await save_user_data(qq_id, data)
        await duaa_cmd.finish(f"✅ 任务刷新完毕，今日共检测到 {count_all} 节课。")

    elif action == "设签到":
        if len(sub_cmd) < 4: await duaa_cmd.finish("用法：/duaa 设签到 [ID] [序号] [HH:MM]")
        alias, idx_str, time_str = sub_cmd[1], sub_cmd[2], sub_cmd[3]
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        try:
            idx = int(idx_str) - 1
            sched = accounts[alias].get("today_schedule", [])
            sched[idx]["auto_sign_trigger_hm"] = time_str
            sched[idx]["retries"] = 0
            await save_user_data(qq_id, data)
            await duaa_cmd.send(f"✅ 已将《{sched[idx]['courseName']}》签到时间设为 {time_str}")
        except: await duaa_cmd.finish("❌ 设定失败，请检查序号和格式。")

    elif action == "绑定":
        if len(sub_cmd) < 4: await duaa_cmd.finish("用法：/duaa 绑定 [学号] [自定义ID] [密码]")
        sid, alias, password = sub_cmd[1], sub_cmd[2], sub_cmd[3]
        try:
            uid, sess, real_name, cookies = await perform_duaa_login(sid, password)
            accounts[alias] = {"student_id": sid, "password": password, "real_name": real_name, "cookies": cookies, "uid": uid, "session_id": sess}
            data["accounts"] = accounts
            await save_user_data(qq_id, data)
            await duaa_cmd.send(f"✅ 绑定成功：{real_name} ({sid})\n你的账号已加入 VPN 号池。")
        except Exception as e: 
            await duaa_cmd.finish(str(e))

    elif action == "课表":
        alias = sub_cmd[1] if len(sub_cmd) > 1 else (list(accounts.keys())[0] if len(accounts) == 1 else None)
        if not alias or alias not in accounts: await duaa_cmd.finish("❓ 未找到该账号")
        acc = accounts[alias]
        try:
            date_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
            sched, auth_updated = await safe_fetch_schedule(acc, date_str)
            
            old_times = {c["id"]: c.get("auto_sign_trigger_hm") for c in acc.get("today_schedule", [])}
            for c in sched: c["auto_sign_trigger_hm"] = old_times.get(c["id"])
            
            acc["today_schedule"] = sched
            acc["schedule_date"] = date_str 
            await save_user_data(qq_id, data)
            
            if not sched: await duaa_cmd.finish(f"📅 {acc['real_name']} 今日无课")
            
            msg = f"📅 {acc['real_name']} 的今日课表:\n"
            for i, c in enumerate(sched, 1):
                status = "✅已签" if str(c.get("signStatus")) == "1" else "⏳未签"
                trig = f" | ⏰打卡: {c.get('auto_sign_trigger_hm')}" if c.get("auto_sign_trigger_hm") and status == "⏳未签" else ""
                room = c.get("roomName") or c.get("classroomName") or "未知"
                msg += f"\n[{i}] 📖 {c['courseName']}\n    📍 {room} | {status}{trig}"
            await duaa_cmd.send(msg)
        except Exception as e: await duaa_cmd.finish(f"❌ 查课表失败: {e}")

    elif action == "签到":
        if len(sub_cmd) < 3: await duaa_cmd.finish("用法：/duaa 签到 [ID] [序号] [-su]")
        alias, idx_str = sub_cmd[1], sub_cmd[2]
        is_su = "-su" in sub_cmd  
        if alias not in accounts: await duaa_cmd.finish("❓ 未找到账号")
        acc = accounts[alias]
        try: idx = int(idx_str) - 1
        except: await duaa_cmd.finish("❌ 序号错误")
            
        sched = acc.get("today_schedule", [])
        if idx < 0 or idx >= len(sched): await duaa_cmd.finish("⚠️ 找不到该课程")
        target = sched[idx]

        if not is_su:
            now = datetime.now(TZ_BEIJING)
            try:
                t = datetime.strptime(target.get("classBeginTime", "").split(" ")[-1][:5], "%H:%M")
                if now < now.replace(hour=t.hour, minute=t.minute) - timedelta(minutes=15):
                    await duaa_cmd.finish("⚠️ 尚未开放签到。使用 -su 强制签到。")
            except: pass

        fake_time = target.get("classBeginTime") if is_su else None

        try:
            res_data, auth_updated = await safe_execute_sign_in(acc, target["id"], fake_time)
            if (str(res_data.get("STATUS")) == "0" and str(res_data.get("result", {}).get("stuSignStatus")) == "1"):
                target["signStatus"] = "1"
                await save_user_data(qq_id, data)
                await duaa_cmd.send(f"🎯 《{target['courseName']}》签到成功！")
            else:
                if auth_updated: await save_user_data(qq_id, data)
                await duaa_cmd.send(f"❌ 签到失败：{res_data.get('ERRMSG', '未知错误')}")
        except Exception as e: await duaa_cmd.finish(f"❌ 执行错误: {e}")

# FastAPI Endpoint for Daemon
import nonebot
from fastapi import Body

try:
    app = nonebot.get_app()
    
    @app.post("/duaa/notify")
    async def receive_duaa_notify(payload: dict = Body(...)):
        group_id = payload.get("group_id")
        message = payload.get("message")
        bots = nonebot.get_bots()
        if bots and group_id and message:
            bot = list(bots.values())[0]
            try:
                await bot.send_group_msg(group_id=int(group_id), message=message)
                return {"status": "success"}
            except Exception as e:
                return {"status": "error", "error": str(e)}
        return {"status": "skipped"}
except Exception as e:
    # Fallback if fastAPI app is not available
    pass