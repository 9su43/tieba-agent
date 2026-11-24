"""主循环：读吧内新帖 → 打分挑帖 → 出草稿（dry-run 落盘）。

规则：
- 三个吧轮流分配额度，不让一个吧吃满；
- 跳过水楼/日程表/教程/打卡这类没内容的帖子；
- 优先"有人在吵/在问/在骂"的帖；
- 同一帖按 last_pid 去重，不重复回。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path


import draft as draft_mod
import forms
import posttype
import store
import posting_policy
import tieba_read

from settings import local_now

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"

SKIP_TITLE = re.compile(r"水楼|水经验|经验贴|经验\+|日程表|教程|打卡|签到|求助贴|答疑|公告|吧规")
DRAMA = ("?", "？", "吗", "怎么", "为什么", "吵", "典", "急", "笑死", "毕业", "散了",
         "线下", "开盒", "中之人", "唯粉", "cp", "拉踩", "节奏", "塌", "退")


def load_cfg() -> dict:
    f = ROOT / "config.json"
    if not f.exists():
        f = ROOT / "config.example.json"
    return json.loads(f.read_text(encoding="utf-8"))


def load_seen() -> dict:
    f = STATE / "seen.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}


def save_seen(seen: dict) -> None:
    STATE.mkdir(exist_ok=True)
    (STATE / "seen.json").write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")


def own_thread_ids() -> set[str]:
    """自己创建的帖子 tid 集合（搬运台每发一条就记一笔）。这些帖子里的评论不回。"""
    f = STATE / "own-threads.json"
    if not f.exists():
        return set()
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return set()
    tids = data.get("tids") if isinstance(data, dict) else None
    return {str(k) for k in (tids or {})}


def _pid_of(v):
    """seen 的值可能是旧格式（int pid）或新格式（{"pid","rn"}）。"""
    return v.get("pid") if isinstance(v, dict) else v


def mark_seen(seen: dict, c: dict) -> None:
    """记下已处理到的楼层，同时记回复数，下一轮回复数没变就不用再读这帖。"""
    seen[c["key"]] = {"pid": c["last_pid"], "rn": c["thread"].get("reply_num")}


def score(title: str, posts: list[dict]) -> int:
    text = (title or "") + " " + " ".join((p.get("text") or "") for p in posts[-5:])
    hits = sum(1 for c in DRAMA if c in text)
    return hits


# 每轮统计：读了多少帖、卡在哪一步、时间花在哪
STATS: dict = {}


def reset_stats() -> None:
    STATS.clear()
    STATS.update({"reads": 0, "read_sec": 0.0, "threads": 0, "cands": 0,
                  "skip_strategy": 0, "skip_silence": 0, "rejected": 0,
                  "model_calls": 0, "made": 0})


def stat(key: str, n: float = 1) -> None:
    STATS[key] = STATS.get(key, 0) + n


def stats_line() -> str:
    s = STATS
    return (f"[统计] 扫帖 {s.get('threads', 0)} · 读帖 {s.get('reads', 0)} 次/{s.get('read_sec', 0):.0f}s"
            f" · 候选 {s.get('cands', 0)} · 策略跳过 {s.get('skip_strategy', 0)}"
            f" · 沉默跳过 {s.get('skip_silence', 0)} · 打回 {s.get('rejected', 0)}"
            f" · 模型调用 {s.get('model_calls', 0)} · 成稿 {s.get('made', 0)}")


async def collect(cfg: dict, seen: dict, manual: bool = False) -> list[dict]:
    """把三个吧的候选帖都拉出来打分。

    manual=True（人点了运行）：多抓「按发帖时间」和「热门」，并且不因"看过"而跳过，
    目的是让手动那一轮尽量刷满。
    """
    done_tids = {str(r.get("tid")) for r in store.all_rows() if r.get("status") in ("posted", "pending")}
    cands = []
    if manual:
        n_reply = int(cfg.get("manualFollow", 24) or 24)
        n_new = int(cfg.get("manualNew", 12) or 12)
        n_hot = int(cfg.get("manualHot", 12) or 12)
        sorts = [("reply", n_reply), ("create", n_new), ("hot", n_hot)]
    else:
        n_reply = int(cfg.get("followThreads", 18) or 18)
        n_new = int(cfg.get("newThreads", 8) or 0)
        sorts = [("reply", n_reply)] + ([("create", n_new)] if n_new > 0 else [])
    for forum in cfg["forums"]:
        threads = []
        have = set()
        for sort_name, n in sorts:
            try:
                got = await tieba_read.forum_threads(forum, n, sort=sort_name)
            except Exception as e:
                print(f"[{forum}] 读 {sort_name} 失败：{e}")
                continue
            for t in got:
                if t["tid"] in have:
                    continue
                have.add(t["tid"])
                if sort_name == "create":
                    t["fresh"] = True        # 新帖：0 回复也放行
                if sort_name in ("create", "hot"):
                    t["force"] = True        # 手动模式：新帖/热门强制重考，不受 rn 短路
                threads.append(t)
        read_delay = float(cfg.get("readDelaySec", 0.8) or 0)
        stat("threads", len(threads))
        for t in threads:
            # [2026-11-03] 自己发的帖子（搬运台搬过来的）里别人的评论，不去回。
            # 用户要求：自己发帖时不接帖内消息；回别人的帖子才正常回。
            if own_thread_ids() and str(t["tid"]) in own_thread_ids():
                continue
            # 0 回复的新帖也放行（否则永远抢不到首楼）
            if (t["reply_num"] < 1 and not t.get("fresh")) or SKIP_TITLE.search(t["title"] or ""):
                continue
            if str(t["tid"]) in done_tids:
                continue
            key = f"{forum}:{t['tid']}"
            prev = seen.get(key)
            # 回复数没变 → 和上轮读到的状态一样，不必再读（省请求、避免 429）
            # 但手动模式里来自「新帖/热门」的，强制重考一次
            if (not t.get("force") and isinstance(prev, dict)
                    and prev.get("rn") is not None and prev.get("rn") == t["reply_num"]):
                continue
            try:
                if read_delay:
                    await asyncio.sleep(read_delay)     # 读帖限速，避免 429
                _t = time.time()
                posts = await tieba_read.thread_posts(t["tid"], 8)
                stat("reads")
                stat("read_sec", time.time() - _t)
            except Exception as e:
                print(f"  帖 {t['tid']} 读失败：{e}")
                await asyncio.sleep(5)                  # 被限流就多等一会儿
                continue
            last_pid = posts[-1]["pid"] if posts else 0
            if _pid_of(prev) == last_pid and not t.get("force"):
                # 没新楼层也记下回复数，下一轮就不用再读这帖
                seen[key] = {"pid": last_pid, "rn": t.get("reply_num")}
                continue
            s = score(t["title"], posts)
            if s < 1:
                seen[key] = {"pid": last_pid, "rn": t.get("reply_num")}
                continue
            cands.append({"forum": forum, "thread": t, "posts": posts, "last_pid": last_pid, "key": key, "score": s})
    return cands


async def run_once(cfg: dict, seen: dict, manual: bool = False) -> int:
    persona = draft_mod.load_persona(cfg["persona"])
    today = local_now().strftime("%Y-%m-%d %H:%M")
    daily_cap = max(1, cfg.get("maxPostsPerDay", 3))
    posted_today = store.stats().get("postedToday", 0)
    review_mode = bool((cfg.get("auto") or {}).get("review", False))
    over_cap = posted_today >= daily_cap
    if over_cap and not review_mode:
        print(f"[今日已发 {posted_today}/{daily_cap}] 不再发")
        return 0
    if over_cap:
        # 审核模式：发多少由人点，上限只挡"发"，不挡"出草稿"
        print(f"[今日已发 {posted_today}/{daily_cap}] 已达上限，本轮只出草稿、不发")
    reason = posting_policy.check(cfg)
    if reason and not review_mode:
        print('[频控] ' + reason)
        return 0
    # 手动运行：一次出够 manualDrafts 条（默认 6），否则按 maxPerRun
    run_cap = (max(1, int(cfg.get("manualDrafts", 6) or 6)) if manual
               else max(1, cfg.get("maxPerRun", 1)))
    lo, hi = (cfg.get("replyDelaySec") or [180, 900])[:2]
    per_forum = {f: 0 for f in cfg["forums"]}
    STATE.mkdir(exist_ok=True)

    reset_stats()
    cands = await collect(cfg, seen, manual=manual)
    stat("cands", len(cands))
    cands.sort(key=lambda c: -c["score"])
    made = 0
    for c in cands:
        if made >= run_cap:
            break
        try:
            ptype, meta = posttype.classify(c["thread"]["title"], c["posts"])
            strategy = meta["strategy"]
            op_text = (c["posts"][0].get("text") or "").strip() if c["posts"] else ""
            if strategy == "skip":
                stat("skip_strategy")
                mark_seen(seen, c)
                continue
            # 常态沉默：符合条件的帖子里大部分也不回
            # 手动运行用单独的 manualSilenceRate（默认 0.35，比自动的 0.65 低，
            # 既保留随机性、又能凑够 manualDrafts 条）；设成 0 就是完全不沉默
            rate = float(cfg.get("manualSilenceRate", 0.35) if manual
                         else cfg.get("silenceRate", 0.65))
            if random.random() < rate:
                stat("skip_silence")
                mark_seen(seen, c)
                continue
            form, max_chars = forms.pick_form(c["forum"])
            # 纯图 / 无正文：只允许极短形态
            if not op_text and form not in ("digit", "c1_3"):
                form, max_chars = "c1_3", 3
            cons = posttype.consensus(c["posts"])
            msgs = draft_mod.build_messages(persona, c["forum"], c["thread"]["title"], c["posts"], today,
                                            ptype=ptype, strategy=strategy, consensus=cons,
                                            vision_cfg=cfg.get("vision"), form=form, max_chars=max_chars)
            stat("model_calls")
            raw = draft_mod.call_model(cfg, msgs)
            # [2026-11-03] 楼中楼：模型要回楼里某一层时会在最后写 TARGET: 楼层号。
            # 摘出这一行、换成该层的 pid，发帖时传给 reply()，回复就会挂在那一层下面。
            target_pid = None
            import re as _re
            m = _re.search(r"(?im)^\s*TARGET\s*[:：]\s*(\d+)\s*$", raw or "")
            if m:
                want = int(m.group(1))
                raw = _re.sub(r"(?im)^\s*TARGET\s*[:：]\s*\d+\s*$", "", raw or "").strip()
                for p in c["posts"]:
                    if int(p.get("floor") or -1) == want:
                        target_pid = int(p.get("pid"))
                        break
            existing = [p.get("text") or "" for p in c["posts"][:12]]
            ok, text = draft_mod.validate(raw, cfg, ptype=ptype, form=form, max_chars=max_chars,
                                          existing=existing, op_text=op_text)
        except Exception as e:
            print(f"  帖 {c['thread']['tid']} 生成失败：{e}")
            mark_seen(seen, c)
            continue
        if not ok:
            stat("rejected")
            if text != "SKIP":
                print(f"  帖 {c['thread']['tid']}（{ptype}/{form}）草稿被打回：{text}")
            mark_seen(seen, c)
            continue
        rec = {"at": today, "forum": c["forum"], "tid": c["thread"]["tid"],
               "title": c["thread"]["title"], "last_pid": c["last_pid"], "reply": text,
               "kind": "reply",
               "ptype": ptype, "form": form,
               "mode": "dry-run" if cfg.get("dryRun", True) else "write"}
        if not cfg.get("dryRun", True) and not over_cap:
            import tieba_post
            res = posting_policy.publish(cfg, c["thread"]["tid"], text, c["forum"], pid=target_pid)
            rec["target_pid"] = target_pid
            rec["target_floor"] = next((p.get("floor") for p in c["posts"] if int(p.get("pid") or 0) == int(target_pid or 0)), None)
            rec["status"] = "posted" if res.get("ok") else "failed"
            rec["post_error"] = "" if res.get("ok") else str(res.get("error") or res.get("no"))
            print(f"[发布] {c['forum']} / {c['thread']['title'][:20]} → {'成功' if res.get('ok') else '失败：' + rec['post_error']}")
            store.append(rec)
            if not res.get("ok"):
                continue
            mark_seen(seen, c)
            made += 1
            if made < run_cap:
                wait = random.randint(int(lo), int(hi))
                print(f"[间隔] 等 {wait // 60} 分 {wait % 60} 秒再发下一条")
                await asyncio.sleep(wait)
            continue
        store.append(rec)
        mark_seen(seen, c)
        made += 1
        print(f"[草案] {c['forum']} / {c['thread']['title'][:22]} → {text}")
    stat("made", made)
    print(stats_line())
    save_seen(seen)
    return made


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--manual", action="store_true",
                    help="人工点运行：扩大候选池（含热门）、不沉默、一次出够 N 条")
    args = ap.parse_args()
    cfg = load_cfg()
    seen = load_seen()
    while True:
        n = await run_once(cfg, seen, manual=args.manual)
        print(f"[完成] 本轮 {n} 条草稿 → state/drafts.jsonl")
        if args.once:
            return
        await asyncio.sleep(cfg.get("pollSeconds", 600))


def acquire_run_lock():
    """单实例锁：调度器和人工点击可能同时起火，两个进程一起写 drafts.jsonl
    会在 Windows 上撞 WinError 5。拿不到锁就直接退出。"""
    lock = STATE / "run.lock"
    STATE.mkdir(exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return lock
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                age = 0
            if age > 1800:          # 超过 30 分钟视为残留锁
                try:
                    lock.unlink()
                except OSError:
                    pass
                continue
            return None
    return None


async def main_guarded() -> None:
    lock = acquire_run_lock()
    if lock is None:
        print("[跳过] 已经有一轮在跑（state/run.lock 存在），本次不出草稿")
        return
    try:
        await main()
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    asyncio.run(main_guarded())
