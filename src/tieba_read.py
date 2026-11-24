"""读贴吧：吧内帖子、楼层、我收到的 @。只用 aiotieba，不需要登录。"""
from __future__ import annotations

import asyncio
import json
import sys

from aiotieba import Client
from aiotieba.enums import ThreadSortType

# reply=按最后回复（老帖有新回复） create=按发帖时间（刚发的新帖）
_SORTS = {"reply": ThreadSortType.REPLY, "create": ThreadSortType.CREATE,
          "hot": ThreadSortType.HOT}


async def forum_threads(forum: str, n: int = 20, sort: str = "reply"):
    async with Client() as c:
        page = await c.get_threads(forum, pn=1, sort=_SORTS.get(sort, ThreadSortType.REPLY))
        rows = []
        for t in (page.objs or [])[:n]:
            rows.append({
                "tid": int(getattr(t, "tid", 0)),
                "title": getattr(t, "title", "") or "",
                "reply_num": int(getattr(t, "reply_num", 0) or 0),
                "text": (getattr(t, "text", "") or "")[:500],
                "author_id": int(getattr(t, "user_id", 0) or 0),
                "last_time": int(getattr(t, "last_time", 0) or 0),
            })
        return rows


async def thread_posts(tid: int, n: int = 12):
    async with Client() as c:
        page = await c.get_posts(tid, pn=1)
        rows = []
        for p in (page.objs or [])[:n]:
            objs = getattr(getattr(p, "contents", None), "objs", None) or []
            images = []
            for frag in objs:
                for attr in ("src", "big_src", "origin_src"):
                    v = getattr(frag, attr, None)
                    if v:
                        images.append(str(v))
                        break
            rows.append({
                "pid": int(getattr(p, "pid", 0) or 0),
                "floor": int(getattr(p, "floor", 0) or 0),
                "author_id": int(getattr(p, "user_id", 0) or 0),
                "text": (getattr(p, "text", "") or "")[:600],
                "images": images[:3],
                "time": int(getattr(p, "create_time", 0) or 0),
            })
        return rows


async def my_ats(n: int = 10):
    """需要登录（BDUSS）才能拿到；未登录会抛异常。"""
    async with Client() as c:
        page = await c.get_ats(pn=1)
        return [{
            "tid": int(getattr(a, "tid", 0) or 0),
            "pid": int(getattr(a, "pid", 0) or 0),
            "text": (getattr(a, "text", "") or "")[:400],
            "fname": getattr(a, "fname", "") or "",
        } for a in (page.objs or [])[:n]]


async def _cli():
    if len(sys.argv) < 2:
        print("用法: python src/tieba_read.py <吧名> [tid]")
        return
    forum = sys.argv[1]
    if len(sys.argv) > 2:
        posts = await thread_posts(int(sys.argv[2]))
        print(json.dumps(posts, ensure_ascii=False, indent=2))
        return
    rows = await forum_threads(forum)
    for r in rows:
        print(f"{r['tid']}\t{r['reply_num']}\t{r['title'][:40]}")


if __name__ == "__main__":
    asyncio.run(_cli())
