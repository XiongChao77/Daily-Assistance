"""Local plan state shared by the daily job and the report's loopback API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import uuid
import msvcrt
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PORT = 8765


def plans_directory(root: Path = ROOT) -> Path:
    return root / "data" / "plans"


def state_directory(root: Path = ROOT) -> Path:
    return root / "data" / "state"


def atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def ensure_token(root: Path = ROOT) -> str:
    path = state_directory(root) / ".plan_service_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    token = secrets.token_urlsafe(32)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(token + "\n")
        return token
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip()


def load_state(root: Path = ROOT) -> dict:
    path = state_directory(root) / "plan_state.json"
    if not path.exists():
        return {"plans": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("plans"), dict):
        raise ValueError("plan_state.json 结构无效")
    return data


def save_state(data: dict, root: Path = ROOT) -> None:
    atomic_text(state_directory(root) / "plan_state.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def state_lock(root: Path):
    path = state_directory(root) / "plan_state.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.seek(0)
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def plan_id(source: str, title: str) -> str:
    return hashlib.sha256(f"{source}\0{title}".encode("utf-8")).hexdigest()[:16]


def ensure_stable_ids(root: Path = ROOT) -> None:
    """Migrate plan lines to durable IDs while preserving existing state keys."""
    marker = re.compile(r"\s*<!--\s*plan-id:\s*([a-zA-Z0-9_-]{8,64})\s*-->\s*$")
    checkbox = re.compile(r"^([-*]\s+\[[ xX]\]\s+)(.+)$")
    heading = re.compile(r"^(#{1,6}\s+)(.+)$")
    due_date = re.compile(r"\s*(?:due|截止)[:：]\s*\d{4}-\d{2}-\d{2}", re.I)
    for path in sorted(plans_directory(root).glob("*.md")):
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines()
        changed = False
        seen: set[str] = set()
        for index, line in enumerate(lines):
            match = checkbox.match(line) or heading.match(line)
            if not match:
                continue
            prefix, raw_title = match.groups()
            id_match = marker.search(raw_title)
            if id_match:
                item_id = id_match.group(1)
                title = marker.sub("", raw_title).strip()
            else:
                title = raw_title.strip()
                title = due_date.sub("", title).strip()
                item_id = plan_id(path.name, title)
                if item_id in seen:
                    item_id = uuid.uuid4().hex[:16]
                lines[index] = f"{prefix}{raw_title.rstrip()} <!-- plan-id: {item_id} -->"
                changed = True
            if item_id in seen:
                item_id = uuid.uuid4().hex[:16]
                base_title = marker.sub("", raw_title).rstrip()
                lines[index] = f"{prefix}{base_title} <!-- plan-id: {item_id} -->"
                changed = True
            seen.add(item_id)
        if changed:
            trailing_newline = "\n" if original.endswith(("\n", "\r")) else ""
            atomic_text(path, "\n".join(lines) + trailing_newline)


def read_plans(today: date, root: Path = ROOT, include_done: bool = False) -> list[dict]:
    ensure_stable_ids(root)
    overrides = load_state(root)["plans"]
    plans = []
    for path in sorted(plans_directory(root).glob("*.md")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            checkbox = re.match(r"^[-*]\s+\[([ xX])\]\s+(.+)$", stripped)
            if checkbox:
                checked = checkbox.group(1).lower() == "x"
                raw_title = checkbox.group(2)
            else:
                header = re.match(r"^#{1,6}\s+(.+)$", stripped)
                if not header:
                    continue
                checked = False
                raw_title = header.group(1)
            explicit_id = re.search(r"\s*<!--\s*plan-id:\s*([a-zA-Z0-9_-]{8,64})\s*-->\s*$", raw_title)
            stable_id = explicit_id.group(1) if explicit_id else None
            title = re.sub(r"\s*<!--\s*plan-id:\s*[a-zA-Z0-9_-]{8,64}\s*-->\s*$", "", raw_title).strip()
            due_match = re.search(r"(?:due|截止)[:：]\s*(\d{4}-\d{2}-\d{2})", title, re.I)
            due = None
            if due_match:
                try:
                    due = date.fromisoformat(due_match.group(1))
                except ValueError:
                    pass
                title = re.sub(r"\s*(?:due|截止)[:：]\s*\d{4}-\d{2}-\d{2}", "", title, flags=re.I).strip()
            item_id = stable_id or plan_id(path.name, title)
            saved = overrides.get(item_id, {})
            done = checked or bool(saved.get("done"))
            if done and not include_done:
                continue
            status = "已完成" if done else "持续关注" if due is None else "已逾期" if due < today else "今日到期" if due == today else "未到期"
            plans.append({
                "id": item_id, "title": title, "source": path.name,
                "due": due.isoformat() if due else None, "status": status,
                "done": done, "progress": int(saved.get("progress", 0)),
                "note": str(saved.get("note", "")),
            })
    return plans


def add_plan(title: str, due: str | None = None, root: Path = ROOT) -> dict:
    if not isinstance(title, str):
        raise ValueError("计划名称不能为空")
    title = title.strip()
    if not title or len(title) > 200 or any(char in title for char in "\r\n<>"):
        raise ValueError("计划名称须为 1–200 个字符，且不能包含换行或尖括号")
    if due in (None, ""):
        due_date = None
    elif isinstance(due, str):
        try:
            due_date = date.fromisoformat(due)
        except ValueError as exc:
            raise ValueError("截止日期格式须为 YYYY-MM-DD") from exc
        if due_date.isoformat() != due:
            raise ValueError("截止日期格式须为 YYYY-MM-DD")
    else:
        raise ValueError("截止日期格式须为 YYYY-MM-DD")

    with state_lock(root):
        path = plans_directory(root) / "custom.md"
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        item_id = uuid.uuid4().hex[:16]
        line = f"- [ ] {title}"
        if due_date:
            line += f" 截止: {due_date.isoformat()}"
        line += f" <!-- plan-id: {item_id} -->\n"
        atomic_text(path, existing.rstrip("\n") + ("\n" if existing else "") + line)
        return next(item for item in read_plans(date.today(), root, include_done=True) if item["id"] == item_id)


def update_plan(item_id: str, action: str, root: Path = ROOT, progress: int | None = None, note: str | None = None) -> dict:
    with state_lock(root):
        current = {item["id"]: item for item in read_plans(date.today(), root, include_done=True)}
        if item_id not in current:
            raise KeyError("计划事项不存在")
        if action not in ("complete", "progress"):
            raise ValueError("无效的操作")
        data = load_state(root)
        record = data["plans"].setdefault(item_id, {})
        if action == "complete":
            record.update({"done": True, "progress": 100})
        else:
            if not isinstance(progress, int) or isinstance(progress, bool) or not 0 <= progress <= 100:
                raise ValueError("进度必须是 0–100 的整数")
            record["progress"] = progress
            if note is not None:
                record["note"] = str(note).strip()[:500]
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        save_state(data, root)
    return next(item for item in read_plans(date.today(), root, include_done=True) if item["id"] == item_id)
