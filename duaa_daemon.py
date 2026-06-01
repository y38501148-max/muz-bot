import random
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import httpx

from duaa_core import (
    USER_DIR,
    load_user_data,
    save_user_data,
    safe_fetch_schedule,
    safe_execute_sign_in,
    TZ_BEIJING
)

# HTTP endpoint of the NoneBot (to forward messages)
BOT_API_URL = "http://127.0.0.1:8080/duaa/notify"

async def send_notify(group_id, message, qq_id=None):
    if qq_id:
        message = f"[CQ:at,qq={qq_id}] " + message
    try:
        async with httpx.AsyncClient() as client:
            await client.post(BOT_API_URL, json={"group_id": group_id, "message": message}, timeout=5)
    except Exception as e:
        print(f"Failed to send notify to bot: {e}")

async def midnight_sleep_reminder():
    """每天半夜12点提醒全体成员睡觉"""
    groups = set()
    for f in USER_DIR.glob("*.json"):
        try:
            data = await load_user_data(f.stem)
            if data.get("notify_group"):
                groups.add(data.get("notify_group"))
        except Exception: pass
            
    for gid in groups:
        await send_notify(gid, "🌙 滴滴！现在是半夜12点。宝宝们快去睡觉吧！晚安~", "all")

async def daily_sync():
    """早上7点自动同步并发送通知"""
    today_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
    for file in USER_DIR.glob("*.json"):
        try:
            qq_id = file.stem
            data = await load_user_data(qq_id)
            group_id = data.get("notify_group")
            if not group_id: continue
            
            changed, count = False, 0
            for alias, acc in data.get("accounts", {}).items():
                try:
                    sched, auth_updated = await safe_fetch_schedule(acc, today_str)
                    if auth_updated: changed = True
                    
                    for course in sched:
                        t_str = course.get("classBeginTime", "")
                        if t_str:
                            dt = datetime.strptime(t_str.split(" ")[-1][:5], "%H:%M")
                            course["auto_sign_trigger_hm"] = (dt - timedelta(minutes=random.randint(3, 9))).strftime("%H:%M")
                            course["retries"] = 0
                            count += 1
                    acc["today_schedule"] = sched
                    acc["schedule_date"] = today_str
                    changed = True
                except Exception as e:
                    print(f"Daily sync failed for {qq_id}/{alias}: {e}")
            
            if changed:
                await save_user_data(qq_id, data)
                if count > 0:
                    await send_notify(group_id, f"🌅 早上好！今日检测到 {count} 节课，已为你安排好自动打卡。", qq_id)
        except Exception as e:
            print(f"Daily sync error for {file}: {e}")

async def auto_checkin_executor():
    """每分钟轮询自动打卡"""
    now_hm = datetime.now(TZ_BEIJING).strftime("%H:%M")
    today_str = datetime.now(TZ_BEIJING).strftime("%Y%m%d")
    
    for file in USER_DIR.glob("*.json"):
        try:
            qq_id = file.stem
            data = await load_user_data(qq_id)
            group_id = data.get("notify_group")
            if not group_id: continue
            
            changed = False
            for alias, acc in data.get("accounts", {}).items():
                if acc.get("schedule_date") != today_str: continue
                
                for course in acc.get("today_schedule", []):
                    trig = course.get("auto_sign_trigger_hm")
                    if (
                        course.get("retries", 0) >= 30
                        and str(course.get("signStatus")) != "1"
                        and not course.get("terminal_reason")
                        and not course.get("auth_recovery_done")
                    ):
                        course["retries"] = 0
                        course["auth_recovery_done"] = True
                        changed = True

                    if trig and now_hm >= trig and str(course.get("signStatus")) != "1" and course.get("retries", 0) < 30:
                        is_first_try = course.get("retries", 0) == 0
                        course["retries"] = course.get("retries", 0) + 1
                        changed = True
                        try:
                            res, auth_updated = await safe_execute_sign_in(acc, course["id"], force_refresh=is_first_try)
                            suc = (str(res.get("STATUS")) == "0" and str(res.get("result", {}).get("stuSignStatus")) == "1")
                            msg_err = res.get('ERRMSG', '')
                            
                            if suc or "已签到" in msg_err:
                                course["signStatus"] = "1"
                                await send_notify(group_id, f"🤖 自动签到成功！\n账号：[{alias}]\n课程：《{course.get('courseName')}》", qq_id)
                            elif "用户不存在" in msg_err or "账号" in msg_err or "登录" in msg_err or "session" in msg_err.lower():
                                print(f"Auth-like sign in failure for {qq_id}/{alias} - {course.get('courseName')}: {msg_err}")
                            elif "结束" in msg_err or ("课程" in msg_err and "不存在" in msg_err):
                                course["retries"] = 99
                                course["terminal_reason"] = "course_closed_or_missing"
                        except Exception as e:
                            print(f"Sign in error for {alias} - {course.get('courseName')}: {e}")
            
            if changed:
                await save_user_data(qq_id, data)
        except Exception as e:
            print(f"Auto checkin error for {file}: {e}")

async def main():
    scheduler = AsyncIOScheduler(timezone=TZ_BEIJING)
    scheduler.add_job(midnight_sleep_reminder, "cron", hour=0, minute=0, id="duaa_midnight")
    scheduler.add_job(daily_sync, "cron", hour=7, minute=0, id="duaa_daily_sync")
    scheduler.add_job(auto_checkin_executor, "cron", minute="*", id="duaa_auto_checkin_executor")
    
    scheduler.start()
    print("Duaa Daemon started. Running scheduled tasks independently...")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass

if __name__ == "__main__":
    asyncio.run(main())
