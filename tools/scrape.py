"""抓贴吧一手数据：帖子标题、楼层、作者名/昵称/UID/等级/IP、楼中楼、原文。

用法：
  python tools/scrape.py --per-forum 60 --pages 2
输出：
  data/tieba_posts.jsonl   机器可读（每帖一行）
  data/tieba_posts.md      人可读（给 Claude 看的）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from aiotieba import Client

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
import sys
sys.path.insert(0, str(ROOT / 'src'))
import settings
FORUMS = settings.load()['forums']


def now_str(ts: int | None = None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts or time.time()))


def who(u) -> dict:
    if not u:
        return {}
    return {
        "uid": int(getattr(u, "user_id", 0) or 0),
        "name": getattr(u, "user_name", "") or "",
        "nick": getattr(u, "nick_name_new", "") or "",
        "level": int(getattr(u, "level", 0) or 0),
        "ip": getattr(u, "ip", "") or "",
    }


async def scrape_thread(c: Client, forum: str, t, max_pages: int) -> dict:
    tid = int(getattr(t, "tid", 0) or 0)
    rec = {
        "forum": forum,
        "tid": tid,
        "title": getattr(t, "title", "") or "",
        "thread_time": now_str(getattr(t, "create_time", 0)),
        "reply_num": int(getattr(t, "reply_num", 0) or 0),
        "author": who(getattr(t, "user", None)),
        "posts": [],
    }
    max_pages = 3 if int(getattr(t, "reply_num", 0) or 0) > 60 else max_pages
    for pn in range(1, max_pages + 1):
        try:
            pg = await c.get_posts(tid, pn=pn)
        except Exception as e:
            rec.setdefault("errors", []).append(f"pn{pn}: {e}")
            break
        objs = pg.objs or []
        if not objs:
            break
        for p in objs:
            text = "\n".join(
                getattr(f, "text", "") for f in (getattr(getattr(p, "contents", None), "objs", None) or [])
                if getattr(f, "text", "")
            )
            item = {
                "floor": int(getattr(p, "floor", 0) or 0),
                "pid": int(getattr(p, "pid", 0) or 0),
                "author": who(getattr(p, "user", None)),
                "text": text,
                "time": now_str(getattr(p, "create_time", 0)),
                "agree": int(getattr(p, "agree", 0) or 0),
                "is_op": bool(getattr(p, "is_thread_author", False)),
                "comments": [],
            }
            # 楼中楼（有回复的楼层才抓，省调用）
            if int(getattr(p, "reply_num", 0) or 0) > 0:
                try:
                    cp = await c.get_comments(tid, item["pid"], pn=1)
                    for cm in (cp.objs or [])[:10]:
                        item["comments"].append({
                            "author": who(getattr(cm, "user", None)),
                            "text": getattr(cm, "text", "") or "",
                            "time": now_str(getattr(cm, "create_time", 0)),
                        })
                except Exception:
                    pass
                await asyncio.sleep(0.25)
            rec["posts"].append(item)
        if len(objs) < 30:
            break
        await asyncio.sleep(0.25)
    return rec


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-forum", type=int, default=60)
    ap.add_argument("--pages", type=int, default=2, help="每帖抓几页楼层（每页30楼）")
    ap.add_argument("--forums", default=",".join(FORUMS))
    args = ap.parse_args()
    DATA.mkdir(exist_ok=True)

    out_jsonl = DATA / "tieba_posts.jsonl"
    with out_jsonl.open("w", encoding="utf-8") as fh:
        async with Client() as c:
            for forum in [f.strip() for f in args.forums.split(",") if f.strip()]:
                print(f"== {forum}")
                total = 0
                for pn in range(1, 6):
                    if total >= args.per_forum:
                        break
                    try:
                        page = await c.get_threads(forum, pn=pn)
                    except Exception as e:
                        print(f"  第{pn}页读失败: {e}")
                        break
                    for t in (page.objs or []):
                        if total >= args.per_forum:
                            break
                        rec = await scrape_thread(c, forum, t, args.pages)
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        fh.flush()
                        total += 1
                        print(f"  {total:>3} {rec['title'][:36]}  楼层{len(rec['posts'])}")
                        await asyncio.sleep(0.35)
    print("完成:", out_jsonl)


if __name__ == "__main__":
    asyncio.run(main())
