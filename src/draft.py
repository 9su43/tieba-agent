"""拼上下文 → 调模型 → 出一条草稿，并做确定性校验（不靠模型自觉）。"""
from __future__ import annotations

import json
import os
import random
import re
import urllib.request
from pathlib import Path

import http_util

ROOT = Path(__file__).resolve().parent.parent

# 元话语/点评楼：真人不会站在楼外评价这楼
META_BLOCK = ("这楼", "楼里", "楼上说", "评论区", "味道不对", "有内味", "润了", "先润",
              "我站", "我说句公道话", "理性讨论", "求同存异", "别引战", "都少说两句")
# 官腔/辟谣/教育口吻
OFFICIAL_BLOCK = ("官方通报", "以官方为准", "先看官方", "等官方", "据XX报道", "来源：",
                  "科普一下", "顺便说", "辟谣")
# 认错/服软：串子不认账
APOLOGY_BLOCK = ("我错了", "行行行", "我的错", "对不起", "抱歉")
# 看图失败/技术解释：人设里不许提
SIGHT_BLOCK = ("看不了", "看不到", "图挂", "没图", "加载不出", "显示不出", "无法查看", "系统未提供")
# 总结/推演句式：吧友不会替楼主下结论
SUMMARY_BLOCK = ("那以后", "这样下去", "说白了", "也就是说", "总结", "归根到底", "一句话", "这说明",
                 "可见", "换句话说", "本质上", "反正吧", "简而言之")

STRATEGY_HINT = {
    "troll_or_skip": (
        "这帖是引流的/炒作的。你有两个选择：跟着楼里的离谱方向串一句（极短，不超过 12 字，别认真），"
        "或者根本不回。**不要**认真回答、不要分析、不要点评这楼、不要辟谣。"
        "如果写不出像样的串法，就只输出 SKIP。"
    ),
    "participate": "这是互动帖（对联/猜/数/投票）。认真给个答案，短，一两句内。"
                   "注意：互动帖也可能是钓鱼，只贴着楼主给的内容说，别上价值、别点评。",
    "answer": "有人在问问题。知道就一句话说清；不确定就说不清楚，别科普、别长篇、别上纲上线。",
    "react": "热点/新闻转述。给一句你自己的反应，别写通稿腔，别写来源，别当评论区主持人。",
    "join": "有人在这个楼里吵。顺着楼里已有的说法说一句，别当评论员，别劝架。",
    "skip": "这种帖子不该回。只输出 SKIP。",
}


def load_persona(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def build_messages(persona: str, forum: str, title: str, posts: list[dict], today: str,
                   ptype: str = "chat", strategy: str = "join", consensus: list[str] | None = None,
                   vision_cfg: dict | None = None, form: str = "c9_15", max_chars: int = 15) -> list[dict]:
    import corpus
    op = posts[0] if posts else {}
    replies = posts[1:] if len(posts) > 1 else []
    op_text = re.sub(r"\s+", " ", (op.get("text") or "")).strip()

    lines = [f"你在逛「{forum}」吧，现在是 {today}。", "", f"帖子标题：{title}"]
    if op_text:
        lines += ["", "楼主原帖（可能很长，**不要总结它、不要复述它的观点**）：", f"  {op_text[:600]}"]
    images = (op.get("images") or []) if op else []
    if images and vision_cfg:
        try:
            import vision
            descs = vision.describe_many(images, {"vision": vision_cfg})
        except Exception as e:  # noqa: BLE001
            descs = []
            print(f"    [看图异常] {str(e)[:80]}")
        if descs:
            lines += ["", "楼主配的图（内容已描述，别复述描述，只当你知道图是什么）："]
            lines += [f"  · {d}" for d in descs]
    if not op_text.strip() and not (images and vision_cfg):
        lines += ["", "**你没看到任何正文内容（只有图，而且没读到）。直接输出 SKIP。**"]
    if replies:
        lines += ["", "楼里别人的回复（用来体会语气。**一般不要接他们的话**；只有当你确实是在回某一层时，"
                      "才在最后一行写 TARGET: 楼层号，程序会把这条回复挂到那一层下面）："]
        for p in replies[-5:]:
            txt = re.sub(r"\s+", " ", p.get("text") or "").strip()
            if txt:
                lines.append(f"[{p.get('floor') or '?'}楼] {txt[:200]}")

    if consensus:
        lines += ["", "这楼里被重复说的短句（可以顺着用）：", "  · " + " / ".join(consensus)]

    samples = corpus.real_quotes(forum, k=6)
    if samples:
        lines += ["", "这个吧里别人平时的回帖（体会语气，不要照抄）："]
        lines += [f"  · {s}" for s in samples]

    blob = title + " " + op_text[:300] + " " + " ".join((p.get("text") or "") for p in replies[-4:])
    facts = corpus.kb_relevant(blob, k=4)
    if facts:
        lines += ["", "你记得的社区情况（用得上一句就够）："]
        lines += [f"  · {f}" for f in facts]

    lines += [
        "",
        "帖型策略：" + STRATEGY_HINT.get(strategy, STRATEGY_HINT["join"]),
        f"**本次程序给你的形态：{form}，最多 {max_chars} 个字（硬上限，超了会被打回）。**",
        "",
        "回复前按顺序自检（任何一步不过就只输出 SKIP）：",
        "0. 这帖你已经回过 → SKIP；楼主 1 楼没文字且你也没看到图 → 只能发一两个字的反应或 SKIP；"
        "想不出新东西 → SKIP；沉默是常态，大部分帖你都不回。",
        "1. 先看这楼在干什么（报数/顶帖/复读/玩句式/求解释/吹牛/吵架/转信息），你的回复必须属于这个活动类型。",
        "2. 长度听程序的，不许自己加长。",
        "3. 内容只能来自：本楼已有的梗、词典里的梗、你自己的大白话。"
        "不许创造新梗/新外号/新谚语；不许复述楼主或已有人说过的话（换个说法也算）；"
        "不许对真人说\"要出事了\"\"等着看\"\"报应\"；拿不准的词不用；不许编自己的现实情况。",
        "4. 自检：有没有\"A，但B\"这类工整转折？有没有\"有点东西/说到点子上\"这类点评腔？"
        "像不像一句完整的小点评？像就不对。",
        "",
        "通用要求：不要提自己是 AI；不碰隐私和真人；不要用没在这个吧里见过的口头禅。",
        "只输出回复本身。不想回就只输出 SKIP。",
        "如果要回的是楼里某一层，最后单独一行写 `TARGET: 楼层号`（例如 TARGET: 3）；回楼主正文就不要写这行。",
    ]

    return [
        {"role": "system", "content": persona},
        {"role": "user", "content": "\n".join(lines)},
    ]


def call_model(cfg: dict, messages: list[dict], max_tokens: int | None = None, model: str | None = None) -> str:
    import api
    conf = api.resolve(cfg)
    key = conf["key"]
    if not key:
        raise RuntimeError("未配置模型密钥，请在控制台设置 API 或配置环境变量")
    payload = {
        "model": model or conf["model"],
        "messages": messages,
        "max_tokens": max_tokens or cfg["model"].get("maxTokens", 300),
        "temperature": 1.0,
        "stream": False,
    }
    if cfg["model"].get("thinkingDisabled", True):
        payload["thinking"] = {"type": "disabled"}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        api.chat_url(conf["endpoint"]),
        data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    with http_util.open_url(req, timeout=90, stage="调模型") as r:
        data = json.loads(r.read().decode("utf-8"))
    ch = data["choices"][0]
    text = (ch["message"].get("content") or "").strip()
    if not text:
        raise RuntimeError(f"模型返回空内容（finish={ch.get('finish_reason')}）")
    return text


def validate(text: str, cfg: dict, ptype: str = "chat", form: str = "", max_chars: int = 140,
             existing: list[str] | None = None, op_text: str = "") -> tuple[bool, str]:
    t = text.strip().strip('"“”')
    if not t or t.upper() == "SKIP":
        return False, "SKIP"
    if len(t) > 140:
        return False, "太长"
    if re.search(r"https?://", t):
        return False, "带链接"
    if ptype == "troll" and len(t) > 16:
        return False, "炒作帖回太长"
    for w in (cfg.get("blockedWords", []) + list(META_BLOCK) + list(OFFICIAL_BLOCK)
               + list(APOLOGY_BLOCK) + list(SUMMARY_BLOCK) + list(SIGHT_BLOCK)):
        if w and w in t:
            return False, f"含禁用说法「{w}」"
    if re.search(r"(我是|作为)(一个)?(AI|人工智能|机器人|助手)", t):
        return False, "暴露身份"
    if form:
        import forms
        status, why = forms.check(t, form, max_chars, existing or [], op_text)
        if status != "ok":
            return False, f"{status}（{why}）"
    return True, t
