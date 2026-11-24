"""Shared posting quotas for automatic replies and manual draft approvals."""
import json
from datetime import datetime
import file_lock
import settings

def in_hours(hours, now=None):
    hm=(now or settings.local_now()).strftime("%H:%M")
    return any((a<=hm<=b if a<=b else hm>=a or hm<=b) for a,b in (span.split("-") for span in hours))

def _ledger():
    path=settings.ROOT/"state"/"reply-posting-ledger.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

def check(cfg, rows=None, now=None):
    now=now or settings.local_now();rows=_ledger() if rows is None else rows
    if not in_hours(cfg["activeHours"],now):return "不在回复活跃时段"
    dates=[]
    for r in rows:
        try:dates.append(datetime.fromisoformat(r["at"]))
        except (KeyError,ValueError,TypeError):continue
    if sum(t.date()==now.date() for t in dates)>=cfg["maxPostsPerDay"]:return "今天回复已达到上限"
    if sum(0<=(now-t).total_seconds()<3600 for t in dates)>=cfg["maxRepliesPerHour"]:return "本小时回复已达到上限"
    if any(0<=(now-t).total_seconds()<cfg["minMinutesBetween"]*60 for t in dates):return "未达到回复最小间隔"
    return ""

def publish(cfg,tid,content,forum,pid=None):
    if forum not in cfg["forums"]:return {"ok":False,"error":"目标吧不在回复活动列表中"}
    import tieba_post
    lock=file_lock.acquire(settings.ROOT/"state"/"reply-posting.lock",blocking=True)
    try:
        rows=_ledger();now=settings.local_now();reason=check(cfg,rows,now)
        if reason:return {"ok":False,"error":reason}
        # Persist a reservation before POST. An ambiguous network failure must not cause quota bypass.
        rows=[r for r in rows if str(r.get("at", ""))[:10]>=now.strftime("%Y-%m-%d")]
        rows.append({"at":now.isoformat(timespec="seconds"),"tid":str(tid),"status":"pending"})
        settings.atomic_json(settings.ROOT/"state"/"reply-posting-ledger.json",rows)
        try:result=tieba_post.reply(tid,content,forum,pid=pid)
        except Exception:
            rows[-1]["status"]="unknown";settings.atomic_json(settings.ROOT/"state"/"reply-posting-ledger.json",rows)
            raise
        rows[-1]["status"]="sent" if result.get("ok") else "failed"
        # Known failures do not consume the daily quota, but keep an audit record separately.
        if not result.get("ok"):rows.pop()
        settings.atomic_json(settings.ROOT/"state"/"reply-posting-ledger.json",rows)
        return result
    finally:lock.close()
