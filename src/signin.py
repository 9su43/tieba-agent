"""贴吧每日签到（2026-11-04 用户要求：让她以后每天自己签到）。

为什么不走 aiotieba 的账号配置：这个号在贴吧的登录态在**浏览器**里（发帖走 kimi-webbridge），
aiotieba 没有配 BDUSS。所以这里从浏览器里把 cookie 取出来（BDUSS/STOKEN，HttpOnly 也能拿到），
喂给 aiotieba 的 Client，再用 sign_forum 逐个吧签到。

每天只签一次：签完写 state/signin.json（date=北京时间的日期），当天再调直接跳过。
控制台调度线程每轮会顺手调一次 run()，签到失败/没签到都不会影响发帖。
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path


from settings import local_now

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
DONE_FILE = STATE / "signin.json"
ALREADY = 160002  # "您之前已经签过了"


def log(msg: str) -> None:
    print(f"[{local_now():%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, obj) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def config() -> dict:
    return load_json(ROOT / "config.json", {})


def forums_to_sign(cfg: dict) -> list[str]:
    si = cfg.get("signIn") or {}
    forums = [str(f).strip() for f in (si.get("forums") or []) if str(f).strip()]
    if not forums:
        forums = [str(f).strip() for f in (cfg.get("forums") or []) if str(f).strip()]
    return list(dict.fromkeys(forums))


def browser_cookies() -> dict:
    """从浏览器拿贴吧 cookie（BDUSS/STOKEN 都是 HttpOnly，JS 读不到，走 CDP）。"""
    sys.path.insert(0, str(ROOT / "src"))
    import tieba_browser  # noqa: E402

    r = tieba_browser._api("cdp", {"method": "Network.getCookies", "params": {"urls": ["https://tieba.baidu.com"]}}, timeout=25)
    cookies = ((r.get("data") or {}).get("cookies")) or []
    return {str(c.get("name")): str(c.get("value")) for c in cookies if c.get("name")}


def already_done_today() -> str:
    rec = load_json(DONE_FILE, {})
    return str(rec.get("date") or "")


def run(force: bool = False) -> dict:
    """签今天的到。返回 {ok, skipped, signed, already, failed, detail}。"""
    cfg = config()
    si = cfg.get("signIn") or {}
    if si.get("enabled") is False:
        return {"ok": True, "skipped": True, "detail": "签到未启用"}
    today = f"{local_now():%Y-%m-%d}"
    if not force and already_done_today() == today:
        return {"ok": True, "skipped": True, "detail": "今天已经签过"}

    at_hour = int(si.get("atHour", 8) or 0)
    if not force and local_now().hour < at_hour:
        return {"ok": True, "skipped": True, "detail": f"还没到 {at_hour}:00"}

    explicit = [str(f).strip() for f in ((si.get("forums") or [])) if str(f).strip()]
    forums = forums_to_sign(cfg)
    if not explicit and not forums:
        return {"ok": False, "detail": "没有配置要签到的吧"}

    try:
        cookies = browser_cookies()
    except Exception as e:
        log(f"[签到] 拿浏览器 cookie 失败：{e}")
        return {"ok": False, "detail": f"拿不到登录态：{str(e)[:80]}"}
    bduss, stoken = cookies.get("BDUSS", ""), cookies.get("STOKEN", "")
    if not bduss:
        log("[签到] 浏览器里没有 BDUSS（可能没登录贴吧）")
        return {"ok": False, "detail": "浏览器里没有登录态"}

    async def sign_all() -> dict:
        from aiotieba import Client

        out = {"signed": [], "already": [], "failed": {}}
        async with Client(bduss, stoken) as c:
            targets = list(forums)
            if not explicit:
                # [2026-11-21] 用户要求：账号里已关注的吧都要保持签到
                try:
                    followed = await c.get_self_follow_forums()
                    names = [str(getattr(x, 'fname', '') or '').strip() for x in (getattr(followed, 'objs', None) or [])]
                    names = [n for n in names if n]
                    if names:
                        targets = list(dict.fromkeys(names))
                        log(f"[签到] 使用账号关注的 {len(targets)} 个吧")
                except Exception as e:
                    log(f"[签到] 取关注列表失败，退回配置里的吧：{e}")
            for f in targets:
                try:
                    r = await c.sign_forum(f)
                    # 成功时 err 是 None；已签/失败时 err 是 TiebaServerError（有 .code/.msg）
                    err = getattr(r, "err", None)
                    code = int(getattr(err, "code", 0) or 0) if err is not None else 0
                    msg = str(getattr(err, "msg", "") or "")
                    if code == 0:
                        out["signed"].append(f)
                    elif code == ALREADY:
                        out["already"].append(f)
                    else:
                        out["failed"][f] = f"{code} {msg[:40]}"
                except Exception as e:
                    out["failed"][f] = str(e)[:80]
                await asyncio.sleep(0.6)
        return out

    try:
        res = asyncio.run(sign_all())
    except Exception as e:
        log(f"[签到] 失败：{e}")
        return {"ok": False, "detail": str(e)[:120]}

    ok = not res["failed"]
    save_json(DONE_FILE, {"date": today, "at": local_now().isoformat(timespec="seconds"),
                          "source": "browser-cookies", **res})
    log(f"[签到] 新签 {len(res['signed'])} 个、原本已签 {len(res['already'])} 个、失败 {len(res['failed'])} 个"
        + (f"：{res['failed']}" if res['failed'] else ""))
    return {"ok": ok, "skipped": False, "detail": "签到完成", **res}


def status() -> dict:
    cfg = config()
    rec = load_json(DONE_FILE, {})
    return {
        "enabled": (cfg.get("signIn") or {}).get("enabled") is not False,
        "atHour": int((cfg.get("signIn") or {}).get("atHour", 8) or 0),
        "forums": forums_to_sign(cfg),
        "today": f"{local_now():%Y-%m-%d}",
        "todayDone": str(rec.get("date") or "") == f"{local_now():%Y-%m-%d}",
        "last": rec or None,
    }


if __name__ == "__main__":
    force_flag = "--force" in sys.argv
    print(json.dumps(run(force=force_flag), ensure_ascii=False, indent=1))
