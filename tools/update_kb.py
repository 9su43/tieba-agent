"""定期更新社区知识库：抓新帖 → 模型抽取条目 → 增量并入 knowledge/。

用法：
  python tools/update_kb.py            # 抓一轮，抽条目，合并
  python tools/update_kb.py --dry      # 只打印抽到什么，不写库

要点：
- 只增不删（同话题同主张才跳过）；
- 每条都带原帖链接和原文引用；
- 抓过的帖子记在 knowledge/seen.json，不重复抽。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import draft as draft_mod  # noqa: E402
import kb  # noqa: E402
import tieba_read  # noqa: E402

KB = ROOT / "knowledge"
SEEN = KB / "seen.json"
import settings
FORUMS = settings.load()["forums"]

EXTRACT_PROMPT = """你在整理运行者指定贴吧社区的知识库。下面是若干帖子原文。

请从里面抽取**社区知识条目**，只抽下面这几类：
- 黑话：吧里在用的词、缩写、梗，写出它的意思和用法
- 人物：吧里提到的人/账号/团体/官方称呼（只写公开昵称，不要写任何真实身份、姓名、地址）
- 事件：发生过的具体事情（时间、谁、发生了什么），只写帖子里明确说了的
- 现状：当下大家在讨论的局势、争议、矛盾
- 立场：某一方对某事的典型态度
- 梗/名场面：反复被提到的句子、名场面

要求：
1. 每条一个 claim（一句话，用吧友会说的话），能带一句原文 quote 就带（照抄 30 字内）。
2. 不要编造，帖子没说的不要写。不确定的标注 (不确定)。
3. 每帖最多抽 3 条，宁缺勿滥。
4. 只输出 JSON 数组，格式：
[{"topic":"黑话|人物|事件|现状|立场|梗","claim":"...","quote":"...","forum":"来源吧","tid":123}]
"""


def load_seen() -> set[int]:
    if SEEN.exists():
        return set(json.loads(SEEN.read_text(encoding="utf-8")).get("tids", []))
    return set()


def save_seen(tids: set[int]) -> None:
    KB.mkdir(exist_ok=True)
    SEEN.write_text(json.dumps({"tids": sorted(tids)[:20000]}, ensure_ascii=False), encoding="utf-8")


def extract(cfg: dict, forum: str, threads: list[dict]) -> list[dict]:
    blocks = []
    for t in threads:
        lines = [f"【{forum}吧】{t['title']}（tid={t['tid']}）"]
        for p in t["posts"][:6]:
            txt = re.sub(r"\s+", " ", p.get("text") or "").strip()
            if txt:
                lines.append(f"- {txt[:150]}")
        blocks.append("\n".join(lines))
    msgs = [
        {"role": "system", "content": EXTRACT_PROMPT},
        {"role": "user", "content": "\n\n".join(blocks)},
    ]
    # [2026-11-13] 1400 太小：推理模型的思考会把额度吃光，直接返回空（finish=length），抽出来 0 条。
    raw = draft_mod.call_model(cfg, msgs, max_tokens=4000)
    m = re.search(r"\[.*", raw, re.S)
    if not m:
        return []
    text = m.group(0)
    data = None
    for cut in range(len(text), 0, -1):
        try:
            data = json.loads(text[:cut] if cut < len(text) else text)
            break
        except json.JSONDecodeError:
            continue
    if not isinstance(data, list):
        return []
    out = []
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict) or not d.get("claim"):
            continue
        out.append({
            "topic": d.get("topic") or "现状",
            "claim": str(d["claim"])[:200],
            "quote": str(d.get("quote") or "")[:120],
            "source": {"forum": forum, "tid": int(d.get("tid") or 0)},
            "forum": forum,
            "added": time.strftime("%Y-%m-%d %H:%M"),
        })
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--per-forum", type=int, default=12)
    args = ap.parse_args()

    import json as _json
    cfg_file = ROOT / "config.json"
    cfg = _json.loads((cfg_file if cfg_file.exists() else ROOT / "config.example.json").read_text(encoding="utf-8"))

    seen = load_seen()
    total_added = 0
    for forum in FORUMS:
        try:
            threads = await tieba_read.forum_threads(forum, args.per_forum)
        except Exception as e:
            print(f"[{forum}] 读失败 {e}")
            continue
        fresh = [t for t in threads if t["tid"] not in seen]
        if not fresh:
            print(f"[{forum}] 没有新帖")
            continue
        batch = []
        for t in fresh[: args.per_forum]:
            try:
                t["posts"] = await tieba_read.thread_posts(t["tid"], 8)
            except Exception:
                t["posts"] = []
            batch.append(t)
        entries: list[dict] = []
        for i in range(0, len(batch), 3):
            try:
                entries += extract(cfg, forum, batch[i:i + 3])
            except Exception as e:
                print(f"[{forum}] 第{i//3+1}批抽取失败 {e}")
        if args.dry:
            for e in entries:
                print(f"  [{e['topic']}] {e['claim']}")
        else:
            added, dup, skipped = kb.add_many(entries)
            total_added += added
            print(f"[{forum}] 新条目 {added}，重复跳过 {dup}，涉敏感过滤 {skipped}")
        for t in batch:
            seen.add(t["tid"])

    if not args.dry:
        save_seen(seen)
        path = kb.render()
        print(f"知识库已更新：{path}")
        print(f"本轮新增 {total_added} 条")


if __name__ == "__main__":
    asyncio.run(main())
