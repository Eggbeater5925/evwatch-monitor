"""终端与日志输出的脱敏工具。

这些值一旦打印就会被截图、贴进 issue、或者重定向进日志文件，所以统一不输出原值：
手机号、token / JWT、ticket、openid、deviceId、ssid、请求签名。

规则（Python 端和 `deploy/*.sh` 里的 bash 函数保持一致）：

    手机号   177****8414
    token    eyJhbG…9E74tw
    openid   …AbCdEf
    deviceId abc****

新代码要打印这些字段时走这里，不要直接 f-string 拼原值。
"""
from __future__ import annotations


def phone(value) -> str:
    """手机号：177****8414"""
    p = str(value or '').strip()
    if not p:
        return ''
    return f'{p[:3]}****{p[-4:]}' if len(p) >= 7 else '*' * len(p)


def token(value, head: int = 6, tail: int = 4) -> str:
    """JWT / ticket：eyJhbG…9E74tw

    JWT 的前缀是固定的 base64 header，本来就没有信息量；留头尾只为确认「是不是这一份」。
    """
    t = str(value or '').strip()
    if not t:
        return ''
    if len(t) <= head + tail + 1:
        return '*' * len(t)
    return f'{t[:head]}…{t[-tail:]}'


def openid(value, tail: int = 6) -> str:
    """微信 openid：…AbCdEf"""
    o = str(value or '').strip()
    if not o:
        return ''
    return f'…{o[-tail:]}' if len(o) > tail + 4 else '*' * len(o)


def device_id(value) -> str:
    """会话配对标识：abc****（留前 3 位够确认是哪一台设备）"""
    d = str(value or '').strip()
    if not d:
        return ''
    return d[:3] + '*' * min(4, max(1, len(d) - 3))


def ssid(value) -> str:
    """微信会话 id（每次登录随机）"""
    return token(value, head=4, tail=4)


# 报文里出现这些键就打码（--dry-run 打印请求体时用）
_FIELDS = {
    'mobile': phone,
    'cellphone': phone,
    'phone': phone,
    'phoneNumber': phone,
    'token': token,
    'ticket': token,
    'authorization': token,
    'Authorization': token,
    'sign': token,
    'signature': token,
    'ssid': ssid,
    'openid': openid,
    'open_id': openid,
    'openId': openid,
    'miniProgramOpenId': openid,
    'deviceId': device_id,
    'device_id': device_id,
}


def payload(obj):
    """把报文 dict 里的敏感字段换成脱敏值，返回浅拷贝（不改原对象）。

    用于 `--dry-run`：结构照样能核对，但手机号 / 签名不会留在终端回滚缓冲里。
    """
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        fn = _FIELDS.get(k)
        out[k] = fn(v) if (fn and v is not None) else v
    return out
