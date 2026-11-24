"""把 data/tieba_posts.jsonl 渲染成人可读的 Markdown（给 Claude 用的那种）。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = DATA / "tieba_posts.jsonl"
OUT = DATA / "tieba_posts.md"


def main() -> None:
    rows = [json.loads(l) for l in SRC.read_text(encoding="utf-8").splitlines() if l.strip()]
    forums = Counter(r["forum"] for r in rows)
    posts = sum(len(r["posts"]) for r in rows)
    comments = sum(len(p["comments"]) for r in rows for p in r["posts"])

    lines = [
        "# 贴吧一手数据",
        "",
        f"- 帖子：{len(rows)} 篇（" + "、".join(f"{k} {v}" for k, v in forums.items()) + "）",
        f"- 楼层：{posts} 条；楼中楼：{comments} 条",
        "- 都是**原文**：保留错别字、脏话、缩写、表情、@，不要改写。",
        "",
        "> 说明：用户昵称后面的括号是贴吧原昵称；`Lv` 是等级，`IP` 是属地（贴吧公开显示）。",
        "",
    ]
    for forum in sorted(forums):
        group = [r for r in rows if r["forum"] == forum]
        if not group:
            continue
        lines += [f"## {forum} 吧（{len(group)} 帖）", ""]
        for r in group:
            lines.append(f"### [{r['title']}](https://tieba.baidu.com/p/{r['tid']})")
            a = r.get("author") or {}
            lines.append(f"- 楼主：{a.get('nick') or a.get('name')}（{a.get('name')}，Lv{a.get('level')}，{a.get('ip')}）｜{r['thread_time']}｜{r['reply_num']} 回复")
            if r.get("errors"):
                lines.append(f"- ⚠️ 抓取异常：{'; '.join(r['errors'])[:200]}")
            lines.append("")
            for p in r["posts"]:
                pa = p.get("author") or {}
                mark = "【楼主】" if p.get("is_op") else ""
                txt = (p.get("text") or "").replace("\n", "\n  > ")
                lines.append(f"- **{p['floor']}楼** {mark}{pa.get('nick') or pa.get('name')}（{pa.get('name')}，Lv{pa.get('level')}，{pa.get('ip')}）：{p['time']}，赞同 {p['agree']}")
                lines.append(f"  > {txt}")
                for cm in p.get("comments") or []:
                    ca = cm.get("author") or {}
                    ct = (cm.get("text") or "").replace("\n", " ")
                    lines.append(f"  - ↳ {ca.get('nick') or ca.get('name')}：{ct}")
                lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写出 {OUT}（{OUT.stat().st_size/1024:.0f} KB）")

    # 按吧拆分，方便分别投喂
    for forum in sorted(forums):
        group = [r for r in rows if r["forum"] == forum]
        if not group:
            continue
        sub = [f"# {forum} 吧原文（{len(group)} 帖）", "",
               f"- 楼层：{sum(len(r['posts']) for r in group)}；楼中楼：{sum(len(p['comments']) for r in group for p in r['posts'])}",
               "", ]
        for r in group:
            a = r.get("author") or {}
            sub.append(f"### [{r['title']}](https://tieba.baidu.com/p/{r['tid']})")
            sub.append(f"- 楼主：{a.get('nick') or a.get('name')}（{a.get('name')}，Lv{a.get('level')}，{a.get('ip')}）｜{r['thread_time']}｜{r['reply_num']} 回复")
            sub.append("")
            for p in r["posts"]:
                pa = p.get("author") or {}
                mark = "【楼主】" if p.get("is_op") else ""
                txt = (p.get("text") or "").replace("\n", " ")
                sub.append(f"- {p['floor']}楼 {mark}{pa.get('nick') or pa.get('name')}（Lv{pa.get('level')}，{pa.get('ip')}）：{txt}")
                for cm in p.get("comments") or []:
                    ca = cm.get("author") or {}
                    sub.append(f"  - ↳ {ca.get('nick') or ca.get('name')}：{(cm.get('text') or '').strip()}")
            sub.append("")
        path = DATA / f"tieba_{forum}.md"
        path.write_text("\n".join(sub), encoding="utf-8")
        print(f"已写出 {path}（{path.stat().st_size/1024:.0f} KB）")


if __name__ == "__main__":
    main()
