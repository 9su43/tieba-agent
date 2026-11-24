"""查当前登录贴吧账号的数字 uid（用于识别"哪些帖子是她自己发的"）。

做法：用 state/auth.json 里的 BDUSS 打开个人主页，从页面脚本里抠 user_id。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUTH = ROOT / "state" / "auth.json"
CACHE = ROOT / "state" / "self-uid.json"
ENC = ROOT / "state" / "self-encid.txt"


def _bduss() -> str:
    return json.loads(AUTH.read_text(encoding="utf-8"))["bduss"]


def _get(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
            "Cookie": "BDUSS=" + _bduss(),
            "Referer": "https://tieba.baidu.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def find_uid() -> int:
    if CACHE.exists():
        try:
            uid = int(json.loads(CACHE.read_text(encoding="utf-8")).get("uid") or 0)
            if uid:
                return uid
        except Exception:
            pass
    encid = ""
    if ENC.exists():
        encid = ENC.read_text(encoding="utf-8").strip()
    urls = ["https://tieba.baidu.com/f/user/json_userinfo"]
    if encid:
        urls.append(f"https://tieba.baidu.com/home/main?id={encid}")
    for url in urls:
        try:
            if url.endswith("json_userinfo"):
                text = _get(url)
            else:
                text = _get(url)
        except Exception as e:
            print(f"[whoami] {url} 打不开：{e}", file=sys.stderr)
            continue
        for pat in (r'"user_id"\s*:\s*"?(\d{5,12})', r"user_id=(\d{5,12})", r'"id"\s*:\s*(\d{5,12})'):
            m = re.search(pat, text)
            if m:
                uid = int(m.group(1))
                CACHE.write_text(json.dumps({"uid": uid, "from": url}, ensure_ascii=False), encoding="utf-8")
                return uid
    return 0


if __name__ == "__main__":
    print(json.dumps({"uid": find_uid()}, ensure_ascii=False))
