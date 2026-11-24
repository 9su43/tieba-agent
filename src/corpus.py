"""语料检索：从抓到的贴吧原文和知识库里，取"真实说话样本"。

用途：生成回复时把真实帖子片段塞进上下文，模型照着学语气，
而不是让提示词里的示范句（容易是我编的）去带节奏。
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "tieba_posts.jsonl"
KB = ROOT / "knowledge" / "entries.jsonl"

_cache: dict[str, list[dict]] = {}


def _load() -> dict[str, list[dict]]:
    if _cache:
        return _cache
    if not DATA.exists():
        return _cache
    for line in DATA.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        _cache.setdefault(r.get("forum", ""), []).append(r)
    return _cache


def _clean(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t


BLOCK = ("盒", "开盒", "中之人", "真名", "身份证", "手机号", "地址", "死", "自杀", "维人", "领导人")


def real_quotes(forum: str, k: int = 8, seed: int | None = None, max_len: int = 40) -> list[str]:
    """取该吧真实回复，偏向短句（像正常回帖）。"""
    rows = _load().get(forum) or []
    if not rows:
        return []
    rnd = random.Random(seed)
    pool: list[str] = []
    for r in rows:
        for p in r.get("posts", []):
            t = _clean(p.get("text"))
            if 2 <= len(t) <= max_len and not t.startswith("http") and not any(b in t for b in BLOCK):
                pool.append(t)
    rnd.shuffle(pool)
    seen, out = set(), []
    for t in pool:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= k:
            break
    return out


def kb_relevant(text: str, k: int = 6) -> list[str]:
    """按字面重合度挑知识库条目。"""
    if not KB.exists():
        return []
    target = set(re.findall(r"[\u4e00-\u9fa5]{2,}", text or ""))
    scored = []
    for line in KB.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        claim = e.get("claim") or ""
        words = set(re.findall(r"[\u4e00-\u9fa5]{2,}", claim))
        score = len(target & words)
        if score:
            scored.append((score, claim))
    scored.sort(reverse=True)
    return [c for _, c in scored[:k]]
