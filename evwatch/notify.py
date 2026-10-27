"""
notify.py — 钉钉自定义机器人推送

两种安全模式都支持：
  * 关键词：消息正文里必须含关键词（这里是「电池」），否则钉钉返回 errcode=310000
  * 加签：如果机器人开了「加签」，用 --secret 传密钥，会自动拼 timestamp & sign

用法：
  from evwatch.notify import dingtalk
  dingtalk('【电池】电量 18%', webhook=URL)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request

from .net import urlopen as _urlopen

KEYWORD = '电池'


def _signed_url(url: str, secret: str) -> str:
    ts = str(round(time.time() * 1000))
    string_to_sign = f'{ts}\n{secret}'
    h = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(h))
    sep = '&' if '?' in url else '?'
    return f'{url}{sep}timestamp={ts}&sign={sign}'


def dingtalk(text: str, webhook: str | None = None, secret: str | None = None,
             at_all: bool = False) -> dict:
    """发一条文本消息；正文不含关键词时自动补上，避免被钉钉拒收"""
    if not webhook:
        raise ValueError('缺少钉钉 webhook')
    if KEYWORD not in text:
        text = f'【{KEYWORD}】{text}'

    body = {'msgtype': 'text', 'text': {'content': text}}
    if at_all:
        body['at'] = {'isAtAll': True}

    url = _signed_url(webhook, secret) if secret else webhook
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json; charset=utf-8'},
        method='POST',
    )
    with _urlopen(req, timeout=10) as r:
        res = json.loads(r.read().decode())
    if res.get('errcode') != 0:
        raise RuntimeError(f"钉钉拒收: errcode={res.get('errcode')} errmsg={res.get('errmsg')}")
    return res


def push(text: str, dingtalk_webhook: str | None = None, dingtalk_secret: str | None = None,
         wecom_webhook: str | None = None) -> bool:
    """钉钉优先，其次企业微信机器人；都没配就只打日志。

    两个 watcher（电池 / 充电桩）共用这一份，避免各写一遍。
    """
    ok = False
    if dingtalk_webhook:
        try:
            dingtalk(text, webhook=dingtalk_webhook, secret=dingtalk_secret)
            print('   钉钉已推送')
            ok = True
        except Exception as e:
            print('   钉钉推送失败:', e)
    if wecom_webhook:
        try:
            _urlopen(urllib.request.Request(
                wecom_webhook,
                data=json.dumps({'msgtype': 'text', 'text': {'content': text}}).encode(),
                headers={'Content-Type': 'application/json'}), timeout=10)
            print('   企业微信已推送')
            ok = True
        except Exception as e:
            print('   企业微信推送失败:', e)
    if not ok:
        print('   (未配置推送通道，仅本地输出)')
    return ok
