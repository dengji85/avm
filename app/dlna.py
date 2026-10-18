# -*- coding: utf-8 -*-
"""DLNA 投屏：在局域网里找「媒体渲染器」（电视 / 盒子 / 播放器 App）并把影片推过去。

实现要点：
* 发现：组播 M-SEARCH（239.255.255.250:1900）。**Docker 默认网络看不到局域网组播**，
  因此同时支持「按 IP 单播探测」——两种方式都拿得到设备描述地址（LOCATION）。
* 描述：拉取 LOCATION 的 XML，解析厂商 / 型号 / 友好名，并定位 AVTransport 控制地址。
* 控制：SOAP 调用 AVTransport 的 SetAVTransportURI + Play / Stop。
* 推给电视的媒体地址用**服务端签发的短时令牌**（cast token），不暴露访问令牌。

依赖仅标准库；容器 / NAS / Windows 桌面都能跑，只是组播发现只在能收到
局域网组播的环境有效（桌面 exe、host 网络模式），其他情况用「按 IP 探测」。
"""
from __future__ import annotations

import re
import socket
import threading
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from xml.sax import saxutils

import requests

SSDP_ADDR = ("239.255.255.250", 1900)
_MSEARCH = (
    "M-SEARCH * HTTP/1.1\r\n"
    "HOST: 239.255.255.250:1900\r\n"
    "MAN: \"ssdp:discover\"\r\n"
    "MX: 2\r\n"
    "ST: upnp:urn:schemas-upnp-org:device:MediaRenderer:1\r\n"
    "\r\n"
).encode("ascii")

_CACHE_LOCK = threading.Lock()
_DEVICES: Dict[str, Dict[str, Any]] = {}   # usn -> 设备信息


def _now() -> float:
    return time.time()


def _ssdp_once(addr, timeout: float) -> List[Dict[str, str]]:
    """发一次 M-SEARCH 并收集响应（组播或单播到指定地址）。"""
    out: Dict[str, Dict[str, str]] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.4)
    try:
        s.sendto(_MSEARCH, addr)
    except OSError:
        s.close()
        return []
    deadline = _now() + timeout
    while _now() < deadline:
        try:
            data, _addr = s.recvfrom(2048)
        except socket.timeout:
            break
        except OSError:
            break
        text = data.decode("latin-1", "replace")
        headers: Dict[str, str] = {}
        for line in text.split("\r\n")[1:]:
            k, _, v = line.partition(":")
            if k.strip():
                headers[k.strip().lower()] = v.strip()
        usn = headers.get("usn") or headers.get("_udn") or ""
        loc = headers.get("location") or ""
        if usn and loc and usn not in out:
            out[usn] = {"usn": usn, "location": loc, "st": headers.get("st", "")}
    s.close()
    return list(out.values())


def describe(location: str, timeout: float = 4.0) -> Dict[str, Any]:
    """拉取设备描述 XML，提取名称与 AVTransport 控制地址。"""
    r = requests.get(location, timeout=(3, timeout))
    r.raise_for_status()
    # UPnP 描述是 UTF-8，但很多设备不写 charset —— 不指定会被 requests 按
    # text/* 默认成 latin-1，中文设备名直接变乱码
    r.encoding = "utf-8"
    xml = r.text
    name = re.search(r"<friendlyName>(.*?)</friendlyName>", xml, re.S | re.I)
    maker = re.search(r"<manufacturer>(.*?)</manufacturer>", xml, re.S | re.I)
    model = re.search(r"<modelName>(.*?)</modelName>", xml, re.S | re.I)
    control = re.search(
        r"<service>.*?<serviceType>urn:schemas-upnp-org:service:AVTransport[^<]*</serviceType>"
        r".*?<controlURL>(.*?)</controlURL>.*?</service>", xml, re.S | re.I)
    base = "%s://%s" % (urlparse(location).scheme, urlparse(location).netloc)
    ctrl = control.group(1).strip() if control else ""
    if ctrl.startswith("/"):
        ctrl = base + ctrl
    return {
        "location": location,
        "name": (name.group(1).strip() if name else "DLNA 设备"),
        "manufacturer": (maker.group(1).strip() if maker else ""),
        "model": (model.group(1).strip() if model else ""),
        "control": ctrl,
    }


def _cache(info: Dict[str, Any], usn: str = "") -> Dict[str, Any]:
    key = usn or info.get("usn") or info["location"]
    with _CACHE_LOCK:
        _DEVICES[key] = dict(info, usn=key)
        return dict(_DEVICES[key])


def discover(timeout: float = 2.5) -> List[Dict[str, Any]]:
    """组播发现（收不到组播时返回空，提示用户改用按 IP 探测）。"""
    found: List[Dict[str, Any]] = []
    usns = set()
    for _ in range(2):
        for item in _ssdp_once(SSDP_ADDR, timeout / 2):
            if item["usn"] in usns:
                continue
            usns.add(item["usn"])
            try:
                info = _cache(describe(item["location"]), item["usn"])
                found.append(info)
            except Exception:
                continue
    return found


def probe_ip(ip: str, port: int = 1900) -> Dict[str, Any]:
    """向指定 IP 单播 M-SEARCH（容器 / 跨网段时用）。"""
    ip = (ip or "").strip()
    if not re.match(r"^[0-9a-zA-Z.\-_]+$", ip) or "/" in ip:
        raise ValueError("IP 格式不正确")
    items = _ssdp_once((ip, int(port)), 1.5)
    if not items:
        raise ValueError("未收到响应：该地址没有 DLNA 渲染器（或端口不是 1900）")
    return _cache(describe(items[0]["location"]), items[0]["usn"])


def list_cached() -> List[Dict[str, Any]]:
    with _CACHE_LOCK:
        return [dict(v) for v in _DEVICES.values()]


def _soap(control: str, action: str, body_xml: str, timeout: float = 8.0) -> str:
    envelope = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>%s</s:Body></s:Envelope>" % body_xml
    )
    r = requests.post(
        control, data=envelope.encode("utf-8"),
        headers={
            "Content-Type": "text/xml; charset=\"utf-8\"",
            "SOAPACTION": '"urn:schemas-upnp-org:service:AVTransport:1#%s"' % action,
        },
        timeout=(4, timeout))
    if r.status_code >= 400:
        raise ValueError("设备返回 HTTP %d：%s" % (r.status_code, r.text[:160]))
    fault = re.search(r"<faultstring>(.*?)</faultstring>", r.text, re.S | re.I)
    if fault:
        raise ValueError("设备错误：%s" % fault.group(1)[:160])
    return r.text


def _didl(title: str) -> str:
    """构造 DIDL-Lite 元数据。返回**未转义的原始 XML**，嵌入 SOAP 前整体转义一次。"""
    safe = title or "AVM"
    return (
        '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
        '<item id="0" parentID="-1" restricted="1">'
        "<dc:title>%s</dc:title><upnp:class>object.item.videoItem</upnp:class>"
        "</item></DIDL-Lite>" % safe
    )


def cast(usn: str, media_url: str, title: str = "") -> Dict[str, Any]:
    with _CACHE_LOCK:
        dev = _DEVICES.get(usn)
    if not dev:
        raise ValueError("设备不在列表里，请重新发现")
    if not dev.get("control"):
        raise ValueError("该设备没有可用的 AVTransport 控制地址")
    # CurrentURIMetaData 是「一段 XML 字符串」放在 SOAP 文本节点里：
    # 先拼出合法 DIDL，再整体转义一次；对已转义过的内容再转义会得到 &amp;amp; 并弄坏标签
    meta = saxutils.escape(_didl(title))
    _soap(dev["control"], "SetAVTransportURI",
          '<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
          "<InstanceID>0</InstanceID><CurrentURI>%s</CurrentURI>"
          "<CurrentURIMetaData>%s</CurrentURIMetaData>"
          "</u:SetAVTransportURI>" % (saxutils.escape(media_url), meta))
    _soap(dev["control"], "Play",
          '<u:Play xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
          "<InstanceID>0</InstanceID><Speed>1</Speed></u:Play>")
    return {"ok": True, "name": dev.get("name"), "usn": usn}


def stop(usn: str) -> Dict[str, Any]:
    with _CACHE_LOCK:
        dev = _DEVICES.get(usn)
    if not dev or not dev.get("control"):
        raise ValueError("设备不在列表里，请重新发现")
    _soap(dev["control"], "Stop",
          '<u:Stop xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
          "<InstanceID>0</InstanceID></u:Stop>")
    return {"ok": True}


# ------------------------------------------------------------------ 投屏短时令牌
_TOKENS_LOCK = threading.Lock()
_TOKENS: Dict[str, Dict[str, Any]] = {}
TOKEN_TTL = 600          # 投屏链接有效期（秒）：足够电视发起请求
TOKEN_MAX = 200


def make_token(movie_id: int, base: str) -> Dict[str, Any]:
    """为一次投屏签发短时令牌，返回电视要访问的媒体地址。"""
    tok = uuid.uuid4().hex
    now = _now()
    with _TOKENS_LOCK:
        if len(_TOKENS) >= TOKEN_MAX:
            for k in [k for k, v in _TOKENS.items() if v["exp"] < now][: TOKEN_MAX // 4]:
                _TOKENS.pop(k, None)
        _TOKENS[tok] = {"movie_id": int(movie_id), "exp": now + TOKEN_TTL}
    return {"token": tok, "media_url": "%s/api/cast/media/%s" % (base.rstrip("/"), tok)}


def token_movie(tok: str) -> Optional[int]:
    with _TOKENS_LOCK:
        item = _TOKENS.get(tok)
        if not item:
            return None
        if item["exp"] < _now():
            _TOKENS.pop(tok, None)
            return None
        # 滑动续期：电视播放中会持续发 Range 请求，令牌随访问顺延，
        # 否则 10 分钟后过期会让一场两小时的电影中途断流。
        item["exp"] = _now() + TOKEN_TTL
        return int(item["movie_id"])
