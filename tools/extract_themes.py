"""从抓到的贴吧数据里，按主题抽取帖子和原话（给写人设用）。

输出 data/community_themes.md：每个主题下列出相关帖 + 3~6 句原文引用。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = DATA / "tieba_posts.jsonl"
OUT = DATA / "community_themes.md"

THEMES = {
    "毕业·退团·人数": ["毕业", "退团", "走了", "剩下", "三人", "四人", "五人", "只剩"],
    "唯粉·cp·粉丝阵营": ["唯粉", "cp", "cp粉", "团推", "拉踩", "拆团", "毒唯"],
    "开盒·隐私·中之人": ["开盒", "盒", "中之人", "真名", "隐私", "人肉"],
    "线下·演唱会·活动": ["线下", "演唱会", "场", "门票", "应援", "生日会"],
    "官方·运营·企划": ["官方", "运营", "企划", "驼子", "字节", "乐华", "staff"],
    "节奏·争议": ["节奏", "争", "吵", "挂人", "汴京", "打牌", "道歉", "炎上"],
    "二创·梗·日常": ["二创", "梗", "画画", "切片", "直播", "氵", "水楼"],
    "外圈·v圈·对比": ["v吧", "v圈", "别家", "对比", "蹭", "ylg", "引流"],
}


def clean(t: str) -> str:
    t = re.sub(r"\s+", " ", t or "").strip()
    return t[:160]


def main() -> None:
    rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    lines = ["# 社区内部在聊什么（贴吧原文摘要）", "",
             "> 来自使用者配置的源吧抓取结果，引用保留原话（含错别字、脏话、缩写）。", ""]
    for theme, words in THEMES.items():
        hits = []
        for r in rows:
            blob = (r["title"] or "") + " " + " ".join((p.get("text") or "") for p in r["posts"][:8])
            if any(w in blob for w in words):
                hits.append(r)
        hits.sort(key=lambda r: -int(r.get("reply_num") or 0))
        lines += [f"## {theme}（命中 {len(hits)} 帖）", ""]
        for r in hits[:8]:
            lines.append(f"### [{r['title']}](https://tieba.baidu.com/p/{r['tid']}) ｜{r['forum']}｜{r['reply_num']} 回复")
            quotes = []
            for p in r["posts"][:12]:
                t = clean(p.get("text"))
                if len(t) < 8:
                    continue
                au = (p.get("author") or {}).get("nick") or "匿名"
                quotes.append(f"- {au}：{t}")
                if len(quotes) >= 6:
                    break
            lines += quotes or ["- （无正文）"]
            lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT}（{OUT.stat().st_size/1024:.0f} KB）")


if __name__ == "__main__":
    main()
