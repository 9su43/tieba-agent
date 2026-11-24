"""贴吧 agent 控制台（127.0.0.1:3190）。

能力：
- 自动运行：按活跃时段自己跑；审核模式下只出草稿，关掉审核就直接发
- 开关：能不能说话、暂停 X 小时
- 强度：1 保守 / 2 正常 / 3 活跃（控制每天条数和最小间隔）
- 看动态：已发的每一条（含回执、失败原因）
- 知识库：一键增量更新（只增不删）
"""
from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CONSOLE = ROOT / "console"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(CONSOLE))

from settings import local_now
import settings
import api as api_mod  # noqa: E402
import signin as signin_mod  # noqa: E402
import configuration
import posting_policy
import store  # noqa: E402
import tieba_post  # noqa: E402

PORT = settings.load()["console"]["port"]
TOKEN_FILE = STATE / "console-token.txt"
RUNTIME = STATE / "runtime.json"

INTENSITY = {1: {"maxPostsPerDay": 1, "minMinutesBetween": 120}, 2: {"maxPostsPerDay": 3, "minMinutesBetween": 40}, 3: {"maxPostsPerDay": 5, "minMinutesBetween": 20}}


# ---------- 配置与运行态 ----------

def token() -> str:
    STATE.mkdir(exist_ok=True)
    if not TOKEN_FILE.exists():
        TOKEN_FILE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def cfg_file() -> Path:
    f = ROOT / "config.json"
    return f if f.exists() else ROOT / "config.example.json"


def load_cfg() -> dict:
    return settings.load()


def save_cfg(cfg: dict) -> None:
    settings.save(cfg)


def load_runtime() -> dict:
    if RUNTIME.exists():
        try:
            return json.loads(RUNTIME.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"lastRunAt": "", "lastResult": "", "lastKbAt": ""}


def save_runtime(rt: dict) -> None:
    STATE.mkdir(exist_ok=True)
    RUNTIME.write_text(json.dumps(rt, ensure_ascii=False, indent=2), encoding="utf-8")


def in_hours(hours: list[str], now: datetime | None = None) -> bool:
    now = now or local_now()
    hm = now.strftime("%H:%M")
    for span in hours:
        try:
            a, b = [s.strip() for s in span.split("-")]
        except ValueError:
            continue
        if a <= b:
            if a <= hm <= b:
                return True
        else:  # 跨零点，如 22:00-02:00
            if hm >= a or hm <= b:
                return True
    return False


def run_py(script: str, *args: str) -> dict:
    p = subprocess.run([sys.executable, "-X", "utf8", str(ROOT / script), *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    return {"code": p.returncode, "out": (p.stdout or "")[-6000:], "err": (p.stderr or "")[-3000:]}


def run_once_agent(force_dry: bool | None = None, manual: bool = False) -> dict:
    cfg = load_cfg()
    auto = cfg["auto"]
    dry = auto["review"] if force_dry is None else force_dry
    cfg["dryRun"] = dry
    want = INTENSITY.get(int(auto.get("intensity", 2)), INTENSITY[2])
    pass  # Frequency is persisted explicitly; do not overwrite console values on every run.
    # 手动指定的每日上限优先于强度预设；清掉覆盖就回到强度预设
    override = auto.get("maxPostsPerDay")
    if isinstance(override, int) and override >= 1:
        cfg["maxPostsPerDay"] = int(override)
    save_cfg(cfg)
    args = ["src/main.py", "--once"] + (["--manual"] if manual else [])
    res = run_py(*args)
    rt = load_runtime()
    rt["lastRunAt"] = local_now().strftime("%Y-%m-%d %H:%M:%S")
    rt["lastResult"] = ("dry" if dry else "post") + ("/manual" if manual else "") + f" exit={res['code']}"
    save_runtime(rt)
    return res


# ---------- 调度线程 ----------

class Scheduler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.last_tick = 0.0
        self.last_log = ""
        self.signin_log = ""

    def tick(self) -> None:
        cfg = load_cfg()
        # [2026-11-04] 每日签到：跟发帖开关无关，只要开着就签；一天只签一次（signin.run 自己判日期）。
        try:
            r = signin_mod.run()
            if not r.get("skipped"):
                self.signin_log = (f"签到完成：新签 {len(r.get('signed') or [])}、"
                                   f"已签 {len(r.get('already') or [])}、失败 {len(r.get('failed') or {})}")
            elif r.get("detail"):
                self.signin_log = f"签到：{r['detail']}"
        except Exception as e:
            self.signin_log = f"签到出错：{str(e)[:80]}"
        auto = cfg["auto"]
        rt = load_runtime()
        interval = max(60, int(cfg.get("pollSeconds", 900)))
        now = local_now()

        if not auto.get("enabled", True):
            self.last_log = "已关闭（不发言）"
            return
        if auto.get("pausedUntil"):
            try:
                if now < datetime.fromisoformat(auto["pausedUntil"]):
                    self.last_log = f"暂停到 {auto['pausedUntil']}"
                    return
            except ValueError:
                pass
        if not in_hours(cfg.get("activeHours") or []):
            self.last_log = "不在活跃时段"
            return
        last = rt.get("lastRunAt")
        if last:
            try:
                if (now - datetime.fromisoformat(last)).total_seconds() < interval:
                    self.last_log = "冷却中"
                    return
            except ValueError:
                pass
        self.last_log = "运行中…"
        res = run_once_agent()
        self.last_log = f"完成 exit={res['code']}"

    def run(self) -> None:
        import random
        while True:
            try:
                self.tick()
            except Exception as e:
                self.last_log = f"出错：{e}"
            cfg = load_cfg()
            base = max(60, int(cfg.get("pollSeconds", 900)))
            time.sleep(int(base * random.uniform(0.8, 1.3)))


SCHED = Scheduler()


# ---------- 搬运（repost）数据 ----------
# 2026-11-03 新增：搬家台页面用的数据。帖子列表来自 state/repost.log，
# 浏览/回复/点赞用 aiotieba 现读 5soul 吧最新帖（缓存 60 秒，避免每刷一次都去打接口）。

REPOST_LOG = STATE / "repost.log"
REPOST_HEARTBEAT = STATE / "repost.heartbeat"
_repost_cache = {"at": 0.0, "rows": [], "stats": {}}


def _repost_entries() -> list[dict]:
    rows = []
    if not REPOST_LOG.exists():
        return rows
    for line in REPOST_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
        i = line.find("{")
        if i < 0:
            continue
        try:
            d = json.loads(line[i:])
        except Exception:
            continue
        if isinstance(d, dict) and d.get("item"):
            rows.append(d)
    return rows


# 统计刷新与页面渲染分开，所有访问共用同一个去重队列。
from repost_stats import PostStats, sort_posts, post_time

def _fetch_tid_stats(tids):
    import asyncio
    from aiotieba import Client
    try:
        ck = signin_mod.browser_cookies()
    except Exception:
        ck = {}
    async def run():
        out = {}
        async with Client(ck.get('BDUSS', ''), ck.get('STOKEN', '')) as client:
            sem = asyncio.Semaphore(3)
            async def one(tid):
                async with sem:
                    try:
                        posts = await asyncio.wait_for(client.get_posts(tid, pn=1), 12)
                        th = getattr(posts, 'thread', None)
                        if th is not None and int(getattr(th, 'tid', 0) or 0) == tid:
                            out[tid] = {'view': int(getattr(th, 'view_num', 0) or 0),
                                        'reply': int(getattr(th, 'reply_num', 0) or 0),
                                        'agree': int(getattr(th, 'agree', 0) or 0),
                                        'title': str(getattr(th, 'title', '') or '')}
                    except Exception:
                        pass
                    await asyncio.sleep(.25)
            await asyncio.gather(*(one(tid) for tid in tids))
        return out
    return asyncio.run(run())

_STATS = PostStats(STATE / 'tid-stats.json', _fetch_tid_stats)

def _repost_stats(tids=None, max_fetch=40):
    tids = list(dict.fromkeys(int(t) for t in (tids or []) if t))
    _STATS.request(tids, foreground=True)
    return _STATS.snapshot(tids)

def repost_payload(page=1, size=40, sort='at', order='desc'):
    from zoneinfo import ZoneInfo
    cfg=load_cfg(); zone=ZoneInfo(cfg['timezone'])
    rc = cfg.get('repost') or {}
    by_tid = {}
    for d in _repost_entries():
        match = re.search(r'/p/(\d+)', str(d.get('url') or ''))
        if not d.get('ok') or not match:
            continue
        tid = int(match.group(1))
        by_tid[tid] = {'at': d.get('at'), 'forum': str(d.get('forum') or ''),
                       'title': str(d.get('title') or ''), 'url': d['url'],
                       'ok': True, 'imgs': int(d.get('imgs') or 0), 'tid': tid}
    all_rows = list(by_tid.values())
    latest = max((post_time(r.get('at'), tz=zone) for r in all_rows), default=0)
    tids = list(by_tid)
    stats = _STATS.snapshot(tids)
    ordered = sort_posts(all_rows, stats, sort, order, timezone=zone)
    total = len(ordered)
    size = max(10, min(100, int(size or 40)))
    pages = max(1, (total + size - 1) // size)
    page = max(1, min(pages, int(page or 1)))
    rows = ordered[(page - 1) * size:page * size]
    _STATS.request([r['tid'] for r in rows], foreground=True)
    for row in rows:
        value = stats[row['tid']]
        title = row['title']
        row.update({k: v for k, v in value.items() if k != 'title'})
        row['title'] = title or value.get('title') or f"（{row['tid']}）"
    totals = {'view': 0, 'reply': 0, 'agree': 0, 'covered': 0, 'fresh': 0, 'total': total}
    timestamps = []
    for value in stats.values():
        if 'view' not in value:
            continue
        totals['covered'] += 1
        totals['fresh'] += int(value['statsStatus'] == 'fresh')
        if value['updatedAt']:
            timestamps.append(value['updatedAt'])
        for key in ('view', 'reply', 'agree'):
            totals[key] += int(value[key])
    totals['averageView'] = round(totals['view'] / totals['covered']) if totals['covered'] else None
    totals['oldestUpdate'] = min(timestamps, default=None)
    totals['latestUpdate'] = max(timestamps, default=None)
    now = local_now()
    interval = int(rc.get('intervalMinutes', 30))
    nxt = latest + interval * 60
    enabled = rc.get('enabled') is True
    operation = configuration.control.status()
    running = enabled and REPOST_HEARTBEAT.exists() and time.time() - REPOST_HEARTBEAT.stat().st_mtime < max(600, interval * 120)
    return {'config': rc, 'rows': rows, 'page': page, 'pages': pages, 'size': size, 'total': total,
            'totals': totals, 'sort': sort if sort in ('at', 'view', 'reply', 'agree') else 'at',
            'order': 'asc' if order == 'asc' else 'desc', 'refreshSeconds': 5,
            'status': {'enabled': enabled, 'operation': operation, 'today': sum(datetime.fromtimestamp(post_time(r.get('at'), tz=zone), tz=zone).date() == now.date() for r in all_rows),
                       'running': running, 'nextAt': '已停止' if not enabled else ('现在' if time.time() >= nxt else datetime.fromtimestamp(nxt, tz=zone).strftime('%H:%M')),
                       'now': now.strftime('%Y-%m-%d %H:%M:%S')}}


class Handler(BaseHTTPRequestHandler):
    server_version = "TiebaConsole/1.0"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    # ---------- 基础 ----------
    def _json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, name: str):
        p = ROOT / "console" / name
        if not p.exists():
            return self._json({"error": "not found"}, 404)
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self, q) -> bool:
        tok = (urllib.parse.parse_qs(q.query).get("token") or [""])[0]
        if secrets.compare_digest(tok or self.headers.get("Authorization", "").removeprefix("Bearer "), token()):
            return True
        self._json({"error": "unauthorized"}, 401)
        return False

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 1024 * 1024: raise ValueError("请求过长")
            return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        if q.path == "/settings":
            return self._html("settings.html")
        if q.path in ("/", "/index.html"):
            return self._html("index.html")
        if q.path in ("/repost", "/repost.html"):
            return self._html("repost.html")
        if q.path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if not self._allowed(q):
            return
        if q.path == "/api/settings":
            return self._json(configuration.view())
        if q.path == "/api/persona":
            return self._json(configuration.persona())
        if q.path == "/api/status":
            cfg = load_cfg()
            profiles = []
            try:
                profiles = api_mod.list_profiles()
            except Exception:
                profiles = []
            pid = str((cfg.get("api") or {}).get("profile") or "")
            source, model = "", str((cfg.get("model") or {}).get("model") or "")
            try:
                cur = api_mod.resolve_by_id(pid, model) or {}
                source = cur.get("source") or ""
                model = cur.get("model") or model
            except Exception:
                pass
            if not source:
                hit = next((p for p in profiles if p.get("id") == pid), None)
                source = (hit or {}).get("name") or pid or "未配置"
            return self._json({
                "config": cfg,
                "runtime": load_runtime(),
                "scheduler": SCHED.last_log,
                "signin": SCHED.signin_log,
                "stats": _store_stats(),
                "inHours": in_hours(cfg.get("activeHours") or []),
                "profiles": profiles,
                "api": {"profile": pid, "source": source, "model": model},
            })
        if q.path == "/api/drafts":
            rows = _drafts_rows()
            return self._json({"rows": rows})
        if q.path == "/api/feed":
            return self._json({"rows": _feed_rows()})
        if q.path == "/api/kb":
            return self._json(_kb_payload())
        if q.path == "/api/repost":
            qs = urllib.parse.parse_qs(q.query)
            page = int((qs.get("page") or ["1"])[0] or 1)
            size = int((qs.get("size") or ["40"])[0] or 40)
            sort = (qs.get("sort") or [""])[0]
            order = (qs.get("order") or ["desc"])[0]
            return self._json(repost_payload(page, size, sort, order))
        if q.path == "/api/signin":
            return self._json(signin_mod.status())
        return self._json({"error": "not found"}, 404)

    # ---------- POST ----------
    def do_POST(self):
        q = urllib.parse.urlparse(self.path)
        if not self._allowed(q):
            return
        body = self._body()
        try:
            if q.path == '/api/repost/control': return self._json(configuration.repost_control(body))
            if q.path == '/api/settings': return self._json(configuration.configure(body))
            if q.path == '/api/profiles': return self._json(api_mod.save_profile(body))
            if q.path == '/api/account': return self._json(configuration.account(body))
            if q.path == '/api/persona': return self._json(configuration.persona(body))
            if q.path.startswith('/api/browser/'): return self._json(configuration.browser(q.path.rsplit('/',1)[1]))
            if q.path == "/api/auto":
                return self._json(_save_auto(body))
            if q.path == "/api/run":
                res = run_once_agent(bool(body.get("dry", True)), manual=bool(body.get("manual", True)))
                return self._json({"ok": res.get("code") == 0, **res})
            if q.path == "/api/approve":
                return self._json(_approve(int(body.get("index", 0))))
            if q.path == "/api/discard":
                return self._json(_discard(int(body.get("index", 0))))
            if q.path == "/api/sign":
                res = run_py("src/signin.py", "--force")
                return self._json({"ok": res.get("code") == 0, **res})
            if q.path == "/api/signin":
                return self._json(signin_mod.run(force=True))
            if q.path == "/api/kb-update":
                # [2026-11-13] 原来这里跑的是 src/kb.py——那个文件只有函数、没有命令行入口，
                # 等于点一下按钮空转一次，所以 10-27 之后一条都没更新过。真正的更新器是 tools/update_kb.py。
                res = run_py("tools/update_kb.py", "--per-forum", str(int(body.get("count", 12))))
                if res.get("code") == 0:
                    rt = load_runtime(); rt["lastKbAt"] = local_now().strftime("%Y-%m-%d %H:%M:%S"); save_runtime(rt)
                return self._json({"ok": res.get("code") == 0, **res})
            if q.path == "/api/repost/config":
                return self._json(_save_repost(body))
            if q.path == "/api/repost/run":
                if configuration.control.stopped():
                    return self._json({"error":"搬运已停止，请先启动搬运"},400)
                (STATE / "repost-now.flag").write_text(str(int(time.time())), encoding="utf-8")
                return self._json({"ok": True, "detail": "已通知循环立刻搬一条"})
        except Exception as e:
            return self._json({"error": str(e)[:200]}, 500)
        return self._json({"error": "not found"}, 404)


# ---------- 控制台数据辅助 ----------

def _stats_warmer():
    """完整列表轮询入队，已在查的不会重查，错误有独立退避。"""
    while True:
        try:
            tids = []
            for row in _repost_entries():
                match = re.search(r'/p/(\d+)', str(row.get('url') or ''))
                if row.get('ok') and match:
                    tids.append(int(match.group(1)))
            _STATS.request(tids)
        except Exception:
            pass
        time.sleep(30)


def _repost_guard() -> None:
    while True:
        try: configuration.ensure_repost_worker()
        except Exception: pass
        time.sleep(15)


def _store_stats() -> dict:
    try:
        import store  # type: ignore
        return store.stats()
    except Exception:
        return {}


def _drafts_rows() -> list[dict]:
    try:
        import store  # type: ignore
        rows = store.all_rows()
    except Exception:
        return []
    out = []
    for i, r in enumerate(rows):
        out.append({"index": i, **r})
    return out[::-1][:60]


def _feed_rows() -> list[dict]:
    rows = [r for r in _drafts_rows() if r.get("status") == "posted"]
    return rows[::-1][:30]


def _kb_payload() -> dict:
    kb = ROOT / "knowledge" / "entries.jsonl"
    text = ""
    try:
        lines = kb.read_text(encoding="utf-8").splitlines()
        text = "\n".join(lines[-80:])
    except Exception:
        text = "（还没有知识条目）"
    return {"text": text, "runtime": load_runtime()}


def _save_auto(body: dict) -> dict:
    cfg = load_cfg()
    auto = cfg.setdefault("auto", {})
    for k in ("enabled", "review", "intensity", "profile", "pausedUntil"):
        if k in body:
            auto[k] = body[k]
    for k in ("activeHours", "forums", "pollSeconds", "maxPostsPerDay", "maxPerRun"):
        if k in body:
            cfg[k] = body[k]
    if body.get("pauseHours"):
        auto["pausedUntil"] = (local_now() + timedelta(hours=float(body["pauseHours"]))).strftime("%Y-%m-%d %H:%M")
    if body.get("resume"):
        auto["pausedUntil"] = ""
    if body.get("enabled") is False:
        auto["pausedUntil"] = ""
    if 'profile' in body: cfg['api']['profile']=str(body['profile'])
    if 'intensity' in body:
        cfg.update(INTENSITY[int(body['intensity'])])
    save_cfg(cfg)
    return {"ok": True, "config": cfg, "scheduler": SCHED.last_log}


def _save_repost(body: dict) -> dict:
    result=configuration.configure({'repost':body})
    return {'ok':True,'config':result['config']['repost']}


def _approve(index: int) -> dict:
    import store  # type: ignore
    import tieba_post  # type: ignore

    rows = store.all_rows()
    pending = [i for i, r in enumerate(rows) if r.get("status") == "pending"]
    if index < 0 or index >= len(pending):
        return {"ok": False, "detail": "没有这条待审草稿"}
    i = pending[len(pending) - 1 - index]
    r = rows[i]
    res = posting_policy.publish(load_cfg(), int(r.get("tid")), str(r.get("reply") or ""), str(r.get("forum") or ""),
                           pid=r.get("target_pid"))
    if res.get("ok"):
        store.update(i, status="posted", posted_at=local_now().strftime("%Y-%m-%d %H:%M"))
        return {"ok": True}
    return {"ok": False, "detail": res}


def _discard(index: int) -> dict:
    import store  # type: ignore
    rows = store.all_rows()
    pending = [i for i, r in enumerate(rows) if r.get("status") == "pending"]
    if index < 0 or index >= len(pending):
        return {"ok": False, "detail": "没有这条待审草稿"}
    i = pending[len(pending) - 1 - index]
    store.update(i, status="discarded")
    return {"ok": True}



def main() -> None:
    STATE.mkdir(exist_ok=True)
    if not (ROOT / "config.json").exists(): save_cfg(load_cfg())
    SCHED.start()
    threading.Thread(target=_stats_warmer, daemon=True).start()
    threading.Thread(target=_repost_guard, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"控制台: http://127.0.0.1:{PORT}/?token={token()}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
