"""Generate a daily brief and remind the current Windows user of plans."""

from __future__ import annotations

import argparse
import json
import logging
import msvcrt
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import plan_store


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "data" / "state"
STATE = STATE_DIR / "state.json"
OUTPUT = ROOT / "data" / "reports"
DAILY_LOGS = ROOT / "logs" / "daily"
TIMEZONE = "Asia/Shanghai"
RUN_AT = clock_time(7, 30)
LOG = logging.getLogger("daily")
REPORT_NAME = "Daily.html"


def update_desktop_shortcut(report: Path) -> None:
    """Point the current user's desktop shortcut at the latest report."""
    script = (
        "$ErrorActionPreference = 'Stop'; "
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        "if (-not $desktop) { throw 'Desktop folder not found' }; "
        "$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $desktop 'Daily.lnk')); "
        "$shortcut.TargetPath = $env:DAILY_REPORT_PATH; "
        "$shortcut.WorkingDirectory = $env:DAILY_REPORT_DIR; "
        "$shortcut.Description = 'Daily report'; "
        "$shortcut.Save()"
    )
    env = os.environ.copy()
    env["DAILY_REPORT_PATH"] = str(report.resolve())
    env["DAILY_REPORT_DIR"] = str(report.resolve().parent)
    subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, capture_output=True, text=True, env=env)


def day_output(day: str) -> Path:
    return OUTPUT / day


def now_local() -> datetime:
    try:
        return datetime.now(ZoneInfo(TIMEZONE))
    except ZoneInfoNotFoundError:
        # The scheduled Windows machine is configured for China Standard Time.
        from datetime import timedelta, timezone

        return datetime.now(timezone(timedelta(hours=8)))


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def single_instance():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / "run.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if lock.read(1) == b"":
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("已有一个每日任务正在运行") from exc
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def load_state() -> dict:
    if not STATE.exists():
        return {"days": {}}
    try:
        data = json.loads(STATE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
            raise ValueError("状态文件结构无效")
        return data
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"无法读取 {STATE}: {exc}") from exc


def save_state(state: dict) -> None:
    atomic_text(STATE, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def read_plans(today: date, include_done: bool = False) -> list[dict]:
    return plan_store.read_plans(today, ROOT, include_done)


def notify(plans: list[dict], report: Path | None, error: str | None) -> None:
    if not plans:
        LOG.info("所有计划事项已完成，不发送计划通知或弹窗")
        return
    urgent = sum(item["status"] in ("今日到期", "已逾期") for item in plans)
    body = f"{len(plans)} 项计划待办，其中 {urgent} 项到期或逾期。"
    body += " 今日简报已生成。" if report else " 今日简报生成失败，请查看日志。" if error else " 今日简报正在生成，完成后可从桌面 Daily 快捷方式打开。"
    try:
        from win11toast import notify as show_toast

        options = {"title": "每日资讯与计划提醒", "body": body, "duration": "long"}
        if report:
            options["button"] = {"activationType": "protocol", "arguments": report.resolve().as_uri(), "content": "查看报告"}
        show_toast(**options)
    except Exception:
        LOG.exception("Windows 通知中心消息发送失败，继续显示桌面提醒")

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        raise RuntimeError(f"找不到桌面提醒解释器：{pythonw}")
    ready = day_output(now_local().date().isoformat()) / f"popup-{os.getpid()}-{time.time_ns()}.ready"
    command = [str(pythonw), str(ROOT / "reminder_popup.py"), "--ready-file", str(ready)]
    if report:
        command.extend(["--report", str(report)])
    child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failure = ready.with_suffix(".error")
    try:
        for _ in range(50):
            if ready.exists():
                return
            if failure.exists():
                raise RuntimeError(f"桌面提醒启动失败：{failure.read_text(encoding='utf-8')}")
            if child.poll() is not None:
                raise RuntimeError(f"桌面提醒进程提前退出：{child.returncode}")
            time.sleep(0.1)
        raise RuntimeError("桌面提醒未能在 5 秒内显示")
    finally:
        ready.unlink(missing_ok=True)
        failure.unlink(missing_ok=True)


def run(force: bool = False, refresh: bool = False, render_only: bool = False) -> int:
    current = now_local()
    if not force and current.time() < RUN_AT:
        LOG.info("尚未到 07:30，跳过")
        return 0
    day = current.date().isoformat()
    with single_instance():
        state = load_state()
        entry = state["days"].setdefault(day, {})
        report_path = day_output(day) / REPORT_NAME
        old_report_path = day_output(day) / "report.html"
        data_path = day_output(day) / "report.json"
        plans = read_plans(current.date(), include_done=True)
        pending_plans = [plan for plan in plans if not plan["done"]]
        error = None

        # Send plan reminders before any potentially slow model requests.
        if not entry.get("notification_done") or not entry.get("popup_done"):
            try:
                ready_report = report_path if entry.get("report_complete", entry.get("report_done", False)) and report_path.exists() else None
                notify(pending_plans, ready_report, None)
                entry.update({"notification_done": True, "popup_done": True, "notification_report_done": bool(ready_report), "notification_at": now_local().isoformat()})
                save_state(state)
                LOG.info("已发送计划提醒")
            except Exception as exc:
                entry["notification_error"] = str(exc)
                save_state(state)
                LOG.exception("通知发送失败")

        needs_retry = not entry.get("report_complete", entry.get("report_done", False))
        if refresh or render_only or needs_retry or not report_path.exists():
            try:
                from reporting import build_report_data, render_saved

                if render_only or (not refresh and old_report_path.exists() and data_path.exists() and not report_path.exists()):
                    data = json.loads(data_path.read_text(encoding="utf-8"))
                else:
                    data = build_report_data(current, plans, day_output(day) / "report_draft.json")
                report = render_saved(data)
                atomic_text(report_path, report)
                atomic_text(data_path, json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
                old_report_path.unlink(missing_ok=True)
                complete = not data.get("generation_errors")
                error = None if complete else "；".join(item["message"] for item in data["generation_errors"])
                draft_path = day_output(day) / "report_draft.json"
                if not render_only and draft_path.exists():
                    draft = json.loads(draft_path.read_text(encoding="utf-8"))
                    draft["complete"] = complete
                    atomic_text(draft_path, json.dumps(draft, ensure_ascii=False, indent=2) + "\n")
                entry.update({"report_done": True, "report_complete": complete, "report_at": now_local().isoformat(), "report_path": str(report_path), "last_error": error})
                save_state(state)
                LOG.info("%s：%s", "已完整生成报告" if complete else "已生成部分报告，失败栏目将在后续运行重试", report_path)
            except Exception as exc:
                error = str(exc)
                entry.update({"report_done": bool(report_path.exists()), "report_complete": False, "last_error": error, "last_attempt": now_local().isoformat()})
                save_state(state)
                LOG.exception("报告生成失败")
        if report_path.exists():
            try:
                update_desktop_shortcut(report_path)
                LOG.info("已更新桌面快捷方式：%s", report_path)
            except Exception:
                LOG.exception("桌面快捷方式更新失败")
                return 1
        return 0 if not error and entry.get("report_done") and entry.get("report_complete", True) and entry.get("notification_done") else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="忽略 07:30 时间门槛，用于手动运行")
    parser.add_argument("--refresh", action="store_true", help="重新生成今天的报告，即使已有成功记录")
    parser.add_argument("--render-only", action="store_true", help="从当天 report.json 重新套用 HTML 模板，不调用模型")
    parser.add_argument("--notify-test", action="store_true", help="只测试 Windows 通知")
    args = parser.parse_args()
    today_dir = day_output(now_local().date().isoformat())
    today_dir.mkdir(parents=True, exist_ok=True)
    log_dir = DAILY_LOGS / now_local().date().isoformat()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_dir / "run.log", encoding="utf-8"), logging.StreamHandler()],
    )
    if args.notify_test:
        report = today_dir / REPORT_NAME
        notify(read_plans(now_local().date()), report if report.exists() else None, None)
        return 0
    try:
        return run(args.force or args.refresh or args.render_only, args.refresh, args.render_only)
    except Exception:
        LOG.exception("每日任务启动失败")
        return 1


if __name__ == "__main__":
    sys.exit(main())
