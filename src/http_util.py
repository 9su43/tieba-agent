"""HTTP 请求辅助：直连优先，必要时使用使用者配置的系统代理。写入请求禁止自动网络重发。"""
from __future__ import annotations

import urllib.error
import urllib.request

_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_VIA_PROXY = urllib.request.build_opener()

# HTTP 状态码话，错误信息要带服务端原文（例如 403 model_not_allowed），
# 否则只会显示 "HTTP Error 403: Forbidden"，看不出是权限问题还是网络问题。
_BODY_HINT = 300


def _http_error(stage: str, e: urllib.error.HTTPError) -> RuntimeError:
    body = ""
    try:
        body = e.read()[:_BODY_HINT].decode("utf-8", "replace")
    except Exception:
        pass
    return RuntimeError(f"{stage}失败（HTTP {e.code}）：{body or e.reason}")


def open_url(req, timeout: int = 30, stage: str = "请求", allow_fallback: bool = True):
    try:
        return _NO_PROXY.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        # 4xx 是服务端在拒绝（鉴权/模型权限/参数），换代理重试没有意义
        if not allow_fallback or 400 <= e.code < 500:
            raise _http_error(stage, e) from e
        err = e
    except Exception as e1:
        if not allow_fallback:
            raise RuntimeError(f"{stage}失败（直连）：{e1}") from e1
        err = e1
    try:
        return _VIA_PROXY.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise _http_error(stage, e) from e
    except Exception:
        raise RuntimeError(f"{stage}失败（直连+代理都断）：{err}") from err
