"""草稿存储：state/drafts.jsonl，每行一条，带 status 方便控制台审批。

status: pending（待审） / posted（已发） / failed（发失败） / discarded（丢弃）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
FILE = STATE / "drafts.jsonl"


def _read() -> list[dict]:
    if not FILE.exists():
        return []
    rows = []
    for line in FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        row.setdefault("status", "pending")
        rows.append(row)
    return rows


def _write(rows: list[dict]) -> None:
    """整表重写。Windows 上 os.replace 会因目标被占用报 WinError 5，
    而控制台点击（丢/发）与 main.py 跑一轮可能同时写，所以要退避重试。"""
    STATE.mkdir(exist_ok=True)
    tmp = FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    last: Exception | None = None
    for delay in (0.05, 0.15, 0.3, 0.6, 1.0, 1.5):
        try:
            tmp.replace(FILE)
            return
        except PermissionError as e:      # 文件被别的进程占用
            last = e
            time.sleep(delay)
    raise last if last else RuntimeError("写入 drafts.jsonl 失败")


def append(rec: dict) -> None:
    STATE.mkdir(exist_ok=True)
    rec.setdefault("status", "pending")
    rec.setdefault("at", time.strftime("%Y-%m-%d %H:%M"))
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    last: Exception | None = None
    for delay in (0.05, 0.15, 0.3, 0.6, 1.0):
        try:
            with FILE.open("a", encoding="utf-8") as fh:
                fh.write(line)
            return
        except PermissionError as e:
            last = e
            time.sleep(delay)
    raise last if last else RuntimeError("追加 drafts.jsonl 失败")


def all_rows() -> list[dict]:
    return _read()


def update(index: int, **fields) -> dict | None:
    rows = _read()
    if index < 0 or index >= len(rows):
        return None
    rows[index].update(fields)
    _write(rows)
    return rows[index]


def stats() -> dict:
    rows = _read()
    from settings import local_now
    today = local_now().strftime("%Y-%m-%d")
    return {
        "total": len(rows),
        "pending": sum(1 for r in rows if r.get("status") == "pending"),
        "posted": sum(1 for r in rows if r.get("status") == "posted"),
        "failed": sum(1 for r in rows if r.get("status") == "failed"),
        "discarded": sum(1 for r in rows if r.get("status") == "discarded"),
        "postedToday": sum(1 for r in rows if r.get("status") == "posted" and str(r.get("posted_at") or r.get("at", "")).startswith(today)),
    }
