"""回复形态抽样 + AI 味过滤器（补丁 v2 第三部分）。

要点：
- 长度由程序决定（抽样形态），不让模型自己选；
- 与楼主/本楼已有回复相似度过高 → 判 echo；
- 高危句式命中 → 重写；WARN 只告警。
"""
from __future__ import annotations

import random
import re

FORMS = {
    "asoul": [("digit", .08), ("c1_3", .20), ("c4_8", .28), ("c9_15", .22), ("c16_30", .17), ("c31_60", .05)],
    "a不灭": [("c1_3", .10), ("c4_8", .25), ("c9_15", .30), ("c16_30", .25), ("c31_60", .10)],
    "5soul": [("digit", .06), ("c1_3", .16), ("c4_8", .34), ("c9_15", .26), ("c16_30", .18)],
}
MAXC = {"digit": 4, "c1_3": 3, "c4_8": 8, "c9_15": 15, "c16_30": 30, "c31_60": 60}

CHANT_OK = {"1", "3", "6", "666", "dd", "辛苦了", "没a不玩", "确实", "还真是"}

BAN = [
    r"(但|不过|然而|只是).{1,12}(吧|呢|而已)$",
    r"有点东西|有那味|说到点子上|一针见血|很有道理",
    r"的话.{2,14}[，,].{0,4}(不算|就当|要不然)",
    r"先放一边|暂且|姑且|不妨|或许|可能是因为",
    r"等等看|拭目以待|等着瞧|报应",
]
WARN = [r"搞得跟.{1,8}似的", r"我(觉得|认为|个人认为)", r"[吧呗]$"]


def pick_form(bar: str) -> tuple[str, int]:
    items = FORMS.get(bar) or FORMS["asoul"]
    r, acc = random.random(), 0.0
    for f, w in items:
        acc += w
        if r <= acc:
            return f, MAXC[f]
    return items[-1][0], MAXC[items[-1][0]]


def grams(s: str, n: int = 3) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    if not s:
        return set()
    return {s[i:i + n] for i in range(max(1, len(s) - n + 1))}


def too_similar(cand: str, existing: list[str], th: float = 0.30) -> bool:
    c = (cand or "").strip()
    if not c:
        return False
    for e in existing:
        e = (e or "").strip()
        if not e:
            continue
        if c == e and c not in CHANT_OK:
            return True
        g1, g2 = grams(c), grams(e)
        if g1 and g2 and len(g1 & g2) / max(1, len(g1 | g2)) >= th:
            return True
    return False


def check(cand: str, form: str, max_chars: int, existing_texts: list[str], op_text: str) -> tuple[str, str]:
    """返回 (状态, 说明)；状态 ok / too_long / banned_pattern / echo / punct_in_short。"""
    c = (cand or "").strip()
    n = len(c)
    if n > max_chars:
        return "too_long", f"{n}>{max_chars}"
    for p in BAN:
        if re.search(p, c):
            return "banned_pattern", p
    if too_similar(c, list(existing_texts) + [op_text]):
        return "echo", "与本楼/楼主相似"
    if form in ("c1_3", "c4_8", "digit") and re.search(r"[，,。；;]", c):
        return "punct_in_short", "短形态带标点"
    return "ok", ""


def warnings(cand: str) -> list[str]:
    return [p for p in WARN if re.search(p, cand or "")]
