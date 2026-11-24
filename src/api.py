"""Self-contained OpenAI-compatible API profiles; credentials never leave local state."""
from __future__ import annotations
import os
import re
from urllib.parse import urlsplit
from pathlib import Path
import settings

ROOT = Path(__file__).resolve().parent.parent
LOCAL_ID = "__local__"
LOCAL_PROFILES = ROOT / "state" / "api-profiles.json"
LOCAL_KEY_FILE = ROOT / "state" / "api-key.txt"

def local_profiles():
    import json
    try: return json.loads(LOCAL_PROFILES.read_text(encoding="utf-8"))
    except FileNotFoundError: return {}

def validate_endpoint(value):
    uri=urlsplit(str(value).rstrip("/"))
    if uri.scheme not in ("https","http") or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
        raise ValueError("接口地址需为不含密钥、查询参数和账号密码的 HTTP(S) 地址")
    return uri.geturl()

def chat_url(endpoint):
    base=validate_endpoint(endpoint).rstrip("/")
    if base.endswith("/chat/completions"): return base
    return base+("/chat/completions" if re.search(r"/v\d+(?:beta\d*)?(?:/openai)?$",base) else "/v1/chat/completions")

def list_profiles():
    return [{"id":pid,"name":p.get("name") or pid,"baseUrl":p.get("endpoint", ""),"model":p.get("model", ""),"hasKey":bool(p.get("apiKey") or os.environ.get(p.get("apiKeyEnv", "")))} for pid,p in local_profiles().items()]

def resolve_by_id(pid, fallback_model=""):
    p=local_profiles().get(pid)
    if not p: return {}
    return {"endpoint":validate_endpoint(p["endpoint"]),"key":p.get("apiKey") or os.environ.get(p.get("apiKeyEnv", ""),""),"model":fallback_model or p.get("model", ""),"profile":pid,"source":"local-profile"}

def resolve(cfg):
    want=(cfg.get("api") or {}).get("profile", "")
    if want:
        result=resolve_by_id(want)
        if not result: raise ValueError("所选 API 配置不存在，请在控制台重新选择")
        return result
    model=cfg.get("model") or {}
    key=os.environ.get(model.get("apiKeyEnv") or "TIEBA_AGENT_API_KEY", "")
    if not key and LOCAL_KEY_FILE.exists(): key=LOCAL_KEY_FILE.read_text(encoding="utf-8").strip()
    return {"endpoint":validate_endpoint(model.get("endpoint") or "https://api.deepseek.com/v1"),"key":key,"model":model.get("model", ""),"profile":"","source":"environment" if key else "not-configured"}

def resolve_vision(vision_cfg):
    cfg=settings.load()
    if vision_cfg.get("profile"):cfg["api"]["profile"]=vision_cfg["profile"]
    connection=resolve(cfg)
    if vision_cfg.get("model"):connection["model"]=vision_cfg["model"]
    return connection

def save_profile(body):
    pid=str(body.get("id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}",pid): raise ValueError("API ID 只能包含字母数字、下划线和短横线")
    with settings.LOCK:
        profiles=local_profiles(); old=profiles.get(pid,{})
        if body.get("delete"):
            profiles.pop(pid,None)
        else:
            model=str(body.get("model", "")).strip()
            if not model: raise ValueError("请填写模型名称")
            key=str(body.get("apiKey", "")).strip() or old.get("apiKey", "")
            if any(c.isspace() for c in key): raise ValueError("API Key 不能包含空白字符")
            profiles[pid]={"name":str(body.get("name") or pid),"endpoint":validate_endpoint(body["endpoint"]),"model":model,"apiKey":key}
        settings.atomic_json(LOCAL_PROFILES,profiles)
    return {"ok":True,"profiles":list_profiles()}
