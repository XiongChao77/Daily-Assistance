import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import daily
import plan_store
import reporting


class DailyTests(unittest.TestCase):
    def test_added_plan_persists_and_can_be_updated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = plan_store.add_plan("准备演讲", "2026-11-05", root)
            self.assertEqual(created["title"], "准备演讲")
            self.assertEqual(created["due"], "2026-11-05")
            self.assertIn(created["id"], (root / "data" / "plans" / "custom.md").read_text(encoding="utf-8"))
            loaded = plan_store.read_plans(date(2026, 11, 1), root)
            self.assertEqual(loaded[0]["id"], created["id"])
            plan_store.update_plan(created["id"], "complete", root)
            self.assertEqual(plan_store.read_plans(date(2026, 11, 1), root), [])

    def test_added_plan_rejects_invalid_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for title, due in [("", None), ("bad\n# injected", None), ("valid", "2026-02-30")]:
                with self.assertRaises(ValueError):
                    plan_store.add_plan(title, due, root)
            self.assertFalse((root / "data" / "plans" / "custom.md").exists())

    def test_completed_plan_is_excluded_from_future_reminders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "plans").mkdir(parents=True)
            (root / "data" / "plans" / "tasks.md").write_text("# 学习英语\n", encoding="utf-8")
            item = plan_store.read_plans(date(2026, 11, 1), root)[0]
            updated = plan_store.update_plan(item["id"], "progress", root, progress=45, note="已学两章")
            self.assertEqual(updated["progress"], 45)
            self.assertEqual(updated["note"], "已学两章")
            plan_store.update_plan(item["id"], "complete", root)
            self.assertEqual(plan_store.read_plans(date(2026, 11, 1), root), [])
            self.assertEqual(plan_store.read_plans(date(2026, 11, 1), root, include_done=True)[0]["status"], "已完成")

    def test_html_template_renders_structured_data(self):
        story = {"title": "测试新闻标题", "body": "具体新闻内容", "impact": "市场影响", "published_at": "2026-11-01", "source_name": "测试来源", "source_url": "https://example.com"}
        market = {"name": "S&P 500", "url": "https://finance.yahoo.com/", "available": True, "price_label": "7,580.07", "change_label": "-140.32", "percent_label": "-1.82%", "direction": "down", "points": "0,5 100,20", "as_of": "2026-10-30 20:00 UTC"}
        asset = {"flow_date": "2026-10-30", "net_flow": "净流出", "funds": [], "analysis": "资金流与行情分析", "source_name": "测试来源", "source_url": "https://example.com"}
        data = {"date": "2026-11-01", "generated_at": "2026-11-01 07:30:00", "news": [{"id": "wars", "title": "Wars", "display_items": 4, "items": [story.copy() for _ in range(10)]}], "markets": [{"id": "us", "title": "US Markets", "instruments": [market]}], "crypto_news": [story.copy() for _ in range(10)], "etf": {"btc": asset, "eth": asset, "market_takeaway": "市场综述"}, "plans": [{"id": "plan1", "title": "学习英语", "source": "tasks.md", "due": None, "status": "持续关注", "done": False, "progress": 0, "note": ""}, {"id": "plan2", "title": "已完成事项", "source": "tasks.md", "due": None, "status": "已完成", "done": True, "progress": 100, "note": ""}]}
        html = reporting.render_saved(data)
        self.assertIn("测试新闻标题", html)
        self.assertIn("7,580.07", html)
        self.assertIn("Bitcoin ETF", html)
        self.assertEqual(html.count('data-extra="wars"'), 6)
        self.assertEqual(html.count('data-extra="crypto-news"'), 6)
        self.assertIn('data-action="complete"', html)
        self.assertIn('data-action="progress"', html)
        self.assertIn('id="add-plan-button"', html)
        self.assertIn('id="add-plan-dialog"', html)
        self.assertLess(html.index('<section id="plans">'), html.index('<section id="news">'))
        self.assertLess(html.index('href="#plans"'), html.index('href="#news"'))
        self.assertIn('<title>Daily · 2026-11-01</title>', html)
        self.assertLess(html.index('data-plan-id="plan1"'), html.index('<details class="completed-plans"'))
        self.assertLess(html.index('<details class="completed-plans"'), html.index('data-plan-id="plan2"'))
        self.assertIn('<span id="completed-count">1</span>', html)

    def test_model_json_and_text_cleanup(self):
        self.assertEqual(reporting.parse_json('```json\n{"items": []}\n```'), {"items": []})
        self.assertEqual(reporting.clean_text('内容。([来源](https://example.com))'), '内容。')

    def test_each_day_has_its_own_output_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(daily, "OUTPUT", Path(directory)):
                self.assertEqual(daily.day_output("2026-11-01"), Path(directory) / "2026-11-01")
                self.assertNotEqual(daily.day_output("2026-11-01"), daily.day_output("2026-11-02"))

    def test_plans_parse_headings_checkboxes_and_due_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data" / "plans").mkdir(parents=True)
            (root / "data" / "plans" / "tasks.md").write_text(
                "# 交易\n- [ ] 健身 截止: 2026-11-01\n- [x] 已完成\n- [ ] 买衣服 due: 2026-10-31\n",
                encoding="utf-8",
            )
            with patch.object(daily, "ROOT", root):
                plans = daily.read_plans(date(2026, 11, 1))
        self.assertEqual([p["status"] for p in plans], ["持续关注", "今日到期", "已逾期"])
        self.assertEqual([p["title"] for p in plans], ["交易", "健身", "买衣服"])

    def test_response_text_extracts_responses_message(self):
        payload = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "# 简报"}]}]}
        self.assertEqual(reporting.response_text(payload), "# 简报")

    def test_atomic_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(daily, "STATE", Path(directory) / "state.json"):
                daily.save_state({"days": {"2026-11-01": {"report_done": True}}})
                self.assertTrue(daily.load_state()["days"]["2026-11-01"]["report_done"])


if __name__ == "__main__":
    unittest.main()
