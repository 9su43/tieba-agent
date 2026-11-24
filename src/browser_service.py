"""Project-owned, token-protected Playwright session. No personal browser profile is imported."""
from __future__ import annotations
import base64
import json
import mimetypes
import secrets
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit
import settings

ROOT=settings.ROOT
TOKEN_FILE=ROOT/"state"/"browser-token.txt"

def token():
    TOKEN_FILE.parent.mkdir(parents=True,exist_ok=True)
    if not TOKEN_FILE.exists():
        fd=TOKEN_FILE.open("x",encoding="utf-8");fd.write(secrets.token_urlsafe(32));fd.close()
        try:TOKEN_FILE.chmod(0o600)
        except OSError:pass
    return TOKEN_FILE.read_text(encoding="utf-8").strip()

def checked_url(value):
    u=urlsplit(str(value))
    if u.scheme!="https" or u.username or u.password or not (u.hostname=="baidu.com" or str(u.hostname).endswith(".baidu.com")):
        raise ValueError("登录浏览器仅允许访问百度/贴吧 HTTPS 页面")
    return u.geturl()

class Browser:
    def __init__(self):self.context=None;self.pw=None;self.page=None;self.clipboard=None
    def ensure(self):
        if self.context:return
        from playwright.sync_api import sync_playwright
        self.pw=sync_playwright().start()
        config=settings.load()["browser"]
        options={"channel":config["channel"]} if config.get("channel") else {}
        self.context=self.pw.chromium.launch_persistent_context(str(ROOT/"state"/"browser-profile"),headless=config["headless"],viewport={"width":1280,"height":900},**options)
        auth=ROOT/'state'/'auth.json'
        if auth.exists():
            bduss=json.loads(auth.read_text(encoding='utf-8')).get('bduss','')
            if bduss:self.context.add_cookies([{'name':'BDUSS','value':bduss,'domain':'.baidu.com','path':'/','secure':True,'httpOnly':True}])
        self.page=self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.on("dialog",lambda d:d.dismiss())
    def command(self,action,args):
        if action=="health":return {"running":True,"browserOpen":self.context is not None}
        if action=="close":
            self.close();self.context=None;self.pw=None;self.page=None;return {"browserOpen":False}
        self.ensure()
        if action=="list_tabs":return {"tabs":[{"id":i,"url":p.url} for i,p in enumerate(self.context.pages)]}
        if action=="navigate":self.page.goto(checked_url(args["url"]),wait_until="domcontentloaded",timeout=60000);return {"url":self.page.url}
        if action=="activate_tab":self.page.bring_to_front();return {}
        if action=="evaluate":return {"value":self.page.evaluate(args["code"])}
        if action=="click":self.page.locator(args["selector"]).first.click(timeout=15000);return {}
        if action=="set_clipboard_text":self.clipboard=("text",str(args.get("text", "")));return {}
        if action=="set_clipboard_image":
            p=Path(args["path"]).resolve();allowed=(ROOT/"state"/"repost-images").resolve()
            if allowed not in p.parents or not p.is_file() or p.stat().st_size>20*1024*1024:raise ValueError("图片必须是本项目下载的、20MiB 以内的图片")
            self.clipboard=("image",p);return {}
        if action=="send_keys":
            keys=args.get("keys", "")
            if keys=="Mod+V" and self.clipboard:
                kind,value=self.clipboard
                if kind=="text":self.page.keyboard.insert_text(value)
                else:
                    inputs=self.page.locator('input[type="file"][accept*="image"]')
                    if inputs.count():inputs.last.set_input_files(str(value))
                    else:
                        payload={"data":base64.b64encode(value.read_bytes()).decode(),"name":value.name,"mime":mimetypes.guess_type(value.name)[0] or "image/png"}
                        self.page.evaluate("""p=>{const e=document.activeElement;if(!e)throw Error('没有聚焦的编辑器');const bytes=Uint8Array.from(atob(p.data),c=>c.charCodeAt(0));const dt=new DataTransfer();dt.items.add(new File([bytes],p.name,{type:p.mime}));e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:dt,bubbles:true,cancelable:true}));}""",payload)
            else:self.page.keyboard.press(keys.replace("Mod+","ControlOrMeta+"))
            return {}
        if action=="cdp":
            method=args.get("method");params=args.get("params") or {}
            if method=="Network.getCookies":
                urls=[checked_url(u) for u in params.get("urls",["https://tieba.baidu.com/"])]
                return {"cookies":self.context.cookies(urls)}
            if method not in {"Page.bringToFront","Page.handleJavaScriptDialog","Emulation.setFocusEmulationEnabled"}:raise ValueError("不支持的浏览器命令")
            session=self.context.new_cdp_session(self.page)
            try:return session.send(method,params)
            finally:session.detach()
        raise ValueError("不支持的浏览器操作")
    def close(self):
        if self.context:self.context.close()
        if self.pw:self.pw.stop()

BROWSER=Browser()
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        if self.path!="/command" or not secrets.compare_digest(self.headers.get("Authorization", ""),"Bearer "+token()):return self.respond(401,{"ok":False,"error":"unauthorized"})
        try:
            size=int(self.headers.get("Content-Length", "0"))
            if not 0<size<=1024*1024:raise ValueError("请求过长或为空")
            body=json.loads(self.rfile.read(size))
            result=BROWSER.command(body["action"],body.get("args") or {})
            self.respond(200,{"ok":True,"data":result})
        except Exception as e:self.respond(400,{"ok":False,"error":str(e)[:300]})
    def respond(self,status,value):
        raw=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)

def main():
    token();server=HTTPServer(("127.0.0.1",settings.load()["browser"]["port"]),Handler)
    try:server.serve_forever()
    finally:BROWSER.close();server.server_close()
if __name__=="__main__":main()
