"""贴吧发帖/回帖（协议直发）。

需要 BDUSS：放 state/auth.json 的 {"bduss": "..."}，不要写进 config、不要进日志。
只用标准库，不额外装包。
"""
from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import http_util

ROOT = Path(__file__).resolve().parent.parent
AUTH = ROOT / "state" / "auth.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def load_bduss() -> str:
    if not AUTH.exists():
        raise SystemExit(f"缺少 {AUTH}，先写 {{\"bduss\": \"...\"}}")
    data = json.loads(AUTH.read_text(encoding="utf-8"))
    bduss = (data.get("bduss") or "").strip()
    if not bduss:
        raise SystemExit("auth.json 里没有 bduss")
    return bduss


def _request(url: str, data: bytes | None = None, referer: str = "https://tieba.baidu.com/"):
    headers = {
        "User-Agent": UA,
        "Cookie": f"BDUSS={load_bduss()}",
        "Referer": referer,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    # POST 不走代理兜底：失败重发可能造成重复发帖
    with http_util.open_url(req, timeout=30, stage="贴吧请求",
                            allow_fallback=(data is None)) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:300]}


def get_tbs() -> dict:
    """确认登录态并取 tbs（发帖必须带）。"""
    return _request("https://tieba.baidu.com/dc/common/tbs")


def reply(tid: int, content: str, kw: str, pid: int | None = None) -> dict:
    """回一个帖子。kw 是所在吧名；pid 不为空时回该楼层（楼中楼）。"""
    tbs = (get_tbs() or {}).get("tbs") or ""
    if not tbs:
        return {"ok": False, "error": "拿不到 tbs（登录态失效？）"}
    fields = {
        "ie": "utf-8",
        "kw": kw,
        "tid": str(tid),
        "content": content,
        "tbs": tbs,
        "use_vcode": "0",
        "vcode": "",
        "vcode_md5": "",
        "is_ad": "0",
        "subapp_type": "tb_web",
        "fr": "",
    }
    if pid:
        fields["pid"] = str(pid)
        fields["is_feedback"] = "1"
    form = urllib.parse.urlencode(fields).encode("utf-8")
    res = _request(f"https://tieba.baidu.com/f/commit/post/add", data=form,
                   referer=f"https://tieba.baidu.com/p/{tid}")
    no = res.get("no")
    ok = no in (0, "0")
    # [2026-11-05] 8010/210009 是接口侧的拒绝（发太勤/触发风控/要验证），帖子通常没发出去。
    # 等一会儿重试一次；仍失败就把人话写进 error，控制台能看懂。
    if not ok and str(no) in ("8010", "210009"):
        import time
        time.sleep(20)
        retry = _request(f"https://tieba.baidu.com/f/commit/post/add", data=form,
                         referer=f"https://tieba.baidu.com/p/{tid}")
        no2 = retry.get("no")
        if no2 in (0, "0"):
            return {"ok": True, "no": 0, "error": "", "raw": None, "retried": True}
        hint = "需要验证码（去浏览器手动回一条）" if str(no2) == "210009" else "接口拒绝（发太勤或触发审核）"
        return {"ok": False, "no": no2, "need_vcode": str(no2) == "210009",
                "error": f"{no2} {hint}", "raw": retry}
    return {"ok": ok, "no": no, "error": res.get("error") or res.get("err_msg") or "",
            "raw": None if ok else res}


def new_thread(kw: str, title: str, content: str) -> dict:
    """发一个新主题。

    先走协议接口；被风控挡下（本号实测 210009）时自动改走浏览器网页版
    （tieba_browser，需要 kimi-webbridge + 浏览器已登录贴吧）。
    """
    tbs = (get_tbs() or {}).get("tbs") or ""
    res: dict = {}
    if tbs:
        form = urllib.parse.urlencode({
            "ie": "utf-8", "kw": kw, "title": title, "content": content,
            "tbs": tbs, "use_vcode": "0", "vcode": "", "vcode_md5": "",
            "is_ad": "0", "subapp_type": "tb_web",
        }).encode("utf-8")
        res = _request("https://tieba.baidu.com/f/commit/thread/add", data=form,
                       referer=f"https://tieba.baidu.com/f?kw={urllib.parse.quote(kw)}")
        if res.get("no") in (0, "0"):
            return {"ok": True, "no": 0, "error": "", "raw": None, "via": "api"}

    # 接口被拦 → 浏览器兜底
    api_err = res.get("error") or res.get("no") or ("没有 tbs" if not tbs else "")
    try:
        import tieba_browser
        if tieba_browser.available():
            br = tieba_browser.new_thread(kw, title, content)
            return {"ok": bool(br.get("ok")), "no": None, "via": "browser",
                    "need_vcode": br.get("need_vcode", False),
                    "error": "" if br.get("ok") else str(br.get("detail") or ""),
                    "raw": {"api_first": res, "browser": br}}
    except Exception as e:
        return {"ok": False, "no": res.get("no"), "via": "api",
                "error": f"{api_err}；浏览器兜底也失败：{type(e).__name__}: {str(e)[:120]}",
                "raw": res}
    return {"ok": False, "no": res.get("no"), "via": "api",
            "error": str(api_err), "raw": res}


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] == "check":
        print(json.dumps(get_tbs(), ensure_ascii=False)[:300])
        return
    if args[0] == "reply" and len(args) >= 4:
        print(json.dumps(reply(int(args[1]), args[3], args[2]), ensure_ascii=False))
        return
    print("用法：\n  python src/tieba_post.py check\n  python src/tieba_post.py reply <tid> <吧名> <内容>")


if __name__ == "__main__":
    main()
