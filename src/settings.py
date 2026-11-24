"""Standalone configuration, validation and local-only secret storage."""
from __future__ import annotations
import copy
import json
import os
import re
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
LOCK = threading.RLock()
DEFAULTS = {
    "forums": [], "dryRun": True, "pollSeconds": 600, "maxPostsPerDay": 3,
    "maxRepliesPerHour": 2, "minMinutesBetween": 40, "followThreads": 12,
    "maxPerRun": 1, "silenceRate": 0.65, "replyDelaySec": [180, 900],
    "manualDrafts": 6, "manualSilenceRate": 0.35,
    "activeHours": ["11:00-13:00", "22:00-02:00"], "timezone": "Asia/Shanghai",
    "persona": "persona/default.md", "blockedWords": ["身份证", "手机号", "开盒"],
    "auto": {"enabled": False, "review": True, "intensity": 2, "pausedUntil": ""},
    "api": {"profile": ""},
    "model": {"endpoint": "https://api.deepseek.com/v1", "model": "", "apiKeyEnv": "TIEBA_AGENT_API_KEY", "maxTokens": 300, "thinkingDisabled": False},
    "vision": {"enabled": False, "profile": "", "model": "", "timeoutSec": 45},
    "signIn": {"enabled": False, "atHour": 8, "forums": []},
    "browser": {"port": 3191, "headless": False, "channel": ""},
    "console": {"port": 3190},
    "repost": {"enabled": False, "target": "", "forums": [], "intervalMinutes": 30,
        "perDay": 12, "activeHours": ["11:00-23:00"], "until": "", "minReply": 30, "minPotential": 4,
        "maxImages": 9, "maxBodyChars": 1500, "storyFloors": 8, "includeSource": True,
        "sourceMode": "list", "sourceForums": [], "sourceBatch": 8,
        "judgeWithModel": True, "judgeProfile": "", "judgeModel": "",
        "altTarget": "", "altEvery": 0, "altForums": [], "altMinReply": 30, "altMinPotential": 4},
}

def merge(base, patch):
    out = copy.deepcopy(base)
    for key, value in patch.items():
        out[key] = merge(out.get(key, {}), value) if isinstance(value, dict) and isinstance(out.get(key, {}), dict) else copy.deepcopy(value)
    return out

def load():
    path = ROOT / "config.json"
    return merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8"))) if path.exists() else copy.deepcopy(DEFAULTS)

def atomic_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".save-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        try: os.chmod(tmp, 0o600)
        except OSError: pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def validate(cfg):
    if not isinstance(cfg, dict): raise ValueError("配置必须是对象")
    def walk(value, trail=""):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r"(?:api.?key|secret|bduss|cookie|password|token)$", key, re.I) and key not in ("apiKeyEnv", "maxTokens"):
                    raise ValueError("密钥和登录态请通过专用入口保存，不能放进公开配置")
                walk(item, trail + "." + key)
        elif isinstance(value, list):
            for item in value: walk(item, trail)
    walk(cfg)
    def integer(value, low, high, label):
        if type(value) is not int or not low <= value <= high: raise ValueError(f"{label} 应为 {low}–{high} 的整数")
    def hours(spans):
        if not isinstance(spans, list): raise ValueError("时段应是列表")
        for span in spans:
            if not isinstance(span, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", span):
                raise ValueError("时段格式应为 HH:MM-HH:MM，可跨午夜")
    for k in ["forums"]:
        if not isinstance(cfg.get(k), list) or any(not isinstance(x, str) or not x.strip() or len(x)>100 for x in cfg[k]): raise ValueError("吧名应为非空字符串列表")
    integer(cfg["pollSeconds"], 30, 86400, "扫描间隔")
    for k in ["maxPostsPerDay", "maxRepliesPerHour", "minMinutesBetween"]: integer(cfg[k], 0, 10000, k)
    for k in ["maxPerRun", "followThreads"]: integer(cfg[k], 1, 10000, k)
    if type(cfg["silenceRate"]) not in (float, int) or not 0 <= cfg["silenceRate"] <= 1: raise ValueError("沉默概率应在 0–1")
    delays = cfg["replyDelaySec"]
    if not isinstance(delays,list) or len(delays)!=2 or any(type(x) is not int or x<0 for x in delays) or delays[0]>delays[1]: raise ValueError("回复延迟应为递增的两个非负秒数")
    hours(cfg["activeHours"]); hours(cfg["repost"]["activeHours"])
    ZoneInfo(cfg["timezone"])
    p = (ROOT / cfg["persona"]).resolve()
    if ROOT.resolve() not in p.parents or p.suffix != ".md": raise ValueError("人格文件必须位于项目内且为 Markdown")
    integer(cfg["auto"]["intensity"], 1, 3, "强度")
    for group, keys in [(cfg["auto"],["enabled","review"]),(cfg["vision"],["enabled"]),(cfg["signIn"],["enabled"]),(cfg["repost"],["enabled","includeSource","judgeWithModel"]),(cfg["browser"],["headless"])]:
        for k in keys:
            if type(group[k]) is not bool: raise ValueError(k+" 必须是开关")
    for k in ["port"]:
        integer(cfg["browser"][k],1024,65535,"浏览器端口"); integer(cfg["console"][k],1024,65535,"控制台端口")
    if cfg["browser"]["port"] == cfg["console"]["port"]: raise ValueError("两个服务端口不能相同")
    if cfg["browser"]["channel"] not in ("","chrome","msedge"): raise ValueError("浏览器渠道只能为空、chrome 或 msedge")
    integer(cfg["signIn"]["atHour"],0,23,"签到小时")
    integer(cfg["model"]["maxTokens"],1,128000,"模型输出上限")
    integer(cfg["vision"]["timeoutSec"],1,300,"看图超时")
    integer(cfg["manualDrafts"],1,100,"手动草稿数")
    if type(cfg["manualSilenceRate"]) not in (float,int) or not 0<=cfg["manualSilenceRate"]<=1: raise ValueError("手动沉默概率应在 0–1")
    if not isinstance(cfg["signIn"]["forums"],list) or any(not isinstance(x,str) or not x.strip() for x in cfg["signIn"]["forums"]): raise ValueError("签到吧名应为列表")
    rc=cfg["repost"]
    for key in ["forums","sourceForums","altForums"]:
        if not isinstance(rc[key],list) or any(not isinstance(x,str) or not x.strip() for x in rc[key]): raise ValueError(key+" 应为吧名列表")
    for key in ["intervalMinutes","perDay","minReply","maxBodyChars","storyFloors","sourceBatch","altEvery","altMinReply"]: integer(rc[key],0,100000,key)
    for key in ["intervalMinutes","sourceBatch"]: integer(rc[key],1,100000,key)
    if rc["until"]:datetime.strptime(rc["until"],"%Y-%m-%d %H:%M")
    integer(rc["maxImages"],0,100,"图片上限")
    for key in ["minPotential","altMinPotential"]: integer(rc[key],1,5,key)
    if rc["sourceMode"] not in ["list","sitewide"]: raise ValueError("未知的源吧索引模式")
    if rc["enabled"] and (not rc["target"].strip() or not (rc["sourceForums"] if rc["sourceMode"]=="sitewide" else rc["forums"])): raise ValueError("启用搬运前请填写目标吧和源吧")
    return cfg

def save(cfg):
    with LOCK: atomic_json(ROOT / "config.json", validate(merge(DEFAULTS,cfg)))

def local_now():
    return datetime.now(ZoneInfo(load()["timezone"])).replace(tzinfo=None)
