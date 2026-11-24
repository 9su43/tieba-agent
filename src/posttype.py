"""帖型分类 + 楼内共识抽取。

标题权重 ×3，正文 ×1，按得分取最高；水楼直接 skip。
每种帖型对应一个策略：
- troll_or_skip：炒作引流帖——要么装路人串一句，要么不回
- participate：互动游戏（对联、猜、数、投票）——可以认真参与
- answer：求解释——知道就一句话，不知道不回
- react：热点/吃瓜——只给一句反应
- join：吵架站队——最多一条，跟着楼里的说法
- skip：水楼——不回
"""
from __future__ import annotations

import re
from collections import Counter

WEIGHTS: dict[str, tuple[str, ...]] = {
    "water": ("水楼", "水经验", "经验贴", "打卡", "签到", "盖楼", "求助贴", "氵"),
    "game": ("对联", "下联", "上联", "猜", "数一下", "几个", "投票", "接龙", "脑筋急转", "思维题", "求个答案"),
    "troll": ("该不该", "要不要", "该嫁", "该娶", "如何评价", "你们觉得", "兄弟们", "家人们", "老哥们",
              "我该怎么办", "要怎么办", "我这算", "是不是有点", "正常吗", "过分吗", "算不算",
              "探险发现", "发现一个", "谁懂", "有懂哥吗"),
    "ask": ("谁能说下", "谁能告诉", "怎么回事", "为啥", "为什么", "没看懂", "求懂哥", "来个懂哥",
            "这是什么", "干啥了", "咋了", "求解释"),
    "hot": ("官方", "通报", "发布", "pv", "PV", "联动", "评分", "预告", "官宣", "二测", "一桌", "热搜"),
    "fight": ("节奏", "挂", "抄袭", "道歉", "站队", "拉踩", "反串", "冲了", "开团"),
}

STRATEGY = {
    "water": "skip",
    "game": "participate",
    "troll": "troll_or_skip",
    "ask": "answer",
    "hot": "react",
    "fight": "join",
    "chat": "join",
}


def classify(title: str, posts: list[dict]) -> tuple[str, dict]:
    t = title or ""
    body = " ".join((p.get("text") or "") for p in posts[:6])
    scores = {}
    for ptype, words in WEIGHTS.items():
        s = sum(3 for w in words if w in t) + sum(1 for w in words if w in body)
        if s:
            scores[ptype] = s
    if not scores:
        return "chat", {"strategy": STRATEGY["chat"]}
    ptype = max(scores, key=scores.get)
    if ptype == "water":
        return "water", {"strategy": "skip"}
    return ptype, {"strategy": STRATEGY[ptype], "score": scores[ptype]}


def consensus(posts: list[dict], k: int = 3, max_len: int = 10) -> list[str]:
    """楼里被重复说的短句（共识），用于让回复顺着说而不是自创。"""
    cnt: Counter[str] = Counter()
    for p in posts:
        s = re.sub(r"\s+", "", p.get("text") or "")
        if 1 <= len(s) <= max_len and not s.startswith("http"):
            cnt[s] += 1
    return [w for w, n in cnt.most_common(k) if n >= 2]
