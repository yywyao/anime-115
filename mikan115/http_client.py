import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from .errors import ProviderError


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (old.scheme,old.netloc) != (new.scheme,new.netloc):
            raise ProviderError('UPSTREAM_REDIRECT','上游返回了跨站重定向，已停止发送凭据')
        return super().redirect_request(req,fp,code,msg,headers,newurl)


class HTTP:
    """No logging of URLs, request bodies, credentials or provider responses."""
    def __init__(self,timeout=20):
        self.timeout=timeout
        self.opener=urllib.request.build_opener(SameOriginRedirect())

    def open(self,method,url,*,body=None,form=None,headers=None):
        h={'User-Agent':'Mozilla/5.0 Mikan115/0.1','Accept':'application/json',**(headers or {})}
        payload=None
        if body is not None:
            payload=json.dumps(body,ensure_ascii=False).encode();h['Content-Type']='application/json'
        if form is not None:
            payload=urllib.parse.urlencode(form,doseq=True).encode();h['Content-Type']='application/x-www-form-urlencoded'
        req=urllib.request.Request(url,data=payload,headers=h,method=method)
        try:
            return self.opener.open(req,timeout=self.timeout)
        except urllib.error.HTTPError as e:
            e.close()
            raise ProviderError('UPSTREAM_HTTP_ERROR','上游请求失败，请检查连接状态',uncertain=method not in ('GET','HEAD'))
        except (TimeoutError,socket.timeout,urllib.error.URLError,ConnectionError,OSError):
            raise ProviderError('UPSTREAM_UNAVAILABLE','上游连接超时或暂时不可用',uncertain=method not in ('GET','HEAD'))

    def bytes(self,method,url,*,limit=8*1024*1024,**kwargs):
        with self.open(method,url,**kwargs) as response:
            raw=response.read(limit+1)
            if len(raw)>limit:raise ProviderError('UPSTREAM_TOO_LARGE','上游响应超过大小限制')
            return raw

    def json(self,method,url,*,allow_list=False,**kwargs):
        try:
            data=json.loads(self.bytes(method,url,**kwargs))
        except (ValueError,UnicodeError):
            raise ProviderError('UPSTREAM_INVALID_JSON','上游返回数据格式异常',uncertain=method not in ('GET','HEAD'))
        if not isinstance(data,(dict,list)) or (isinstance(data,list) and not allow_list):
            raise ProviderError('UPSTREAM_INVALID_JSON','上游返回数据格式异常',uncertain=method not in ('GET','HEAD'))
        return data
