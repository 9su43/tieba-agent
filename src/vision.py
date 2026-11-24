"""看图：把贴吧图片交给视觉模型，得到一句描述。

只用来"知道图里是什么"，不评价、不解读立场。
结果按图片 URL 缓存，避免重复花钱。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
CACHE = STATE / "vision-cache.json"

PROMPT = ("用一句中文说清楚这张图是什么：图里有什么、写的什么字、是什么梗或哪来的截图。"
          "只描述客观内容，不要评价，不要分析，不超过 60 字。")

# 系统代理（如 127.0.0.1:7897）会把国内图床和国内 API 带出国再回来，
# 长连接常被中途掐断（Remote end closed connection）。统一走 http_util：优先直连。
from http_util import open_url as _open  # noqa: E402


def _cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def _save(c: dict) -> None:
    STATE.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")


def _mime(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _to_jpeg(raw: bytes) -> bytes:
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(raw)).convert("RGB")
        im.thumbnail((1400, 1400))
        out = BytesIO()
        im.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return raw


_FAIL_UNTIL = 0.0   # 熔断：视觉接口不可用时，本轮/半小时内不再尝试，避免每条帖卡满超时


def _cool(err: str) -> None:
    global _FAIL_UNTIL
    _FAIL_UNTIL = time.time() + 1800
    print(f"    [看图熔断] {err[:80]} → 30 分钟内不再看图")


def describe(url: str, cfg: dict) -> str:
    if not url:
        return ""
    if time.time() < _FAIL_UNTIL:
        return ""
    c = _cache()
    if url in c:
        return c[url]
    import api as api_mod
    vc = (cfg.get("vision") or {})
    if not vc.get("enabled"):
        return ""
    conf = api_mod.resolve_vision(vc)
    if not conf.get("key"):
        return ""
    vtimeout = int(vc.get("timeoutSec") or 45)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0.0.0 Safari/537.36",
        "Referer": "https://tieba.baidu.com/",
    })
    with _open(req, 60, "下载图片") as r:
        raw = r.read()
    mime = _mime(raw)
    if not mime:
        raw = _to_jpeg(raw)
        mime = "image/jpeg"
    b64 = base64.b64encode(raw).decode()
    payload = {
        "model": conf["model"],
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        "max_tokens": 400,
        "temperature": 0.3,
        "thinking": {"type": "disabled"},
    }
    body = json.dumps(payload).encode("utf-8")
    rq = urllib.request.Request(
        api_mod.chat_url(conf["endpoint"]),
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + conf["key"]},
        method="POST",
    )
    with _open(rq, vtimeout, "调视觉模型") as r:
        data = json.loads(r.read().decode("utf-8"))
    text = (data["choices"][0]["message"].get("content") or "").strip()
    bad = ("无法查看", "未提供图像", "看不到", "抱歉", "我无法", "请提供图片", "没有图片")
    if text and not any(b in text for b in bad):
        c[url] = text
        _save(c)
        return text
    return ""


def describe_many(urls: list[str], cfg: dict, limit: int = 2) -> list[str]:
    out = []
    for u in (urls or [])[:limit]:
        if time.time() < _FAIL_UNTIL:
            break
        try:
            d = describe(u, cfg)
            if d:
                out.append(d)
        except Exception as e:
            _cool(f"{type(e).__name__}: {e}")
    return out
