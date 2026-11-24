"""根据本地配置读取指定源吧、筛选候选，并通过项目浏览器搬运帖子。"""
from __future__ import annotations

# [2026-11-07] 控制台是 GBK 时 emoji 会抛 'gbk' codec 错误，整条打分被打断。
import sys as _sys
for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import argparse
import repost_control
import asyncio
import json
import os
import random
import re
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path


from settings import local_now

ROOT = Path(__file__).resolve().parent.parent
# [2026-11-21] 多实例支持：TIEBA_AGENT_CONFIG 指定配置，TIEBA_AGENT_SUFFIX 给状态文件加后缀
import os as _os
SUFFIX = str(_os.environ.get('TIEBA_AGENT_SUFFIX') or '')
CONFIG = Path(_os.environ.get('TIEBA_AGENT_CONFIG') or (ROOT / 'config.json'))
SEEN = ROOT / 'state' / f'repost-seen{SUFFIX}.json'
IMG_DIR = ROOT / 'state' / 'repost-images'
LOG = ROOT / 'state' / f'repost{SUFFIX}.log'
def _load_target() -> str:
    try:
        c = json.loads(CONFIG.read_text(encoding='utf-8'))
        return str((c.get('repost') or {}).get('target') or '')
    except Exception:
        return ''

TARGET = _load_target()


def log(msg: str) -> None:
    line = f'[{local_now():%m-%d %H:%M:%S}] {msg}'
    print(line)
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open('a', encoding='utf-8') as f:
            f.write(line + '\n')
    except Exception:
        pass


def cfg() -> dict:
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def repost_cfg() -> dict:
    c = cfg().get('repost') or {}
    return {
        **c,
        'enabled': c.get('enabled', False),
        'forums': c.get('forums') or [],
        'intervalMinutes': int(c.get('intervalMinutes', 30)),
        'perDay': int(c.get('perDay', 12)),
        'activeHours': c.get('activeHours') or ['00:00-23:59'],
        'includeSource': c.get('includeSource', True),
        'maxImages': int(c.get('maxImages', 3)),
        'minReply': int(c.get('minReply', 30)),
        'maxBodyChars': int(c.get('maxBodyChars', 1500)),
        'storyFloors': int(c.get('storyFloors', 8)),
        'minPotential': int(c.get('minPotential', 4)),
        'sourceMode': str(c.get('sourceMode') or 'list'),
        'sourceForums': c.get('sourceForums') or [],
        'sourceBatch': int(c.get('sourceBatch', 8) or 8),
        'target': str(c.get('target') or ''),
        'altTarget': str(c.get('altTarget') or ''),
        'altEvery': int(c.get('altEvery', 0) or 0),
    }


# ── 读 ────────────────────────────────────────────────────────────────────────
def _first_image(contents) -> str:
    """从 contents 里取第一张图的地址（聚类/封面用）。"""
    for frag in (getattr(contents, 'objs', None) or []):
        for attr in ('origin_src', 'big_src', 'src'):
            v = getattr(frag, attr, None)
            if v:
                return str(v)
    return ''


async def _read_forum(forum: str, n: int, min_reply: int, sort: str = 'hot') -> list[dict]:
    from aiotieba import Client
    from aiotieba.enums import ThreadSortType
    async with Client() as c:
        use = ThreadSortType.CREATE if sort == 'create' else ThreadSortType.HOT
        page = await c.get_threads(forum, pn=1, sort=use)
        rows = []
        for t in (page.objs or [])[:n]:
            tid = int(getattr(t, 'tid', 0) or 0)
            reply = int(getattr(t, 'reply_num', 0) or 0)
            title = (getattr(t, 'title', '') or '').strip()
            if not tid or not title or reply < min_reply:
                continue
            rows.append({'forum': forum, 'tid': tid, 'title': title, 'reply': reply,
                         'text': (getattr(t, 'text', '') or '').strip(),
                         'cover': _first_image(getattr(t, 'contents', None))})
        return rows


async def _read_thread(tid: int, limit: int = 8) -> dict:
    """取一楼（楼主）的文字和图片地址。"""
    from aiotieba import Client
    async with Client() as c:
        page = await c.get_posts(tid, pn=1)
        first = (page.objs or [None])[0]
        if first is None:
            return {'text': '', 'images': []}
        text = (getattr(first, 'text', '') or '').strip()
        images = []
        objs = getattr(getattr(first, 'contents', None), 'objs', None) or []
        for frag in objs:
            for attr in ('origin_src', 'big_src', 'src'):
                v = getattr(frag, attr, None)
                if v:
                    images.append(str(v))
                    break
        return {'text': text, 'images': images[:limit]}


CURSOR = ROOT / 'state' / 'repost-source-cursor.json'


def _rotate_sources(pool: list[str], batch: int) -> list[str]:
    """[2026-11-21] 全站模式：62 个吧轮着抓，每轮只抓 batch 个，避免一轮几十个请求。"""
    pool = [str(x).strip() for x in pool if str(x).strip()]
    if not pool:
        return []
    batch = max(1, min(len(pool), int(batch or 8)))
    try:
        cur = int(json.loads(CURSOR.read_text(encoding='utf-8')).get('i', 0))
    except Exception:
        cur = 0
    picked = [pool[(cur + k) % len(pool)] for k in range(batch)]
    try:
        CURSOR.parent.mkdir(parents=True, exist_ok=True)
        CURSOR.write_text(json.dumps({'i': (cur + batch) % len(pool)}, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass
    return picked



POOL = ROOT / 'state' / f'repost-pool{SUFFIX}.json'


def _pool_load() -> list:
    try:
        data = json.loads(POOL.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _pool_save(rows: list) -> None:
    try:
        POOL.parent.mkdir(parents=True, exist_ok=True)
        POOL.write_text(json.dumps(rows[:60], ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def candidates(limit_per_forum: int = 15) -> list[dict]:
    rc = repost_cfg()
    seen = load_seen()
    out: list[dict] = []
    # [2026-11-22] 用户要快：优先用上一轮扫出来、还没搬的候选，避免每轮都重新扫吧
    for row in _pool_load():
        key = f'{row.get("forum")}:{row.get("tid")}'
        if key in seen:
            continue
        out.append(row)
    if len(out) >= 6:
        log(f'[候选池] 复用 {len(out)} 条，本轮不扫吧')
        out = cluster_candidates(out)
        out.sort(key=lambda c: (-(c.get('cluster_size') or 1), -c['reply']))
        return out
    forum_list = rc['forums']
    if rc.get('sourceMode') == 'sitewide' and rc.get('sourceForums'):
        forum_list = _rotate_sources(rc['sourceForums'], rc.get('sourceBatch', 8))
    for forum in forum_list:
        try:
            rows = asyncio.run(_read_forum(forum, limit_per_forum, rc['minReply']))
        except Exception as e:
            log(f'[读] {forum} 失败：{e}')
            continue
        fresh = [r for r in rows if f'{forum}:{r["tid"]}' not in seen]
        fresh.sort(key=lambda r: -r['reply'])
        out.extend(fresh[:6])
        log(f'[读] {forum}：{len(rows)} 条够热，{len(fresh)} 条没搬过')
    # [2026-11-03] 跨吧聚类：同一内容在多个吧同时出现 → 说明是"哪个吧都吃"的通用贴，
    # 不是吧内事务。单吧热、别处没有的，排序往后退。
    out = cluster_candidates(out)
    out.sort(key=lambda c: (-(c.get('cluster_size') or 1), -c['reply']))
    try:
        _pool_save([r for r in out if f'{r.get("forum")}:{r.get("tid")}' not in seen])
    except Exception:
        pass
    hot = [c for c in out if (c.get('cluster_size') or 1) >= 2]
    if hot:
        log('[聚类] 跨吧同题：' + '；'.join(f'{c["title"][:16]}({c["cluster_size"]}吧)' for c in hot[:3]))
    return out


def _norm_title(text: str) -> str:
    return re.sub(r'[\W_]+', '', str(text or '')).lower()


def _similar_titles(a: str, b: str) -> bool:
    """标题相似度：二字的 Jaccard 或整体相似度任一达标就算同题（同一内容常被改成不同说法）。"""
    import difflib
    if not a or not b:
        return False
    def grams(s):
        return {s[i:i + 2] for i in range(max(0, len(s) - 1))}
    ga, gb = grams(a), grams(b)
    if ga and gb and len(a) >= 8 and len(b) >= 8:
        common = ga & gb
        jac = len(common) / len(ga | gb)
        if jac >= 0.35 and len(common) >= 4:
            return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def dhash_bytes(raw: bytes) -> int:
    """图片感知哈希（8x8 灰度差分）。同一张图换标题也能认出来。"""
    from PIL import Image
    import io
    img = Image.open(io.BytesIO(raw)).convert('L').resize((9, 8))
    px = list(img.getdata())
    bits = 0
    for row in range(8):
        for col in range(8):
            left = px[row * 9 + col]
            right = px[row * 9 + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def _fetch_thumb_hash(url: str) -> int:
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://tieba.baidu.com/'})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(200000)
        return dhash_bytes(raw)
    except Exception:
        return 0


def cluster_candidates(rows: list[dict], max_hash: int = 40) -> list[dict]:
    """按标题相似度聚类，再按图片哈希把"同图不同题"的合并；给每条标 cluster_size（涉及几个吧）。"""
    import difflib
    groups: list[list[dict]] = []
    for c in rows:
        key = _norm_title(c.get('title'))
        c['_key'] = key
        for g in groups:
            # 太短的标题（「图片」「求助」）不参与聚类，否则会把一堆不相干的贴并成一组
            if len(key) >= 6 and _similar_titles(key, g[0]['_key']):
                g.append(c)
                break
        else:
            groups.append([c])
    # 图片比对（只对前 max_hash 条取缩略图，控制成本）
    hashed = 0
    for c in rows:
        if hashed >= max_hash:
            break
        url = (c.get('cover') or '')
        if not url:
            continue
        h = _fetch_thumb_hash(url)
        c['_hash'] = h
        hashed += 1
    merged: list[list[dict]] = []
    for g in groups:
        target = None
        for m in merged:
            a = next((x.get('_hash') for x in g if x.get('_hash')), 0)
            b = next((x.get('_hash') for x in m if x.get('_hash')), 0)
            if a and b and bin(a ^ b).count('1') <= 6:
                target = m
                break
        if target is None:
            merged.append(list(g))
        else:
            target.extend(g)
    for g in merged:
        forums = {x['forum'] for x in g}
        for x in g:
            x['cluster_size'] = len(forums)
            x['cluster_title'] = g[0]['title']
    return rows


# ── 组装 ──────────────────────────────────────────────────────────────────────
_BAD = re.compile(r'[<>]|[\x00-\x08\x0b\x0c\x0e-\x1f]')


def clean_text(text: str, limit: int) -> str:
    s = _BAD.sub('', str(text or ''))
    s = re.sub(r'\r\n?', '\n', s).strip()
    return s[:limit]


# ── 内容过滤：水楼/签到/只在吧里有意义的贴不搬 ───────────────────────────────
# 2026-11-03 用户指出：搬了一条「这是一个水楼 / 助力8u早日飞升黄牌，面见天皇」——
# 这种贴离开原来的吧毫无意义。先过规则，再让模型判一次"搬到别的吧还有人看得懂吗"。
WATER_STRONG = re.compile(
    r'水楼|盖楼|刷经验|刷楼|经验\+\d|经验加\d|签到|打卡|助力|飞升|黄牌|升黄牌|面见|顶帖|来水|水一贴|水个贴|'
    r'求经验|凑经验|领经验|升级经验|冲等级|8u|吧友互助|'
    # [2026-11-03] 接龙/点名类：内容靠别人回，搬过去是一张图加一句"你来"，对别吧没意义
    r'被指到的人|指到的人|一人发一张|一人一张|接龙|楼下接力|传到下|点名|来点|都发一张', re.I)
WATER_WEAK = re.compile(r'测试|试试|第一次发|有人吗|在吗|来个人|水一|升级|等级|经验', re.I)


def looks_meaningless(title: str, body: str, has_images: bool = False) -> str:
    """返回空字符串表示"能搬"，否则返回不搬的理由。规则只拦明确的灌水信号；
    纯图贴（has_images）不在这里判，交给模型看图片内容决定。"""
    # [2026-11-22] 目标吧 4soul 没有吧务、要的是发帖量：水楼/签到类也照搬
    if _skip_lowvalue_filters():
        return ''
    text = f'{title}\n{body}'
    strong = WATER_STRONG.findall(text)
    if strong:
        return f'水楼/签到类（{"、".join(sorted(set(strong)))[:24]}）'
    weak = set(WATER_WEAK.findall(text))
    if len(weak) >= 2:
        return f'低信息量（{"、".join(sorted(weak))[:24]}）'
    if has_images:
        return ''
    if len(re.sub(r'\s+', '', body)) < 6:
        return '正文几乎没有字（也没有图）'
    if not re.search(r'[\u4e00-\u9fffA-Za-z]', body):
        return '正文没有文字（纯符号/表情）'
    return ''


POTENTIAL_SYSTEM = (
    '你是贴吧内容的“传播潜力”评审。用户会给你某条帖子的标题、正文（可能有图片描述），'
    '以及它同时在几个吧出现过。给这条帖子打分：'
    '5 = 段子/反转/离谱截图/两难选择，任何吧的人看了都想接一句；'
    '4 = 有梗或有意思，跨吧可读；'
    '3 = 有信息但平淡；'
    '2 = 只对本吧有意义（吧内事务、本吧黑话、求资源、交友）；'
    '1 = 水楼/签到/吧务/无内容。'
    '示例（照这个尺度判）：【股市暴跌+跳楼图】=5；【两广百越+数据/争论】=4；【今天好烦】=2；【8u们来点地狱笑话】=1。'
    '只输出一个数字（1-5），不要解释。'
)


def judge_potential(cfg: dict, title: str, body: str, visual: str = '', cluster_size: int = 1) -> tuple[int, str]:
    """0-5 的传播潜力分，>=4 才搬。结果按内容指纹缓存。"""
    cache_file = ROOT / 'state' / 'repost-filter.json'
    try:
        cache = json.loads(cache_file.read_text(encoding='utf-8')) if cache_file.exists() else {}
    except Exception:
        cache = {}
    # [2026-11-07] 缓存 key 带上评审模型：换模型后旧分自动失效（否则一直被缓存的 3 分卡住）
    rc = cfg.get('repost') or {}
    judge_model = str(rc.get('judgeModel') or '').strip()
    judge_profile = str(rc.get('judgeProfile') or '').strip()
    key = str(abs(hash((judge_model or 'default') + title + body + visual)) % (10 ** 12))
    if key in cache and isinstance(cache[key].get('score'), int):
        return int(cache[key]['score']), str(cache[key].get('why') or '')
    if rc.get('judgeWithModel') is False:
        # [2026-11-22] 提速模式：不调模型，按回复数快速给分
        score = 5 if int(cfg.get('_reply', 0) or 0) >= 50 else 4
        return score, '规则快速判定'
    jcfg = cfg
    if judge_profile:
        jcfg = dict(cfg)
        jcfg['api'] = {**(cfg.get('api') or {}), 'profile': judge_profile}
    try:
        from draft import call_model
        ask = f'标题：{title}\n正文：{body[:800]}\n同时出现在 {cluster_size} 个吧'
        if visual:
            ask += f'\n（这条没有正文，图里是：{visual}）'
        import time as _time
        answer, last_err = '', None
        for attempt in range(2):          # 失败重试一次，别一次抖动就"先放行"
            try:
                answer = call_model(jcfg, [
                    {'role': 'system', 'content': POTENTIAL_SYSTEM},
                    {'role': 'user', 'content': ask},
                ], max_tokens=24, model=judge_model or None)
                if re.search(r'[1-5]', answer or ''):
                    break
                last_err = RuntimeError(f'没读到分数：{answer[:20]!r}')
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt == 0:
                    _time.sleep(1.5)
        if not re.search(r'[1-5]', answer or ''):
            raise last_err or RuntimeError('打分失败')
        score = int(re.search(r'[1-5]', answer or '').group(0))
        why = answer.strip()[:20]
    except Exception as e:
        log(f'[过滤] 潜力打分失败，先放行：{e}')
        return 5, ''
    cache[key] = {'score': score, 'why': why, 'title': title[:40],
                  'at': local_now().isoformat(timespec='seconds')}
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding='utf-8')
    except Exception:
        pass
    return score, why


async def _read_op_floors(tid: int, max_floors: int = 8, max_chars: int = 1500, max_images: int = 3) -> list[dict]:
    """把楼主自己在楼里接着讲的几层按顺序取出来（故事贴要一层层搬，不能只搬开头）。"""
    from aiotieba import Client
    async with Client() as c:
        page = await c.get_posts(tid, pn=1)
        objs = page.objs or []
        if not objs:
            return []
        first_user = getattr(getattr(objs[0], 'user', None), 'user_id', 0)
        parts = []
        for o in objs:
            u = getattr(o, 'user', None)
            uid = getattr(u, 'user_id', 0) if u else 0
            if first_user and uid and uid != first_user:
                continue                      # 别人的楼层
            text = (getattr(o, 'text', '') or '').strip()
            images = []
            for frag in (getattr(getattr(o, 'contents', None), 'objs', None) or []):
                for attr in ('origin_src', 'big_src', 'src'):
                    v = getattr(frag, attr, None)
                    if v:
                        images.append(str(v))
                        break
            if not text and not images:
                continue
            parts.append({'text': text[:max_chars], 'images': images[:max_images]})
            if len(parts) >= max_floors:
                break
        return parts




def clean_title(text: str, limit: int = 60) -> str:
    """[2026-11-22] 把标题里的换行/制表/连续空格压平并截断。
    实测「结束了——数学的田园时代\n开启了——数学的工业时代」这类多行标题
    会让编辑器校验失败、一直卡在同一条。"""
    t = str(text or '').replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
    t = re.sub(r'\s{2,}', ' ', t).strip()
    return t[:limit]

def build_item(cand: dict, rc: dict) -> dict | None:
    detail = asyncio.run(_read_thread(int(cand['tid']), rc['maxImages']))
    body = clean_text(detail['text'] or cand.get('text') or '', rc['maxBodyChars'])
    title = clean_text(cand['title'], 60)
    if len(title) < 4:
        log(f'[跳过] {cand["forum"]}:{cand["tid"]} 标题太短（{len(title)}）')
        return None
    # [2026-11-03] 故事贴：楼主常常一层接一层地讲（"楼下细说"）。只搬第一层等于断在半句上，
    # 所以把楼主自己的楼层按顺序都取出来，第 1 层做主题帖正文，后面几层依次回成楼层。
    try:
        parts = asyncio.run(_read_op_floors(int(cand['tid']), int(rc.get('storyFloors', 8) or 8),
                                           rc['maxBodyChars'], rc['maxImages']))
    except Exception as e:
        log(f'[楼层] 读取失败，按单层处理：{e}')
        parts = []
    if len(parts) >= 2:
        body = clean_text(parts[0]['text'], rc['maxBodyChars']) or body
        log(f'[故事贴] 《{title[:18]}》楼主共 {len(parts)} 层，按原样逐层搬')
    # 先把图下载下来：纯图贴也要能交给模型判，不能因为"没文字"就一律扔掉
    media_urls = []
    if parts:
        for _p in parts:
            for _u in (_p.get('images') or []):
                if _u not in media_urls:
                    media_urls.append(_u)
    else:
        media_urls = list(detail['images'] or [])
    media_urls = media_urls[:int(rc.get('maxImages', 3) or 3)]
    images = []
    for url in media_urls:
        try:
            images.append(download_image(url, cand['tid']))
        except MediaUnusable as e:
            log(f'[图] 跳过：{e}')
        except Exception as e:
            log(f'[图] 下载失败 {url}：{e}')
    # 这条帖子主要靠视频/图片撑着、却一张都没搬进来 → 别发成"没有内容的帖子"。
    # [2026-11-03] 21:46 实发过一条：视频丢了、正文只剩 27 字（p/11163649286），像残帖。
    carried = len(body) + sum(len(p.get('text') or '') for p in parts[1:])
    if looks_media_only(bool(media_urls) or bool(detail.get('images') or []), images, carried):
        log(f'[跳过] 《{title[:18]}》媒体贴不进来（视频/不支持格式），文字只剩 {carried} 字，不搬')
        seen = load_seen()
        seen[f'{cand["forum"]}:{cand["tid"]}'] = {
            'at': local_now().isoformat(timespec='seconds'), 'title': title,
            'skip': f'媒体贴不进来、文字只剩 {carried} 字',
        }
        save_seen(seen)
        return None
    # ① 规则层只拦"明确的灌水信号"（水楼/签到/助力…）；其他一律交给模型，
    #    纯图贴用视觉模型描述后一起判（2026-11-03 用户要求）
    # [2026-11-04] 先挡「某个吧内部的事务贴」：吧的诞生/吧主/吧规这类跨吧搬没意义
    internal = looks_forum_internal(title, body)
    if internal:
        log(f'[过滤] {cand["forum"]}吧《{title[:20]}》不搬：{internal}')
        return None
    reason = looks_meaningless(title, body, has_images=bool(images))
    if reason:
        log(f'[过滤] {cand["forum"]}吧《{title[:20]}》不搬：{reason}')
        return None
    # ② 模型层
    try:
        cfg_now = cfg()
        visual = ''
        if len(body) < 15 and images:
            try:
                import vision
                descs = vision.describe_many(images, {'vision': cfg_now.get('vision')}, limit=2)
                visual = '；'.join(d for d in descs if d)[:300]
                if visual:
                    log(f'[看图] 《{title[:16]}》：{visual[:60]}')
            except Exception as e:
                log(f'[看图] 失败，按纯文字判：{e}')
        # 传播潜力分（1-5），>=4 才搬；跨吧聚类越大越说明通用
        score, why = judge_potential(cfg_now, title, body, visual=visual,
                                     cluster_size=int(cand.get('cluster_size') or 1))
        log(f'[打分] {cand["forum"]}吧《{title[:18]}》{score}/5（跨 {cand.get("cluster_size") or 1} 吧）{why}')
        if score < int(rc.get('minPotential', 4) or 4):
            log(f'[过滤] 潜力不够（{score}/5），不搬')
            return None
    except Exception as e:
        log(f'[过滤] 模型判定异常，先放行：{e}')
    # [2026-11-03] 用户要求"原帖啥样就发成啥样"，不再在正文末尾加来源链接。
    return {'forum': cand['forum'], 'tid': cand['tid'], 'title': clean_title(title), 'body': body,
            'images': images, 'expect_images': len(images),
            'extra_parts': [p for p in parts[1:] if clean_text(p['text'], rc['maxBodyChars']).strip()]}



# [2026-11-04] 用户要求：不搬"某个吧内部"的贴。
# 依据：实测搬过《地狱笑话吧的诞生（老吧主瞎说亲自编写）》——这是那个吧自己的家务事，
# 搬到别的吧完全没上下文，还容易引战。判据取"讲吧本身"的几种说法，不按内容喜好判。
FORUM_INTERNAL_RE = re.compile(
    r'(本吧|这个吧|该吧|此吧|某某吧|吧务|吧主|小吧|大吧|吧规|吧规|建吧|建版|'
    r'吧里的|吧里的?人|本吧的|来这个吧|回这个吧|离开这个吧|'
    r'[\u4e00-\u9fa5A-Za-z0-9]{1,8}吧的诞生|'
    r'吧的?历史|吧的?来历|吧的?起源|这个吧的?氛围)'
)



# [2026-11-04] 这些失败是"环境状态不对"，不是"这条贴不能搬"：
# 用户手动关掉发帖框、浏览器被切走、编辑器还没渲染出来 —— 都不该计入永久跳过。
TRANSIENT_FAIL_RE = re.compile(
    r'(标题没填进去|正文缺失|编辑器|发帖框没打开|页面没就绪|no_editor)')

# [2026-11-22] 这些是"这条贴真发不出去"，不要当环境问题无限重试
HARD_FAIL_RE = re.compile(r'(图片过大|图片太大|尺寸超限|超过限制|too large|file size)')


def transient_failure(result: dict) -> str:
    """是环境问题就返回匹配到的原因，否则 ''。"""
    if result.get('stage') == 'stopped': return '搬运已停止'
    detail = str((result or {}).get('detail') or '')
    if str((result or {}).get('stage') or '') == 'verify-after':
        return ''                      # 发布后校验失败是真失败（可能已经发出去了）
    if HARD_FAIL_RE.search(detail):
        return ''                      # 图过大这类硬失败：不计环境，计失败次数
    hit = TRANSIENT_FAIL_RE.search(detail)
    return hit.group(0) if hit else ''


def _skip_lowvalue_filters() -> bool:
    """目标吧是 4soul 时放宽：水楼/签到也搬（用户要的是挡住不健康内容的量）。"""
    try:
        return TARGET == '4soul'
    except Exception:
        return False


def looks_forum_internal(title: str, body: str) -> str:
    """吧内事务贴 → 返回原因字符串；不像就返回 ''。

    [2026-11-21] 目标吧是 4soul 时这条不生效：那个吧现在没人管，
    用户要的是“有热度就搬”，吧内话题也照发。
    """
    try:
        if TARGET == '4soul':
            return ''
    except Exception:
        pass
    text = (str(title or '') + ' ' + str(body or ''))[:600]
    if not text.strip():
        return ''
    hit = FORUM_INTERNAL_RE.search(text)
    if not hit:
        return ''
    # 「XX吧」当主题在讲（而不是顺口提到某个吧名）：
    # 标题里出现"吧的诞生/吧的历史/建吧/吧主"这类，视为吧内贴。
    title_hit = FORUM_INTERNAL_RE.search(str(title or ''))
    if title_hit:
        return f'吧内事务（标题含「{title_hit.group(1)}」）'
    return f'吧内事务（含「{hit.group(1)}」）'


def looks_media_only(had_media: bool, images: list, carried_chars: int, minimum: int = 120) -> bool:
    """帖子主要靠视频/图片撑着、却一张都没搬进来 → 会发成残帖，直接跳过。

    纯视频贴（正文 0~30 字）一定跳过；视频+长文的，文字自己站得住就照发。
    """
    return bool(had_media) and not images and carried_chars < minimum


class MediaUnusable(RuntimeError):
    """这条"图"其实不是能贴进编辑器的图片（贴吧的视频片段，或者解不开的格式）。"""


# [2026-11-03] 帖子的 contents.objs 里混着**视频片段**，它们的 src 是
# tb-video.bdstatic.com 的 .mp4。旧代码不看类型，一律存成 xxxx.jpg 再去塞剪贴板：
# GDI+ 的 FromFile 对 mp4 报 "Out of memory."（其实是"格式不认识"），剪贴板是空的，
# 编辑器里 0 张图 → 整条帖子判失败 → 每 2 分钟重试同一条，18:32~21:34 三小时一条没发出去
# （repost.log 里「图片没进去」105 次，state/repost-images 里 115 个 mp4 假 jpg）。
# 现在：下载前按 URL 排视频，下载后按魔术字节认格式；只有 GDI+/剪贴板能直读的四种留原样，
# webp/avif 转 PNG，mp4 之类抛 MediaUnusable 让上层**跳过这条**，不再反复重试。
VIDEO_URL_RE = re.compile(r'(tb-video\.bdstatic\.com|\.(mp4|m3u8|flv|mov|avi|mkv)(\?|$))', re.I)
DIRECT_EXT = {'jpeg': '.jpg', 'png': '.png', 'gif': '.gif', 'bmp': '.bmp'}
CONVERTIBLE = ('webp', 'avif')


def _sniff_image(raw: bytes) -> str:
    """按魔术字节认真实格式：jpeg/png/gif/bmp/webp/avif/movie/unknown。"""
    head = raw[:16]
    if head[:2] == b'\xff\xd8':
        return 'jpeg'
    if head[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[:2] == b'BM':
        return 'bmp'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[4:8] == b'ftyp':
        if head[8:12] in (b'avif', b'avis', b'mif1', b'msf1', b'heic', b'heix', b'heim', b'heis'):
            return 'avif'
        return 'movie'
    return 'unknown'



# [2026-11-22] 贴吧编辑器对超大图会直接拒收，之前会「卡在同一条反复重试」。
# 所有图片入库前统一压到最长边 1600、体积 <= 1.2MB。
MAX_EDGE = 1600
MAX_BYTES = int(1.2 * 1024 * 1024)


def normalize_image(path: str) -> str:
    try:
        import io as _io
        from PIL import Image
        p = Path(path)
        raw = p.read_bytes()
        with Image.open(_io.BytesIO(raw)) as im:
            im.seek(0)
            w, h = im.size
            scale = min(1.0, MAX_EDGE / max(w, h))
            if scale >= 1.0 and len(raw) <= MAX_BYTES:
                return str(p)
            if scale < 1.0:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            out = p.with_name(p.stem + '-s.jpg')
            q = 85
            while True:
                buf = _io.BytesIO()
                im.convert('RGB').save(buf, format='JPEG', quality=q, optimize=True)
                if buf.tell() <= MAX_BYTES or q <= 55:
                    break
                q -= 10
            out.write_bytes(buf.getvalue())
            if out != p:
                try:
                    p.unlink()
                except Exception:
                    pass
            log(f'[图压缩] {p.name} {len(raw)//1024}KB → {out.name} {out.stat().st_size//1024}KB（{w}x{h}→{im.size[0]}x{im.size[1]}）')
            return str(out)
    except Exception as e:
        log(f'[图压缩] 失败，按原图发：{e}')
        return str(path)


def download_image(url: str, tid: int) -> str:
    """下载一张**能贴**的图，返回本地路径；贴不了的抛 MediaUnusable。"""
    if VIDEO_URL_RE.search(str(url)):
        raise MediaUnusable(f'视频片段，不是图片（{Path(str(url)).name[:36]}）')
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(str(url), headers={
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://tieba.baidu.com/',
        # 明确要静态图，别让 CDN 按默认协商塞回视频/动图
        'Accept': 'image/jpeg,image/png,image/*;q=0.8',
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    if len(raw) < 2000:
        raise RuntimeError('图片太小，可能是空图')
    kind = _sniff_image(raw)
    ext = DIRECT_EXT.get(kind)
    if ext:
        path = IMG_DIR / f'{tid}-{random.randint(1000, 9999)}{ext}'
        path.write_bytes(raw)
        return normalize_image(str(path))
    if kind in CONVERTIBLE:
        try:
            import io
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                im.seek(0)                      # 动图只取第一帧
                buf = io.BytesIO()
                im.convert('RGB').save(buf, format='PNG')
            out = IMG_DIR / f'{tid}-{random.randint(1000, 9999)}.png'
            out.write_bytes(buf.getvalue())
            log(f'[图] {kind} 转 PNG：{out.name}（{len(raw) // 1024}KB → {out.stat().st_size // 1024}KB）')
            return normalize_image(str(out))
        except Exception as e:
            raise MediaUnusable(f'{kind} 转 PNG 失败：{e}') from e
    raise MediaUnusable(f'不是图片（{kind}，前 8 字节 {raw[:8].hex()}）')


def prune_images(days: int = 3) -> None:
    """清掉几天前的临时图。每次重试都会重下一份，2026-11-03 一天堆到 200MB。"""
    try:
        cut = time.time() - days * 86400
        n = size = 0
        for f in IMG_DIR.glob('*'):
            try:
                st = f.stat()
                if st.st_mtime < cut:
                    size += st.st_size
                    f.unlink()
                    n += 1
            except Exception:
                continue
        if n:
            log(f'[清理] repost-images 删掉 {n} 个 {size // 1048576}MB 旧图（> {days} 天）')
    except Exception as e:
        log(f'[清理] 失败：{e}')


# ── 写（浏览器） ──────────────────────────────────────────────────────────────
def _bridge(action: str, args: dict, timeout: int = 60) -> dict:
    from tieba_browser import _api
    publishing = (action == 'evaluate' and args.get('code') in (globals().get('PUBLISH_JS'), globals().get('REPLY_PUBLISH_JS'))) or (action == 'send_keys' and args.get('keys') in ('Ctrl+Enter', 'Control+Enter', 'Mod+Enter'))
    if publishing:
        with repost_control.submission(CONFIG):
            return _api(action, args, timeout=timeout)
    return _api(action, args, timeout=timeout)


def _ev(code: str) -> str:
    return (_bridge('evaluate', {'code': code}).get('data') or {}).get('value', '')


def ensure_visible() -> bool:
    """把贴吧标签页切到前台再发。

    [2026-11-04] 用户反馈"只要不点到贴吧那个界面就不会发帖"：原来标签页在后台就直接跳过整轮。
    现在先试着用 webbridge 的 activate_tab 把贴吧标签页切到前台；切不过来才放弃这一轮。
    """
    if page_visible():
        return True
    # [2026-11-04] 单纯 activate_tab 不够（浏览器最小化时 visibilityState 仍是 hidden）。
    # webbridge 的 cdp Page.bringToFront 会做 focus-emulation：标签页开始真正渲染、能接收输入，
    # 但不会抢走用户当前窗口的焦点——正好适合后台发帖。
    try:
        r = _bridge('cdp', {'method': 'Page.bringToFront', 'params': {}}, timeout=20)
        if r.get('ok'):
            time.sleep(1.2)
            if page_visible():
                log('[前台] 用 focus-emulation 让贴吧标签页可渲染（不抢用户焦点）')
                return True
    except Exception as e:
        log(f'[前台] Page.bringToFront 失败：{e}')
    try:
        tabs = (_bridge('list_tabs', {}).get('data') or {}).get('tabs') or []
        tab = next((t for t in tabs if 'tieba.baidu.com' in str(t.get('url') or '')), None)
        if tab and tab.get('tabId') is not None:
            _bridge('activate_tab', {'tabId': tab['tabId']}, timeout=15)
            time.sleep(1.5)
            if page_visible():
                log('[前台] 贴吧标签页原来在后台，已切到前台再发')
                return True
    except Exception as e:
        log(f'[前台] 切到贴吧标签页失败：{e}')
    return page_visible()


def page_visible() -> bool:
    """贴吧标签页是不是可见（浏览器在前台）。

    [2026-11-03] 实测：标签页 visibilityState=hidden 时（浏览器在后台/最小化），
    图片是走剪贴板 + Ctrl+V 的，粘贴根本没有效果 → 编辑器里永远是 0 张图。
    这时不能硬试：连试两次就会被当成"同一条连续失败 2 次"记成已搬，候选白白吃掉。
    """
    try:
        return str(_ev('document.visibilityState')).strip() == 'visible'
    except Exception:
        return True                     # 拿不到状态就别拦，照旧跑


EDITOR_STATE_JS = r"""
(function(){
  var pick={title:null, body:null};
  document.querySelectorAll('.ql-editor').forEach(function(e){
    if (e.offsetParent===null) return;
    var ph=e.getAttribute('data-placeholder')||'';
    if (ph.indexOf('标题')>=0) pick.title=e;
    else if (ph.indexOf('正文')>=0) pick.body=e;
  });
  if (!pick.title && !pick.body) return JSON.stringify({editor:0});
  return JSON.stringify({
    editor: 1,
    title: pick.title ? (pick.title.innerText||'').trim() : '',
    text: pick.body ? (pick.body.innerText||'').trim() : '',
    imgs: pick.body ? pick.body.querySelectorAll('img.tb-plugin-image__img').length : 0
  });
})()
"""



# [2026-11-22] 截图里那种"破图"：图占位出来了，但图床上传失败，img 加载不出来。
# 以前只看节点数量，破图也算成功 → 点发布就卡住/失败。现在逐个校验是否真的加载出来。
IMG_HEALTH_JS = r"""
(function(){
  var imgs = Array.prototype.slice.call(document.querySelectorAll('img.tb-plugin-image__img'));
  var broken = 0;
  imgs.forEach(function(im){
    var ok = (im.naturalWidth || 0) > 8 && !!im.getAttribute('src');
    if (!ok) broken++;
  });
  return JSON.stringify({total: imgs.length, broken: broken});
})()
"""


def broken_images() -> int:
    try:
        return int(json.loads(_ev(IMG_HEALTH_JS) or '{}').get('broken') or 0)
    except Exception:
        return 0


def drop_broken_images() -> int:
    """把加载不出来的图从正文里删掉，避免带着破图点发布。"""
    try:
        raw = _ev(r"""
(function(){
  var imgs = Array.prototype.slice.call(document.querySelectorAll('img.tb-plugin-image__img'));
  var n = 0;
  imgs.forEach(function(im){
    if ((im.naturalWidth || 0) <= 8 || !im.getAttribute('src')) {
      var box = im.closest('.tb-plugin-image, .edui-image-wrapper, .image-wrapper') || im;
      try { box.remove(); } catch(e) { try { im.remove(); } catch(e2) {} }
      n++;
    }
  });
  return String(n);
})()
""")
        return int(str(raw).strip() or 0)
    except Exception:
        return 0


def upload_images(paths: list[str]) -> dict:
    """把本地图片塞进正文编辑器。

    实测（2026-11-03）：webbridge 的 upload / CDP 的 DOM.setFileInputFiles 都被
    "扩展没有文件访问权限" 挡住（Not allowed），而且扩展自己也没法开这个权限。
    可行路线是 **剪贴板粘贴**：把图片放进 Windows 剪贴板 → 聚焦正文编辑器 → Ctrl+V，
    贴吧前端会自己把图传到它的图床（回填 tb-plugin-image / tiebapic.baidu.com）。
    """
    if not paths:
        return {'ok': True, 'via': 'none', 'count': 0}
    ok = 0
    for path in paths:
        try:
            set_clipboard_image(path)
            focus_body()
            _bridge('send_keys', {'keys': 'Mod+V'})
            if wait_image_added(ok + 1, timeout=14):
                ok += 1
                log(f'[图] 粘贴第 {ok} 张成功：{Path(path).name}')
                # 等图床回填并真的渲染出来
                for _ in range(6):
                    if broken_images() == 0:
                        break
                    time.sleep(2.5)
                bad = broken_images()
                if bad:
                    n = drop_broken_images()
                    log(f'[图] 有 {bad} 张上传失败（破图），已删掉 {n} 张，这条改发文字')
                    ok -= 1
                    break
            else:
                log(f'[图] 第 {ok + 1} 张粘贴后没出现：{Path(path).name}')
                break
        except Exception as e:
            log(f'[图] 粘贴失败 {Path(path).name}：{e}')
            break
    return {'ok': ok == len(paths), 'via': 'clipboard', 'count': ok}


def set_clipboard_image(path: str) -> None:
    _bridge('set_clipboard_image', {'path': str(Path(path).resolve())})


FOCUS_BODY_JS = r"""
(function(){
  var t=null;
  document.querySelectorAll('.ql-editor').forEach(function(e){
    if(!t && e.offsetParent!==null && ((e.getAttribute('data-placeholder')||'').indexOf('正文')>=0)) t=e;
  });
  if(!t) return 'no';
  t.focus();
  var r=document.createRange(); r.selectNodeContents(t); r.collapse(false);
  var s=window.getSelection(); s.removeAllRanges(); s.addRange(r);
  return 'ok';
})()
"""


def focus_body() -> str:
    return _ev(FOCUS_BODY_JS)


IMG_COUNT_JS = r"""
(function(){
  var t=null;
  document.querySelectorAll('.ql-editor').forEach(function(e){
    if(!t && e.offsetParent!==null && ((e.getAttribute('data-placeholder')||'').indexOf('正文')>=0)) t=e;
  });
  if(!t) return -1;
  return t.querySelectorAll('img.tb-plugin-image__img').length;
})()
"""


def wait_image_added(want: int, timeout: int = 25) -> bool:
    for _ in range(timeout):
        time.sleep(1)
        try:
            if int(float(_ev(IMG_COUNT_JS) or 0)) >= want:
                return True
        except Exception:
            pass
    return False



PUBLISH_JS = r"""
(function(){
  // [2026-11-22] 弹窗里的「发布」是蓝色圆角按钮，之前只认 div/button + 文本完全等于「发布」，
  // 新弹窗里按钮文字带空白/额外节点就找不到 → 一直卡在同一条（用户只能手点）。
  var out = {hit: ''};
  var all = Array.prototype.slice.call(document.querySelectorAll('button,div,span,a'));
  var cands = all.filter(function(el){
    var t = (el.innerText || el.textContent || '').replace(/\s+/g, '');
    if (t !== '发布' && t !== '发布帖子') return false;
    var r = el.getBoundingClientRect();
    if (el.offsetParent === null || r.width < 30 || r.height < 16) return false;
    // 排除外层容器（里面还有别的按钮文字）
    if (el.querySelector('button,a') && (el.innerText||'').replace(/\s+/g,'') !== '发布') return false;
    return true;
  });
  if (!cands.length) {
    // 兜底：找弹窗里最像主按钮的那个（蓝色背景 + 文字含发布）
    var dlg = document.querySelector('.tb-editor-wrapper, .edui-body-container, .dialog, [class*=dialog]') || document.body;
    var btn = Array.prototype.slice.call(dlg.querySelectorAll('button,div,span')).filter(function(el){
      var t = (el.innerText||'').trim();
      var r = el.getBoundingClientRect();
      if (t.indexOf('发布') < 0 || r.width < 40 || r.height < 20 || el.offsetParent === null) return false;
      var bg = getComputedStyle(el).backgroundColor || '';
      return /rgb\(\s*(?:\d{1,3}\s*,){2}\d{1,3}\s*\)/.test(bg) && bg.indexOf('255, 255, 255') < 0;
    }).pop();
    if (!btn) return 'no_publish_btn';
    btn.click();
    return 'clicked_fallback';
  }
  // 取最深（最内层）的那个，避免点到容器
  var target = cands[cands.length - 1];
  target.scrollIntoView({block: 'center'});
  target.click();
  return 'clicked';
})()
"""

BUSY_FILE = ROOT / 'state' / 'repost-busy.txt'




def _close_editor_quietly() -> None:
    """把页面上的发帖框关掉/清空，避免它挡住下一轮。"""
    try:
        from tieba_browser import clear_composer, clear_guard
        clear_composer()
        clear_guard()
        log('[清理] 已关掉残留发帖框')
    except Exception as e:
        log(f'[清理] 关发帖框失败：{e}')

def post_item(item: dict) -> dict:
    """发一条主题帖，发布前后各校验一次。"""
    from tieba_browser import _api, _FILL_JS
    if repost_control.stopped(CONFIG): return {'ok':False,'stage':'stopped','detail':'搬运已停止'}
    want_text = item['body']
    try:
        from tieba_browser import safe_navigate
        safe_navigate(f'https://tieba.baidu.com/f?kw={TARGET}')
    except RuntimeError as e:
        if '正在编辑' in str(e):
            # [2026-11-22] 连续几轮都撞到"正在编辑"，多半是上次失败残留的发帖框。
            # 先尝试清掉再导航一次；还不行就这轮放弃，等下一轮。
            try:
                _busy = int((BUSY_FILE.read_text(encoding='utf-8') or '0').strip() or 0) + 1
            except Exception:
                _busy = 1
            try:
                BUSY_FILE.write_text(str(_busy), encoding='utf-8')
            except Exception:
                pass
            if _busy >= 3:
                log(f'[清理] 连续 {_busy} 轮撞到发帖框，尝试强制清理残留草稿')
                try:
                    from tieba_browser import clear_composer, clear_guard
                    clear_composer(); clear_guard()
                    time.sleep(1.0)
                    from tieba_browser import safe_navigate as _nav
                    _nav(f'https://tieba.baidu.com/f?kw={TARGET}')
                    BUSY_FILE.write_text('0', encoding='utf-8')
                    log('[清理] 清掉了，继续发这条')
                except Exception as e2:
                    log(f'[等待] 清理失败：{e2}')
                    return {'ok': False, 'stage': 'busy', 'detail': '浏览器被人占着'}
            else:
                log(f'[等待] 浏览器里有人正在编辑帖子（第 {_busy} 次），这轮先不搬')
                return {'ok': False, 'stage': 'busy', 'detail': '浏览器被人占着'}
        raise
    except Exception:
        _api('navigate', {'url': f'https://tieba.baidu.com/f?kw={TARGET}'})
    time.sleep(3)
    _api('click', {'selector': '.add-btn'})
    time.sleep(2.5)
    st = json.loads(_ev(EDITOR_STATE_JS) or '{}')
    if not st.get('editor'):
        return {'ok': False, 'stage': 'open', 'detail': '发帖框没打开（浏览器可能没登录贴吧）'}
    # [2026-11-22] 从这里开始页面已经被我们占着：任何提前返回都必须把框关掉，
    # 否则它会一直挂在页面上，下一轮判 busy，从此再也发不出去（用户看到的"卡在没点发布"）。
    _opened = True

    fill = json.loads(_ev(_FILL_JS % json.dumps({'title': clean_title(item['title']), 'body': want_text}, ensure_ascii=False)) or '{}')
    if not fill.get('ok'):
        _close_editor_quietly()
        return {'ok': False, 'stage': 'fill', 'detail': f'填内容失败：{fill.get("err")}'}

    up = upload_images(item['images'])
    # [2026-11-22] 图床上传失败（破图）时，宽紧改成发文字版：不再整条重试，省时间
    if item['expect_images'] and up['count'] < item['expect_images']:
        log(f'[图] {item["expect_images"]} 张里只进去 {up["count"]} 张，这条按文字版发')
        item['expect_images'] = up['count']
        item['images'] = item['images'][:up['count']]
    time.sleep(4 if up['count'] else 0)

    st = json.loads(_ev(EDITOR_STATE_JS) or '{}')
    title_now = (st.get('title') or '')
    text_now = (st.get('text') or '')
    imgs_now = int(st.get('imgs') or 0)
    # 校验①：发布前的编辑器（标题、正文、图片张数、乱码）
    problems = []
    if clean_title(item['title']) not in title_now:
        problems.append(f'标题没填进去（编辑器标题栏是「{title_now[:20]}」）')
    if len(text_now) < max(1, int(len(want_text) * 0.5)):
        problems.append(f'正文缺失（编辑器里只有 {len(text_now)} 字，应约 {len(want_text)} 字）')
    if imgs_now < item['expect_images']:
        problems.append(f'图片没进去（编辑器里 {imgs_now} 张，应有 {item["expect_images"]} 张）')
    if '\ufffd' in text_now or '�' in text_now:
        problems.append('正文有乱码字符')
    if problems:
        _close_editor_quietly()
        return {'ok': False, 'stage': 'verify-before', 'detail': '；'.join(problems)}

    # 发布按钮：沿用 tieba_browser 里的找法（文字是「发布」且可见）
    try:
        clicked = _ev(PUBLISH_JS)
    except repost_control.RepostStopped:
        _close_editor_quietly()
        return {'ok':False,'stage':'stopped','detail':'搬运已停止，未发布'}
    if clicked not in ('clicked', 'clicked_fallback'):
        # 兜底：在正文里按 Ctrl+Enter（贴吧支持快捷发布）
        try:
            focus_body()
            _bridge('send_keys', {'keys': 'Ctrl+Enter'})
            time.sleep(1.5)
            log('[发布] 找不到按钮，已用 Ctrl+Enter 兜底')
        except Exception as e:
            log(f'[发布] Ctrl+Enter 兜底失败：{e}')
        clicked = 'shortcut'
    time.sleep(2)
    # [2026-11-22] 点了发布但弹窗没关 = 没真的发出去。最多试 3 轮：
    # 再点一次 → Ctrl+Enter → 还不行就关掉框、记为硬失败，换下一条。
    closed = False
    for attempt in range(3):
        for _ in range(10):
            time.sleep(1)
            st = json.loads(_ev(EDITOR_STATE_JS) or '{}')
            if not st.get('editor'):
                closed = True
                break
        if closed:
            break
        log(f'[发布] 第 {attempt + 1} 次点了没反应，换方式重试')
        if attempt == 0:
            try:
                again = _ev(PUBLISH_JS)
                log(f'[发布] 再点一次按钮：{again}')
            except Exception as e:
                log(f'[发布] 再点失败：{e}')
        else:
            try:
                focus_body()
                _bridge('send_keys', {'keys': 'Ctrl+Enter'})
                log('[发布] 用 Ctrl+Enter 兜底')
            except Exception as e:
                log(f'[发布] Ctrl+Enter 失败：{e}')
    if not closed:
        _close_editor_quietly()
        return {'ok': False, 'stage': 'publish', 'detail': '点了发布但弹窗没关，没发出去'}
    url = _ev('location.href')
    time.sleep(3)
    result = verify_published(item, url)
    if result.get('ok'):
        result.update(post_extra_parts(item, result.get('url') or ''))
    return result



# ── 楼层带图（2026-11-18）────────────────────────────────────────────────────
# 贴吧的发图只能走浏览器剪贴板（和主帖同一套）：打开帖子 → 点开回复框 → 粘图 → 粘文字 → 发表。
REPLY_OPEN_JS = r"""
(function(){
  var edit=null;
  document.querySelectorAll('textarea, [contenteditable="true"], .ql-editor').forEach(function(e){
    if(edit) return;
    if(e.offsetParent===null) return;
    var ph=(e.getAttribute('placeholder')||e.getAttribute('data-placeholder')||'');
    if(ph.indexOf('回复')>=0 || ph.indexOf('说点什么')>=0 || ph.indexOf('留言')>=0) edit=e;
  });
  if(!edit){
    document.querySelectorAll('[contenteditable="true"], .ql-editor').forEach(function(e){
      if(!edit && e.offsetParent!==null) edit=e;
    });
  }
  if(!edit) return 'no';
  edit.focus();
  return 'ok';
})()
"""


REPLY_PUBLISH_JS = r"""
(function(){
  var words = ['发表回复','发表','回复','发送'];
  var btns = Array.from(document.querySelectorAll('div,button,a,span')).filter(function(el){
    var t=(el.textContent||'').trim();
    return words.indexOf(t)>=0 && el.offsetParent!==null && el.getBoundingClientRect().width>20;
  });
  if(!btns.length) return 'no_publish_btn';
  btns[btns.length-1].click();
  return 'clicked';
})()
"""

def set_clipboard_text(text: str) -> None:
    _bridge('set_clipboard_text', {'text': str(text)})


def reply_extra_part(tid: int, text: str, images: list[str]) -> bool:
    """把一层回复连图一起发出去（浏览器）；任何异常都返回 False，由调用方退回纯文字。"""
    try:
        from tieba_browser import safe_navigate
        safe_navigate(f'https://tieba.baidu.com/p/{tid}')
        time.sleep(4)
        if _ev(REPLY_OPEN_JS) != 'ok':
            log('[楼层] 找不到回复框，退回纯文字')
            return False
        for path in (images or []):
            set_clipboard_image(path)
            _bridge('send_keys', {'keys': 'Mod+V'})
            time.sleep(2.5)
        if text:
            set_clipboard_text(text[:1400])
            _bridge('send_keys', {'keys': 'Mod+V'})
            time.sleep(1.5)
        clicked = _ev(REPLY_PUBLISH_JS)
        if clicked != 'clicked':
            log('[楼层] 找不到发表按钮，退回纯文字')
            return False
        time.sleep(3)
        return True
    except Exception as e:
        log(f'[楼层] 带图回复失败（退回纯文字）：{e}')
        return False


def post_extra_parts(item: dict, url: str) -> dict:
    """故事贴：把楼主后续几层依次回成新帖的楼层（文字），间隔几秒，别看着像机器人。"""
    parts = item.get('extra_parts') or []
    if not parts:
        return {}
    m = re.search(r'/p/(\d+)', url or '')
    if not m:
        return {'floors_expected': len(parts), 'floors_posted': 0, 'floors_error': '拿不到新帖 tid'}
    tid = int(m.group(1))
    posted = 0
    for part in parts:
        if repost_control.stopped(CONFIG): break
        text = clean_text(part.get('text') or '', 1500).strip()
        part_imgs = [p for p in (part.get('images') or [])]
        if not text and not part_imgs:
            continue
        time.sleep(random.uniform(4, 10))
        # [2026-11-18] 楼层带图：先试浏览器粘贴（图+字），失败再退回纯文字接口
        if part_imgs and reply_extra_part(tid, text, part_imgs):
            posted += 1
            log(f'[楼层] 已补第 {posted + 1} 层（带图 {len(part_imgs)} 张）')
            continue
        try:
            from tieba_post import reply as tieba_reply
            with repost_control.submission(CONFIG, 'story-reply'):
                res = tieba_reply(tid, text or '', TARGET)
        except Exception as e:
            log(f'[楼层] 第 {posted + 2} 层发送异常：{e}')
            break
        if res.get('ok'):
            posted += 1
            log(f'[楼层] 已补第 {posted + 1} 层（共 {len(parts) + 1} 层）')
        else:
            log(f'[楼层] 第 {posted + 2} 层发失败：{res.get("error") or res.get("no")}')
            break
    return {'floors_expected': len(parts) + 1, 'floors_posted': posted + 1}


def verify_published(item: dict, url: str) -> dict:
    """回读帖子校验：发布后页面不会跳到 /p/<tid>，所以用 aiotieba 查目标吧最新帖，
    按标题认领，再读这帖的正文和图片数。对不上就算失败，不自动重发。"""
    tid = ''
    for _ in range(8):
        try:
            rows = asyncio.run(_read_forum(TARGET, 20, 0, sort='create'))
        except Exception as e:
            log(f'[校验] 查最新帖失败：{e}')
            rows = []
        hit = next((r for r in rows if str(r['title']).strip() == item['title'].strip()), None)
        if hit:
            tid = str(hit['tid'])
            break
        time.sleep(4)
    if not tid:
        return {'ok': False, 'stage': 'verify-after', 'detail': '发布后在新帖列表里找不到这条（可能被审核/删了）', 'url': url}
    link = f'https://tieba.baidu.com/p/{tid}'
    try:
        detail = asyncio.run(_read_thread(int(tid), 8))
    except Exception as e:
        return {'ok': False, 'stage': 'verify-after', 'detail': f'读回帖子失败：{e}', 'url': link}
    problems = []
    text = str(detail.get('text') or '')
    # [2026-11-05] 原来用 max(10, 原文字数*0.4)：短文（4 字）永远小于 10 被误判成"偏短"，
    # 实测三条已经发出去的帖子被记成失败。改成和原文比，且给短文留余量。
    want = len(item['body'])
    got = len(text)
    if got < max(3, int(want * 0.4)) and got < want - 5:
        problems.append(f'正文偏短（帖子 {got} 字，原 {want} 字）')
    imgs = len(detail.get('images') or [])
    if imgs < item['expect_images']:
        problems.append(f'图片缺（帖子 {imgs} 张，应 {item["expect_images"]} 张）')
    if problems:
        return {'ok': False, 'stage': 'verify-after', 'detail': '；'.join(problems), 'url': link}
    return {'ok': True, 'url': link}


PUBLISHED_STATE_JS = r"""
(function(){
  var h=document.querySelector('h1, .post_title, .core_title_txt');
  var body=document.querySelector('.post_content, .d_post_content, .p_content, .core_reply_content')||document.body;
  return JSON.stringify({
    title: h ? ((h.innerText||h.textContent||'').trim()) : '',
    text: (body.innerText||'').slice(0, 4000),
    imgs: body.querySelectorAll('img').length
  });
})()
"""


# ── 频率与去重 ────────────────────────────────────────────────────────────────
def load_seen() -> dict:
    try:
        return json.loads(SEEN.read_text(encoding='utf-8'))
    except Exception:
        return {}


def remember_own_thread(url: str) -> None:
    """记下自己创建的帖子 tid，回帖机器人看到这些帖子就不再回复里面的楼层。"""
    m = re.search(r'/p/(\d+)', str(url or ''))
    if not m:
        return
    path = ROOT / 'state' / 'own-threads.json'
    try:
        data = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except Exception:
        data = {}
    data.setdefault('tids', {})[m.group(1)] = {'at': local_now().isoformat(timespec='seconds'), 'src': 'repost'}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding='utf-8')


def save_seen(seen: dict) -> None:
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding='utf-8')


def load_fails() -> dict:
    f = ROOT / 'state' / f'repost-fails{SUFFIX}.json'
    try:
        return json.loads(f.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_fails(fails: dict) -> None:
    f = ROOT / 'state' / f'repost-fails{SUFFIX}.json'
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(fails, ensure_ascii=False, indent=1), encoding='utf-8')


def heartbeat() -> None:
    try:
        (ROOT / 'state' / f'repost{SUFFIX}.heartbeat').write_text(str(int(time.time())), encoding='utf-8')
    except Exception:
        pass


def in_active_hours(ranges: list[str], now: datetime | None = None) -> bool:
    now = now or local_now()
    cur = now.hour * 60 + now.minute
    for r in ranges:
        m = re.match(r'^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$', r or '')
        if not m:
            continue
        a = int(m.group(1)) * 60 + int(m.group(2))
        b = int(m.group(3)) * 60 + int(m.group(4))
        if a <= b and a <= cur <= b:
            return True
        if a > b and (cur >= a or cur <= b):   # 跨午夜
            return True
    return False


def _until_at():
    """[2026-11-21] repost.until：到点自动停（配置里写 2026-11-23 18:00 这种）。"""
    try:
        c = json.loads(CONFIG.read_text(encoding='utf-8'))
        raw = str((c.get('repost') or {}).get('until') or '').strip()
        if not raw:
            return None
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
            try:
                return datetime.strptime(raw, fmt)
            except ValueError:
                continue
        return None
    except Exception:
        return None


def _disable_repost(reason: str) -> None:
    """到点停止。如果配置里存了 restore 快照（活动模式前的原规则），就恢复它；
    没有快照才真的把 repost.enabled 置 false。只做一次。"""
    try:
        path = CONFIG
        c = json.loads(path.read_text(encoding='utf-8'))
        r = c.get('repost') or {}
        restore = r.get('restore')
        if isinstance(restore, dict) and restore:
            r.update(restore)
            r.pop('restore', None)
            r.pop('until', None)
            r['enabled'] = True
            r['restoredAt'] = local_now().strftime('%Y-%m-%d %H:%M')
            r['restoredReason'] = reason
            c['repost'] = r
            path.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding='utf-8')
            log(f'[恢复] 活动模式结束（{reason}），已恢复原规则：{r.get("target")} 吧 / 间隔 {r.get("intervalMinutes")} 分')
            return
        if r.get('enabled') is False:
            return
        r['enabled'] = False
        r['disabledReason'] = reason
        c['repost'] = r
        path.write_text(json.dumps(c, ensure_ascii=False, indent=2), encoding='utf-8')
        log(f'[停止] repost 已关闭：{reason}')
    except Exception as e:
        log(f'[停止] 写配置失败：{e}')


def today_count() -> int:
    try:
        rows = [json.loads(l) for l in LOG.read_text(encoding='utf-8').splitlines() if '"ok": true' in l]
    except Exception:
        return 0
    today = local_now().strftime('%m-%d')
    return sum(1 for r in rows if str(r.get('at', '')).startswith(today))


COUNTER = ROOT / 'state' / f'repost-round{SUFFIX}.json'


def _next_target() -> tuple[str, int]:
    """[2026-11-21] 单进程双目标：默认发 target，每 altEvery 轮插一轮 altTarget。
    两个进程同时发会抢同一个贴吧页面，所以放在一个循环里轮换。"""
    global TARGET
    try:
        rc = repost_cfg()
        alt = str(rc.get('altTarget') or '')
        every = int(rc.get('altEvery') or 0)
        if not alt or every <= 0:
            return rc.get('target') or TARGET, 0
        try:
            data = json.loads(COUNTER.read_text(encoding='utf-8'))
        except Exception:
            data = {'n': 0}
        n = int(data.get('n', 0)) + 1
        use_alt = (n % every == 0)
        COUNTER.parent.mkdir(parents=True, exist_ok=True)
        COUNTER.write_text(json.dumps({'n': n, 'alt': use_alt}, ensure_ascii=False), encoding='utf-8')
        return (alt if use_alt else (rc.get('target') or TARGET)), (n if use_alt else 0)
    except Exception:
        return TARGET, 0


def run_once(dry: bool = False, force: bool = False) -> dict:
    if repost_control.stopped(CONFIG): return {'ok':False,'detail':'repost 未启用'}
    until = _until_at()
    if until and local_now() > until:
        _disable_repost('到达 until 时间，自动停止')
        return {'ok': False, 'detail': '已到停止时间'}
    global TARGET
    _tgt, _altRound = _next_target()
    if _tgt:
        TARGET = _tgt
    rc = repost_cfg()
    if _altRound:
        rc['minReply'] = int(rc.get('altMinReply', 30) or 30)
        rc['minPotential'] = int(rc.get('altMinPotential', 3) or 3)
        rc['forums'] = rc.get('altForums') or rc['forums'] or rc['sourceForums']
        rc['sourceMode'] = 'list'
        log(f'[轮换] 本轮发 {TARGET} 吧（第五轮插发）')
    if not rc.get('target'):
        return {'ok': False, 'detail': '请在控制台配置搬运目标吧'}
    if not rc['enabled']:
        return {'ok': False, 'detail': 'repost 未启用'}
    if not dry and not force and not in_active_hours(rc['activeHours']):
        return {'ok': False, 'detail': '不在活跃时段'}
    if not dry and not force and rc['perDay'] > 0 and today_count() >= rc['perDay']:
        return {'ok': False, 'detail': '今天已到上限'}
    cands = candidates()
    if not cands:
        return {'ok': False, 'detail': '没有没搬过的热帖'}
    item = None
    for cand in cands[:4]:
        item = build_item(cand, rc)
        if item:
            break
    if not item:
        _pool_save([])
        log('[候选池] 这批都不合格，已清空，下一轮重新扫吧')
        return {'ok': False, 'detail': '候选都不合格'}
    log(f'[候选] {item["forum"]}吧 《{item["title"]}》图 {item["expect_images"]} 张')
    if dry:
        print(json.dumps({k: v for k, v in item.items() if k != 'images'}, ensure_ascii=False, indent=1))
        print('图片：', item['images'])
        return {'ok': True, 'dry': True}
    # [2026-11-03] 图片只能靠剪贴板 + Ctrl+V 塞进编辑器，标签页隐藏时必然 0 张 →
    # 别让它算成"这条失败"，否则两次就把候选记成已搬、永远不发。
    if not ensure_visible():
        log('[等待] 贴吧标签页不可见、也切不到前台，这轮不搬，候选留着')
        return {'ok': False, 'detail': '贴吧标签页不可见'}
    # [2026-11-03] 发布前去目标吧查重：上一条如果"发出去了但校验没过"，会被当成失败重发，
    # 实测就出现了同一贴发两遍。规则：目标吧最新 20 条里已经有同标题的帖子 → 记成已搬、跳过。
    try:
        recent = asyncio.run(_read_forum(TARGET, 20, 0, sort='create'))
        dup = next((r for r in recent if str(r['title']).strip() == item['title'].strip()), None)
        if dup:
            seen = load_seen()
            seen[f'{item["forum"]}:{item["tid"]}'] = {'at': local_now().isoformat(timespec='seconds'), 'title': item['title'], 'dup': str(dup['tid'])}
            save_seen(seen)
            log(f'[查重] 目标吧已有同标题帖子 {dup["tid"]}，跳过，不再重发')
            return {'ok': False, 'detail': f'已有同标题帖子 {dup["tid"]}，跳过'}
    except Exception as e:
        log(f'[查重] 失败，继续发：{e}')
    result = post_item(item)
    seen = load_seen()
    if result.get('ok'):
        seen[f'{item["forum"]}:{item["tid"]}'] = {'at': local_now().isoformat(timespec='seconds'), 'title': item['title']}
        save_seen(seen)
        _pool_save([r for r in _pool_load() if not (r.get('forum') == item['forum'] and r.get('tid') == item['tid'])])
        remember_own_thread(result.get('url') or '')
    else:
        # [2026-11-04] 环境问题（编辑框被关、浏览器切走）不算这条贴的失败
        why = transient_failure(result)
        if why:
            log(f'[等待] {why}：这轮不计失败，下轮再来（不记成已搬）')
            return result
        # [2026-11-03] 失败会原样重试同一条（看着像"卡在一个贴"）。
        # 同一 tid 连续失败 2 次就记成已搬，别再缠着它。
        fails = load_fails()
        key = f'{item["forum"]}:{item["tid"]}'
        fails[key] = int(fails.get(key, 0)) + 1
        save_fails(fails)
        if fails[key] >= 2:
            seen[f'{item["forum"]}:{item["tid"]}'] = {
                'at': local_now().isoformat(timespec='seconds'), 'title': item['title'],
                'skip': f'连续失败 {fails[key]} 次：{result.get("detail") or result.get("stage")}',
            }
            save_seen(seen)
            log(f'[放弃] 《{item["title"][:18]}》连续失败 {fails[key]} 次，记成已搬，换下一条')
    log(json.dumps({'at': local_now().strftime('%m-%d %H:%M'), 'item': f'{item["forum"]}:{item["tid"]}',
                    'title': item['title'], 'forum': item['forum'], 'imgs': item['expect_images'],
                    **result}, ensure_ascii=False))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry', action='store_true')
    ap.add_argument('--once', action='store_true')
    ap.add_argument('--force', action='store_true', help='忽略活跃时段/每日上限（手动测试用）')
    args = ap.parse_args()
    if args.dry or args.once:
        print(json.dumps(run_once(dry=args.dry, force=args.force), ensure_ascii=False))
        return
    # [2026-11-03] 原来的循环把间隔在启动时读一次，之后改配置不生效；而且"不在活跃时段"也睡满一个间隔，
    # 用户 15:00 打开时段后要等到 15:2x 才动。现在改成每 15 秒看一次：
    #   · 到点就跑；
    #   · 配置改了（间隔/时段）立刻按新值算；
    #   · 有 state/repost-now.flag（页面点"立即搬一条"）就立刻跑；
    #   · 因为"不在时段/到上限"被跳过时，2 分钟后再看，不睡满间隔。
    FLAG = ROOT / 'state' / 'repost-now.flag'
    # [2026-11-06] 单实例：文件锁；重复启动直接退出（看护线程/开机脚本都可能拉它）
    from file_lock import acquire
    try:
        lock_file = acquire(ROOT / 'state' / 'repost.loop.lock')
    except (BlockingIOError, PermissionError, OSError):
        print('[启动] 搬运循环锁不可用，已有实例运行或目录不可写', flush=True)
        return
    rc = repost_cfg()
    prune_images()
    log(f'[启动] 搬运循环：{rc["forums"]} → {TARGET} 吧，间隔 {rc["intervalMinutes"]} 分钟，每天最多 {rc["perDay"]} 条')
    next_due = 0.0
    while True:
        try:
            rc = repost_cfg()
            if repost_control.stopped(CONFIG):
                heartbeat();time.sleep(1);continue
            # [2026-11-21] until 到期就自动停止循环（写配置 + 退出）
            _until = _until_at()
            if _until and local_now() > _until:
                # [2026-11-22] 用户要求：到点恢复原规则后继续跑，不再退出循环
                _disable_repost('到达 until 时间，自动停止')
                log('[恢复] 到点，已切回原规则，循环继续运行')
                next_due = 0.0
            forced = FLAG.exists()
            if forced:
                try:
                    FLAG.unlink()
                except Exception:
                    pass
            if forced or time.time() >= next_due:
                heartbeat()
                res = run_once(force=forced)
                detail = str((res or {}).get('detail') or '')
                if detail in ('不在活跃时段', '今天已到上限', 'repost 未启用') or not (res or {}).get('ok'):
                    wait = 120 if detail else max(120, rc['intervalMinutes'] * 60 * 0.4)
                else:
                    wait = max(20, rc['intervalMinutes'] * 60) * random.uniform(0.85, 1.1)
                next_due = time.time() + wait
                log(f'[调度] 下一轮 {int(wait // 60)} 分 {int(wait % 60)} 秒后（{detail or "已处理"}）')
        except Exception as e:
            log(f'[异常] {e}')
            next_due = time.time() + 120
        time.sleep(15)


if __name__ == '__main__':
    main()
