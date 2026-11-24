"""通过项目独立的 Playwright 浏览器操作贴吧，使用本项目专用登录资料。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import time

ROOT = Path(__file__).resolve().parent.parent
SESSION = 'tieba-main'


class BridgeError(RuntimeError):
    pass


def _api(action: str, args: dict | None = None, timeout: int = 90) -> dict:
    import settings
    port = settings.load()['browser']['port']
    token_file = ROOT / 'state' / 'browser-token.txt'
    if not token_file.exists():
        raise BridgeError('请先在控制台的设置页启动登录浏览器')
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        body=json.dumps({'action':action,'args':args or {}}).encode('utf-8')
        conn.request('POST','/command',body=body,headers={'Content-Type':'application/json','Authorization':'Bearer '+token_file.read_text(encoding='utf-8').strip()})
        result=json.loads(conn.getresponse().read().decode('utf-8'))
        if not result.get('ok'):raise BridgeError(result.get('error','浏览器操作失败'))
        return result
    except OSError as error:
        raise BridgeError('项目浏览器未运行，请在控制台启动浏览器') from error
    finally:conn.close()


def _ev(code: str) -> str:
    return (_api('evaluate', {'code': code}).get('data') or {}).get('value', '')


def available() -> bool:
    try:
        _api('list_tabs', {}, timeout=5)
        return True
    except Exception:
        return False


_FILL_JS = r"""
(function(){
  var want = %s;
  var out = {ok: false, err: ''};
  var pick = {title: null, body: null};
  document.querySelectorAll('.ql-container').forEach(function(c){
    var ed = c.querySelector('.ql-editor');
    if (!ed || ed.offsetParent === null) return;
    var ph = ed.getAttribute('data-placeholder') || '';
    if (ph.indexOf('标题') >= 0) pick.title = ed;
    else if (ph.indexOf('正文') >= 0) pick.body = ed;
  });
  if (!pick.title || !pick.body) { out.err = 'no_editor'; return JSON.stringify(out); }
  function put(el, text){
    el.focus();
    document.execCommand('selectAll', false, null);
    document.execCommand('insertText', false, text);
  }
  put(pick.title, want.title);
  put(pick.body, want.body);
  out.ok = true;
  out.titleLen = (pick.title.innerText||'').length;
  out.bodyLen = (pick.body.innerText||'').length;
  return JSON.stringify(out);
})()
"""

_PUBLISH_JS = r"""
(function(){
  var btns = Array.from(document.querySelectorAll('div,button')).filter(function(el){
    var t = (el.textContent||'').trim();
    return (t === '发布' || t === '发布帖子') && el.offsetParent !== null && el.getBoundingClientRect().width > 30;
  });
  if (!btns.length) return 'no_publish_btn';
  var b = btns[btns.length - 1];
  b.click();
  return 'clicked';
})()
"""

_STATE_JS = r"""
(function(){
  var o = {
    url: location.href,
    editor: document.querySelectorAll('.ql-editor').length,
    vcode: (document.body.innerText || '').indexOf('验证码') >= 0,
    vcodeInput: document.querySelectorAll('input[placeholder*="验证码"]').length,
    toast: ''
  };
  var t = document.querySelector('[class*=toast],[class*=message],[class*=tip]');
  if (t) o.toast = (t.innerText || '').trim().slice(0, 80);
  return JSON.stringify(o);
})()
"""


_OWN_FILL = {"at": 0.0, "title": "", "body": ""}
_OWN_FILL_FILE = Path(__file__).resolve().parent.parent / 'state' / 'browser-own-fill.json'
# [2026-11-22] 以前 _OWN_FILL 只在内存里，程序一重启就丢——上一轮失败留下的
# 发帖框会被当成"用户在编辑"，于是 safe_navigate 永远抛 busy，搬贴卡死。
try:
    if _OWN_FILL_FILE.exists():
        _OWN_FILL.update(json.loads(_OWN_FILL_FILE.read_text(encoding='utf-8')))
except Exception:
    pass


def _remember_own_fill(title: str, body: str) -> None:
    _OWN_FILL.update({'at': time.time(), 'title': str(title), 'body': str(body)})
    try:
        _OWN_FILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        _OWN_FILL_FILE.write_text(json.dumps(_OWN_FILL, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass


def composer_state() -> dict:
    """当前页面上的发帖/回帖框状态：有没有打开、里面有没有字。"""
    try:
        raw = _ev("""(function(){
          var e=document.querySelector('.ql-editor');
          if(!e||e.offsetParent===null) return JSON.stringify({open:false,text:''});
          return JSON.stringify({open:true,text:(e.innerText||'').trim().slice(0,200)});
        })()""")
        return json.loads(raw or '{}') or {"open": False, "text": ""}
    except Exception:
        return {"open": False, "text": ""}


def _is_ours(text: str) -> bool:
    """框里的字是不是我们刚填的（15 分钟内且对得上）。"""
    own = _OWN_FILL
    if not own["at"] or time.time() - own["at"] > 900:
        return False
    t = str(text or "").replace("\n", "").strip()
    want = (own["title"] + own["body"]).replace("\n", "").strip()
    return bool(t) and bool(want) and (t[:40] in want or want[:40] in t)


def clear_composer() -> None:
    """把我们自己留下的草稿清掉（不点发布）。"""
    try:
        _ev("""(function(){
          var e=document.querySelector('.ql-editor');
          if(e){e.focus();e.innerHTML='<p><br></p>';e.dispatchEvent(new Event('input',{bubbles:true}));}
          return 'ok';
        })()""")
    except Exception:
        pass
    try:
        _api('click', {'selector': '.tb-editor-wrapper .close, .tb-editor-close, .edui-close'}, timeout=8)
    except Exception:
        pass


def clear_guard() -> None:
    """清掉页面的 beforeunload 守卫：上一个发帖框还没提交时直接导航，Chrome 会弹
    "重新加载站点？你所做的更改可能未保存" 把自动化卡死（2026-11-05 实测）。"""
    # 对话框如果已经卡在页面上，evaluate 会直接报 "JavaScript dialog is open"，所以先试着接受它
    try:
        _api('cdp', {'method': 'Page.handleJavaScriptDialog', 'params': {'accept': True}}, timeout=8)
    except Exception:
        pass
    try:
        _ev('window.onbeforeunload=null;window.onunload=null;')
    except Exception:
        pass


def safe_navigate(url: str, wait: float = 0.0) -> None:
    clear_guard()
    st = composer_state()
    if st.get("open"):
        text = str(st.get("text") or "").strip()
        if not text:
            # 空框也先关掉：贴吧的页面守卫只要有发帖框就可能在离开时弹"未保存"
            clear_composer()
            time.sleep(0.4)
        elif _is_ours(text):
            clear_composer()
            time.sleep(0.5)
        else:
            # 用户在浏览器里正写着帖子：别抢页面、别弹"未保存"框
            raise RuntimeError("浏览器里有人正在编辑帖子，这轮先不导航")
    _api('navigate', {'url': url})
    if wait:
        time.sleep(wait)


def new_thread(kw: str, title: str, content: str, section: str = '', wait_publish: int = 25) -> dict:
    """在浏览器里发一条主题帖。返回 {ok, via, url, need_vcode, detail}。"""
    # 1) 打开发帖页（顺便重置掉可能残留的发帖框）
    safe_navigate(f'https://tieba.baidu.com/f?kw={kw}', wait=6)

    # 2) 打开编辑器（必须用桥的原生点击，页面内 JS click 不触发）
    _api('click', {'selector': '.add-btn'})
    time.sleep(4)
    st = json.loads(_ev(_STATE_JS) or '{}')
    if not st.get('editor'):
        return {'ok': False, 'via': 'browser', 'need_vcode': False,
                'detail': '发帖框没打开（浏览器可能没登录贴吧）'}

    # 3) 选版块（可选）
    if section:
        _ev("(function(){var a=Array.prototype.slice.call(document.querySelectorAll('div,span'))"
            f".filter(function(e){{return (e.textContent||'').trim()==={json.dumps(section, ensure_ascii=False)}"
            "&&e.offsetParent!==null;}});if(a.length){a[0].click();return 'ok';}return 'no';})()")
        time.sleep(1)

    # 5) 填内容
    fill = json.loads(_ev(_FILL_JS % json.dumps({'title': title, 'body': content}, ensure_ascii=False)) or '{}')
    _remember_own_fill(title, content)
    if not fill.get('ok'):
        return {'ok': False, 'via': 'browser', 'need_vcode': False,
                'detail': f"填内容失败：{fill.get('err')}"}
    time.sleep(1)

    # 6) 点发布
    clicked = _ev(_PUBLISH_JS)
    if clicked != 'clicked':
        return {'ok': False, 'via': 'browser', 'need_vcode': False, 'detail': '找不到发布按钮'}

    # 7) 等结果
    for _ in range(wait_publish):
        time.sleep(1)
        st = json.loads(_ev(_STATE_JS) or '{}')
        if st.get('vcode') or st.get('vcodeInput'):
            return {'ok': False, 'via': 'browser', 'need_vcode': True,
                    'detail': '需要验证码：请在这个浏览器里点一下验证，然后重发'}
        if st.get('editor', 0) == 0:
            # 发帖框关了，大概率发成功
            time.sleep(2)
            return {'ok': True, 'via': 'browser', 'url': _ev('location.href'),
                    'need_vcode': False, 'detail': import_ok_detail()}
    return {'ok': False, 'via': 'browser', 'need_vcode': False,
            'detail': '点了发布但发帖框没关，可能被拦了（去浏览器看看）'}


def import_ok_detail() -> str:
    return '已通过浏览器发布'
