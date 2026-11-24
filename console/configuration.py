"""Console setup endpoints. Secrets have write-only fields and live under ignored state/."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
import api
import settings
import repost_control as control

ROOT=settings.ROOT
def view():
    import tieba_browser
    try:browser=tieba_browser._api("health",timeout=2).get("data",{})
    except Exception:browser={"running":False,"browserOpen":False}
    return {"config":settings.load(),"defaults":settings.DEFAULTS,"profiles":api.list_profiles(),
        "accountConfigured":(ROOT/"state"/"auth.json").exists(),"browser":browser}

def configure(body):
    with settings.LOCK:
        cfg=settings.merge(settings.load(),body)
        settings.save(cfg)
        if 'repost' in body and 'enabled' in body['repost']:control.set_enabled(cfg['repost']['enabled'])
    return {"ok":True,"config":cfg,"restartRequired":"console" in body or "browser" in body}

def account(body):
    value=str(body.get("bduss", "")).strip()
    if not 20<=len(value)<=2048 or any(c.isspace() or c in ';\r\n' for c in value):raise ValueError("BDUSS 格式不正确")
    settings.atomic_json(ROOT/"state"/"auth.json",{"bduss":value})
    return {"ok":True,"accountConfigured":True}

def browser(action):
    import tieba_browser
    if action=="close":return tieba_browser._api("close")
    if action=="import-auth":
        cookies=tieba_browser._api("cdp",{"method":"Network.getCookies","params":{"urls":["https://tieba.baidu.com/"]}}).get("data",{}).get("cookies",[])
        bduss=next((c["value"] for c in cookies if c.get("name")=="BDUSS"), "")
        if not bduss:raise ValueError("专用浏览器尚未登录贴吧，请先扫码登录")
        return account({"bduss":bduss})
    if action not in ("start","login"):raise ValueError("未知浏览器操作")
    try:tieba_browser._api("health",timeout=2)
    except Exception:
        (ROOT/"state").mkdir(exist_ok=True)
        with (ROOT/"state"/"browser.log").open("a",encoding="utf-8") as log:
            subprocess.Popen([sys.executable,str(ROOT/"src"/"browser_service.py")],cwd=ROOT,stdout=log,stderr=log,
                creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        for _ in range(30):
            try:tieba_browser._api("health",timeout=1);break
            except Exception:time.sleep(.2)
        else:raise RuntimeError("浏览器服务未启动，请确认依赖安装成功并检查本地 browser.log")
    return tieba_browser._api("navigate",{"url":"https://tieba.baidu.com/"},timeout=90)

def persona(body=None):
    p=(ROOT/settings.load()["persona"]).resolve()
    if ROOT.resolve() not in p.parents or p.suffix!=".md":raise ValueError("人格文件路径无效")
    if body is not None:
        text=str(body.get("text", ""))
        if not 10<=len(text)<=30000:raise ValueError("人格文本应为 10–30000 字符")
        p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding="utf-8")
    return {"ok":True,"text":p.read_text(encoding="utf-8") if p.exists() else ""}

def ensure_repost_worker():
    import file_lock
    if control.stopped():return False
    try:lock=file_lock.acquire(ROOT/'state'/'repost.loop.lock')
    except (BlockingIOError,OSError):return True
    lock.close()
    with (ROOT/'state'/'repost.loop.log').open('a',encoding='utf-8') as log:
        subprocess.Popen([sys.executable,str(ROOT/'src'/'repost.py')],cwd=ROOT,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    return True

def repost_control(body):
    action=body.get('action')
    if action not in ('start','stop'):raise ValueError('操作只能是 start 或 stop')
    if action=='start':
        cfg=settings.merge(settings.load(),{'repost':{'enabled':True}});settings.validate(cfg)
        if not (ROOT/'state'/'auth.json').exists():raise ValueError('请先在设置页配置贴吧账号')
        if cfg['repost']['judgeWithModel']:
            model_cfg=settings.merge(cfg,{'api':{'profile':cfg['repost'].get('judgeProfile') or cfg['api']['profile']}})
            conn=api.resolve(model_cfg)
            if not conn.get('key') or not conn.get('model'):raise ValueError('请先配置搬运评分 API，或明确关闭模型评分')
        configure({'repost':{'enabled':True}});ensure_repost_worker()
    else:configure({'repost':{'enabled':False}})
    operation=control.status()
    return {'ok':True,'enabled':action=='start','operation':operation,
        'detail':('搬运已启动，将按时段、频率和配额运行' if action=='start' else '搬运已停止，待执行任务已取消；已经提交平台的请求仍需核对回执')}
