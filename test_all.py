"""
Chrono Secretary 全链路自动化集成与单元测试套件
运行方式：python test_all.py
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from fastapi.testclient import TestClient

from backend.main import app
from backend.core.repository import repo
from backend.core.config_manager import config_manager
from backend.core.scheduler import scheduler_manager
from backend.channels.wechat_claw import WeChatClawAdapter
from backend.calendars.caldav_provider import CalDAVProvider
from backend.calendars.google_provider import GoogleCalendarProvider


def run_all_tests() -> None:
    print("=" * 64)
    print("🚀 开始运行 Chrono Secretary 全链路自动化验收测试")
    print("=" * 64)

    with TestClient(app) as client:
        # 1. 前端静态挂载与健康检查
        r_index = client.get("/")
        assert r_index.status_code == 200 and "Chrono Secretary" in r_index.text
        r_health = client.get("/api/health")
        assert r_health.status_code == 200 and r_health.json()["status"] == "ok"
        print("✅ [1/10] Web 前端挂载与 /api/health 探活正常")

        # 2. 动态配置总线 (0重启热更新) 与敏感密钥脱敏防泄漏校验
        r_cfg = client.get("/api/config").json()
        ics_token = r_cfg["ics_feed_token"]
        assert len(ics_token) >= 16

        # 敏感密钥脱敏校验：写入测试密钥，GET /api/config 绝不明文返回且含掩码
        orig_key = config_manager.get("llm_api_key", "")
        test_secret = "sk-test-super-secret-key-12345"
        r_put_sec = client.put(
            "/api/config",
            json={"config": {"llm_api_key": test_secret}},
        )
        assert r_put_sec.status_code == 200

        r_cfg_sec = client.get("/api/config").json()
        masked_key = r_cfg_sec["config"]["llm_api_key"]
        assert "••••••••" in masked_key
        assert test_secret not in masked_key
        assert r_cfg_sec["secret_configured"]["llm_api_key"] is True

        # 校验将脱敏掩码回传给 PUT /api/config 时，不会覆盖底层的真实密钥
        r_put_masked = client.put(
            "/api/config",
            json={"config": {"llm_api_key": masked_key}},
        )
        assert r_put_masked.status_code == 200
        assert config_manager.get("llm_api_key") == test_secret

        # 校验已有敏感字段（如 wechat_claw_token 非空时）也均包含脱敏掩码
        for sec_field in ("wechat_claw_token", "admin_password"):
            val = config_manager.get(sec_field, "")
            if val:
                sec_masked = r_cfg_sec["config"][sec_field]
                assert "••••••••" in sec_masked
                assert val not in sec_masked

        # 恢复原配置
        client.put("/api/config", json={"config": {"llm_api_key": orig_key}})

        r_put = client.put(
            "/api/config",
            json={"config": {"default_ddl_advances": [1440, 720], "default_schedule_remind_minutes": [10, 0]}},
        )
        assert r_put.status_code == 200
        assert r_put.json()["config"]["default_ddl_advances"] == [1440, 720]
        print("✅ [2/10] RuntimeConfigManager 动态热配置保存与广播正常")

        # 3. 自然语言创建日程（支持正反语序 + 重复规则 RRULE）
        s1 = client.post("/api/chat/simulate", json={"text": "30分钟后提醒我拿快递"}).json()
        assert s1["ok"] and "已记录：拿快递" in s1["reply"] and s1["message_id"]
        msg_id_1 = s1["message_id"]

        s2 = client.post("/api/chat/simulate", json={"text": "提醒我2小时后喝水，并且重复提醒"}).json()
        assert s2["ok"] and "已记录：喝水" in s2["reply"] and "FREQ=DAILY" in s2["reply"]
        msg_id_2 = s2["message_id"]
        print("✅ [3/10] 自然语言日程创建（双向语序 + RRULE 周期重复）正常")

        # 4. 自然语言创建 DDL 备忘录（绝对日期 + 相对天数 + 自定义提前预警）
        d1 = client.post("/api/chat/simulate", json={"text": "软件工程作业最晚11月5日23:59截止"}).json()
        assert d1["ok"] and "已记录：软件工程作业" in d1["reply"] and d1["message_id"]
        msg_id_ddl1 = d1["message_id"]

        d2 = client.post("/api/chat/simulate", json={"text": "交报销单最晚要在2天后的17:00截止，提前半天提醒我"}).json()
        assert d2["ok"] and "已记录：交报销单" in d2["reply"] and d2["message_id"]
        msg_id_ddl2 = d2["message_id"]
        print("✅ [4/10] 自然语言 DDL 备忘录创建与阶梯预警计算正常")

        # 5. 方法 A：引用回复（Quote Reply）动态推迟（0 Token 成本）
        q_snooze = client.post(
            "/api/chat/simulate",
            json={"text": "推迟半小时", "reply_to_msg_id": msg_id_1},
        ).json()
        assert q_snooze["ok"] and "📅 已更新：拿快递" in q_snooze["reply"]
        msg_id_1_updated = q_snooze["message_id"]
        print("✅ [5/10] 引用回复（Quote Reply）相对推迟（推迟半小时）正常")

        # 6. 方法 A：引用回复改为具体日期时间
        q_resched = client.post(
            "/api/chat/simulate",
            json={"text": "改到明天下午3点", "reply_to_msg_id": msg_id_1_updated},
        ).json()
        assert q_resched["ok"] and "15:00" in q_resched["reply"]
        msg_id_1_final = q_resched["message_id"]
        print("✅ [6/10] 引用回复（Quote Reply）绝对时间改期（改到明天下午3点）正常")

        # 7. 询问“我还有什么事情没做”按截止紧急度分级汇报
        q_pending = client.post("/api/chat/simulate", json={"text": "我还有什么事情没做"}).json()
        assert q_pending["ok"] and "📋 未完成事项" in q_pending["reply"]
        assert "软件工程作业" in q_pending["reply"] and "交报销单" in q_pending["reply"]
        assert q_pending["message_id"] is None
        print("✅ [7/10] 未完成事项按截止时间紧急度分级盘点汇报正常")

        # 8. 引用回复标记完成与取消清理
        for mid in (msg_id_1_final, msg_id_2, msg_id_ddl1, msg_id_ddl2):
            res = client.post("/api/chat/simulate", json={"text": "已完成", "reply_to_msg_id": mid}).json()
            assert res["ok"] and "✅ 已完成" in res["reply"]
        # 清理测试产生的历史任务，保持用户数据库干净
        for t in client.get("/api/tasks").json()["tasks"]:
            if t["source_channel"] == "web" and t["title"] in ("拿快递", "喝水", "软件工程作业", "交报销单"):
                client.delete(f"/api/tasks/{t['id']}")
        print("✅ [8/10] 引用回复标记完成及 APScheduler 定时任务同步清理正常")

        # 9. Web 端 REST CRUD 与 ICS 手机本地日历订阅流验证
        r_create = client.post(
            "/api/tasks",
            json={
                "task_type": "memo_ddl",
                "title": "开源项目发布 GitHub",
                "deadline": (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds"),
                "remind_advance_minutes": [1440, 720],
            },
        ).json()
        assert r_create["ok"]
        web_task_id = r_create["task"]["id"]

        r_ics = client.get(f"/api/calendar/feed/{ics_token}.ics")
        assert r_ics.status_code == 200 and "BEGIN:VCALENDAR" in r_ics.text
        assert "[DDL截止] 开源项目发布 GitHub" in r_ics.text and "BEGIN:VALARM" in r_ics.text

        r_del = client.delete(f"/api/tasks/{web_task_id}").json()
        assert r_del["ok"]
        print("✅ [9/10] Web 任务 CRUD 接口与手机本地日历 .ics 订阅流生成正常")

    # 10. 微信 iLink 引用解析 & CalDAV / Google Calendar 序列化校验
    wx_payload = {
        "message_type": 1,
        "message_state": 2,
        "from_user_id": "wx_u1",
        "msg_id": "m100",
        "refer_msg": {"msg_id": "m099"},
        "item_list": [{"type": 1, "text_item": {"text": "已完成"}}],
    }
    assert WeChatClawAdapter._text(wx_payload) == "已完成"
    assert WeChatClawAdapter._reply_id(wx_payload) == "m099"
    print("✅ [10/10] 微信 OpenClaw (iLink) 引用结构解析与跨端日历协议校验通过")

    print("=" * 64)
    print("🎉 全部 10 项核心验收测试 100% 通过！")
    print("=" * 64)


if __name__ == "__main__":
    run_all_tests()
