"""
hb_crypto.py — 小哈智电 (switchpowerapi.hellobike.com) 报文加解密 + 签名

全部参数来自小程序源码 mp/app-service.js（module 84 附近），已用抓包逐条验证：

  var l = {
    dev: {key: "1234567890123456", iv: "1234567890123456"},
    pro: {key: "0199bec97dfa5e0d", iv: "0199bec97dfa5e0d"},   // 线上
  }
  // 请求体：CryptoJS.AES.encrypt(明文, key, {iv})  → base64
  // 响应体：CryptoJS.AES.decrypt(密文, key, {iv})  → utf8
  // 签名：  signature = HmacSHA1(明文 + timestamp + nonce, "386a09ea946d46f1b4ca3b4e3df8de45")

要点：
  * AES-128-CBC/PKCS7，key 与 iv 相同且固定 → 同明文必然同密文（看起来像 ECB）
  * 签名算的是【明文】，不是密文
  * timestamp 是毫秒字符串，nonce 是 6 位随机数字字符串
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import hmac
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:  # 没装 pycryptodome：先试着挂载本机 venv 的 site-packages
    import glob
    import sys

    AES = pad = unpad = None
    for _sp in glob.glob(os.path.expanduser('~/.workbuddy/binaries/python/envs/*/lib/python*/site-packages')):
        if os.path.isdir(os.path.join(_sp, 'Crypto')):
            if _sp not in sys.path:
                sys.path.append(_sp)
            try:
                from Crypto.Cipher import AES
                from Crypto.Util.Padding import pad, unpad
                break
            except ImportError:
                continue

CRYPTO_HINT = ('缺少 pycryptodome，请先安装：\n'
               f'  {sys.executable} -m pip install '
               '-i https://pypi.tuna.tsinghua.edu.cn/simple pycryptodome')


def require_crypto():
    if AES is None:
        raise RuntimeError(CRYPTO_HINT)

ENV_KEYS = {
    'pro': ('0199bec97dfa5e0d', '0199bec97dfa5e0d'),
    'dev': ('1234567890123456', '1234567890123456'),
}
ENV_KEYS['fat'] = ENV_KEYS['dev']
ENV_KEYS['uat'] = ENV_KEYS['dev']
ENV_KEYS['pre'] = ENV_KEYS['dev']

SIGN_SECRET = b'386a09ea946d46f1b4ca3b4e3df8de45'
HOST = 'https://switchpowerapi.hellobike.com'
APPID = 'wx9d7f582059211357'
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 26_6_1 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.79(0x18004f30) NetType/WIFI Language/zh_CN')


# ------------------------------------------------------------------ 加解密
def _k(env: str):
    key, iv = ENV_KEYS[env if env in ENV_KEYS else 'pro']
    return key.encode(), iv.encode()


def decrypt(b64, env: str = 'pro') -> str:
    """密文(base64) -> 明文(utf8)"""
    require_crypto()
    s = b64.decode() if isinstance(b64, bytes) else b64
    s = ''.join(s.split())
    raw = base64.b64decode(s + '=' * (-len(s) % 4))
    key, iv = _k(env)
    return unpad(AES.new(key, AES.MODE_CBC, iv=iv).decrypt(raw), 16).decode()


def encrypt(plain: str, env: str = 'pro') -> str:
    """明文 -> 密文(base64)"""
    require_crypto()
    key, iv = _k(env)
    return base64.b64encode(AES.new(key, AES.MODE_CBC, iv=iv).encrypt(pad(plain.encode(), 16))).decode()


# ------------------------------------------------------------------ 签名
def sign(plain: str, secret: bytes = SIGN_SECRET, ts: str | None = None,
         nonce: str | None = None) -> tuple[str, str, str]:
    ts = ts or str(int(time.time() * 1000))
    nonce = nonce or str(random.randint(100000, 999999))
    sig = hmac.new(secret, (plain + ts + nonce).encode(), hashlib.sha1).hexdigest()
    return sig, ts, nonce


def verify_sign(plain: str, ts: str, nonce: str, signature: str,
                secret: bytes = SIGN_SECRET) -> bool:
    return hmac.new(secret, (plain + ts + nonce).encode(), hashlib.sha1).hexdigest() == signature.lower()


# ------------------------------------------------------------------ 抓包解析
@dataclass
class Capture:
    path: str
    kind: str        # request / response
    host: str
    start_line: str
    status: int
    headers: dict
    body: bytes

    @property
    def action(self) -> str:
        m = re.search(r'/api\?([\w.]+)', self.start_line)
        return m.group(1) if m else ''

    def plain(self, env: str = 'pro') -> dict:
        """解密 body；本来就是明文就直接 json 化"""
        txt = self.body.strip()
        if txt.startswith(b'{'):
            return json.loads(txt)
        return json.loads(decrypt(txt, env))


def _unchunk(b: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(b):
        j = b.find(b'\r\n', i)
        step = 2
        if j < 0:
            j, step = b.find(b'\n', i), 1
            if j < 0:
                break
        line = b[i:j].strip()
        if not line:
            i = j + step
            continue
        try:
            n = int(line.split(b';')[0], 16)
        except ValueError:
            break
        if n == 0:
            break
        out += b[j + step:j + step + n]
        i = j + step + n + step
    return bytes(out)


def parse_capture(path: str) -> Capture:
    raw = open(path, 'rb').read()
    sep = b'\r\n\r\n' if b'\r\n\r\n' in raw else b'\n\n'
    head_b, body = raw.split(sep, 1)
    lines = head_b.replace(b'\r\n', b'\n').decode('utf-8', 'replace').split('\n')
    headers = {}
    for ln in lines[1:]:
        if ':' in ln:
            k, v = ln.split(':', 1)
            headers[k.strip().lower()] = v.strip()
    status = 0
    m = re.search(r'HTTP/1\.[01] (\d{3})', lines[0])
    if m:
        status = int(m.group(1))
    if 'chunked' in headers.get('transfer-encoding', '').lower():
        body = _unchunk(body)
    if 'gzip' in headers.get('content-encoding', '').lower():
        try:
            body = gzip.decompress(body)
        except Exception:
            pass
    kind = 'request' if 'request' in os.path.basename(path) else 'response'
    return Capture(path, kind, headers.get('host', ''), lines[0].strip(), status, headers, body)
