"""社区知识库：条目池 + 渲染。

原则：**只增不删**。新信息进来追加成新条目；同一件事的新说法不覆盖旧条目，
而是再存一条并标注 (新说法)，渲染时按时间排，读者自己看先后。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KB = ROOT / "knowledge"
ENTRIES = KB / "entries.jsonl"
BASE = KB / "base.md"
UPDATES = KB / "updates"


def _read() -> list[dict]:
    if not ENTRIES.exists():
        return []
    out = []
    for line in ENTRIES.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _append(e: dict) -> None:
    KB.mkdir(exist_ok=True)
    e.setdefault("id", f"{int(time.time()*1000)}-{len(_read())}")
    e.setdefault("added", time.strftime("%Y-%m-%d %H:%M"))
    with ENTRIES.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def _norm(s: str) -> str:
    return re.sub(r"[\s，。,.!?！？、;；:：]+", "", s or "")[:80]


def is_dup(e: dict, rows: list[dict]) -> bool:
    """粗去重：同话题 + 主张文本高度重合。"""
    n = _norm(e.get("claim", ""))
    for r in rows:
        if r.get("topic") != e.get("topic"):
            continue
        if _norm(r.get("claim", "")) == n:
            return True
    return False


BLOCK = ("黑鬼", "尼哥", "伟人", "领导人", "政治", "党", "国家", "外交", "民族", "地域黑",
         "盒", "开盒", "真名", "身份证", "手机号", "地址")


def blocked(e: dict) -> bool:
    blob = f"{e.get('claim','')}{e.get('quote','')}"
    return any(b in blob for b in BLOCK)


def add_many(entries: list[dict]) -> tuple[int, int, int]:
    rows = _read()
    added = dup = skipped = 0
    for e in entries:
        if not e.get("claim"):
            continue
        if blocked(e):
            skipped += 1
            continue
        if is_dup(e, rows):
            dup += 1
            continue
        _append(e)
        rows.append(e)
        added += 1
    return added, dup, skipped


def render() -> Path:
    rows = _read()
    KB.mkdir(exist_ok=True)
    by_topic: dict[str, list[dict]] = {}
    for r in rows:
        by_topic.setdefault(r.get("topic") or "其他", []).append(r)

    lines = [
        "# 社区知识库（自动增量更新，只增不删）",
        "",
        f"- 条目数：{len(rows)}",
        f"- 最后更新：{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "> 说明：每条后面括号是来源帖和收录时间。同一件事有不同说法时，会并存多条，不覆盖。",
        "",
    ]
    order = ["黑话", "人物", "事件", "现状", "立场", "作品", "梗"]
    topics = [t for t in order if t in by_topic] + [t for t in by_topic if t not in order]
    for t in topics:
        lines.append(f"## {t}")
        lines.append("")
        for r in sorted(by_topic[t], key=lambda x: x.get("added", "")):
            src = r.get("source") or {}
            link = f"[{src.get('forum','')} {src.get('tid','')}](https://tieba.baidu.com/p/{src.get('tid','')})" if src.get("tid") else ""
            lines.append(f"- {r['claim']}")
            if r.get("quote"):
                lines.append(f"  - 原文：「{r['quote']}」")
            lines.append(f"  - 来源：{link} · 收录 {r.get('added','')}")
        lines.append("")
    BASE.write_text("\n".join(lines), encoding="utf-8")

    UPDATES.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d")
    up = UPDATES / f"{stamp}.md"
    with up.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {time.strftime('%H:%M')} 更新（+{len(rows)} 条总池）\n\n")
        for r in rows[-20:]:
            fh.write(f"- [{r.get('topic')}] {r.get('claim')}\n")
    return BASE
