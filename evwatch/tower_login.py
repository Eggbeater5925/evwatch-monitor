"""
tower_login.py — 中国铁塔充电「短信验证码登录」，换一份新的 Authorization（JWT）

为什么要它：铁塔是**单点登录**，手机上再进一次小程序、或 JWT 过期，`tower.py` 就会报
`code=303 账号已在其他设备登录`。以前只能重新抓包；现在用短信登录自己换一份。

流程（全部来自抓包 [611] → [623] → [625]，并逐字节复算验证）：

  1) 发码  POST /auth/mobile/user/getValidateCode
     {"cellphone":"手机号","vailType":1,"timestamp":<毫秒>,"sign":md5(md5(手机号)+timestamp)}
     —— 实测 sign = 2086d859… 与抓包完全一致

  2) 登录  POST /auth/mobile/user/login
     {"cellphone": RSA(手机号), "code":"4651", "loginType":1, "type":1,
      "miniProgramOpenId":"<微信 openid>", "invitationCode":"",
      "appId": RSA("applet_manage:123456"),
      "coordinateY": 经度, "coordinateX": 纬度}
     → data.tokenMap.access_token  = 我们要的 Bearer JWT（exp 约半年，jti 每次不同）
     → data.userDtoCache.token     = 32 位 hex 老系统 token（脚本用不上）
     → data.userDtoCache.deviceId  = 本次会话的 deviceId（后续请求头要一致）

  3) 换发（可选，脚本不做）POST /auth/mobile/user/exchangeOldToken {"uid","token"}

RSA 就是 JSEncrypt 那套：1024 bit / PKCS#1 v1.5 / 输出 base64（172 字符）。
公钥是写死在小程序常量里的 `PUBLIC_KEY_KEY`。

openid 拿不到（需要 wx.login 的一次性 code），所以复用抓包里那个 —— 同一用户同一小程序
恒定不变。首次运行会自动从 `captures/铁塔充电_raw/` 挖出来，之后存在 config/tower_session.json 的 open_id。

用法：
  python3 evwatch/tower_login.py send-code --mobile 13800138000
  python3 evwatch/tower_login.py login     --mobile 13800138000 --code 4651
  python3 evwatch/tower_login.py relogin   --mobile 13800138000        # 发码+等码+登录，一条命令
  python3 evwatch/tower_login.py relogin   --mobile 13800138000 --code-dir "$HOME/Library/Mobile Documents/com~apple~CloudDocs/evwatch"
  python3 evwatch/tower_login.py whoami                                # 看看当前 token 还能用多久
"""
from __future__ import annotations

import argparse
import base64
import glob
import hashlib
import json
import os
import random
import re
import string
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch import hb_crypto  # noqa: F401  先导入它：缺 pycryptodome 时会自动挂载本机 venv
from evwatch.net import urlopen as _urlopen  # noqa: E402
from evwatch.tower import APPID, BASE, UA, Tower, TowerError, token_info  # noqa: E402
from evwatch import paths  # noqa: E402
from evwatch import redact  # noqa: E402

try:
    from Crypto.PublicKey import RSA
    from Crypto.Cipher import PKCS1_v1_5
except ImportError:  # pragma: no cover
    RSA = PKCS1_v1_5 = None

# 小程序常量 PUBLIC_KEY_KEY（__APP__/appservice.app.js，1024 bit）
PUBLIC_KEY = (
    'MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC47r5DjWOpTqOG4/P4uL2VreukntYtSPOt0AU2'
    'FxtpqeTDwLX0QknY6ekDOEE/LpZp8t6bYk73zsKjVycM4abrqNfGMkYkyXGa15CUHzibuVnsY3g0'
    'wj3tHpG/U1CG7LM2/Yba34rKmLQniGp4umHxW5aCPQSu1di1/mlf/Pj23wIDAQAB'
)
APP_ID_PLAIN = 'applet_manage:123456'      # 客户端把这一串也 RSA 一遍当 appId
DEFAULT_DEVICE_ID = 'abc123'                # 6 位占位符；登录成功后服务端会下发真的那个
# 示例坐标（登录接口只记录位置、不参与鉴权；可用 --lng/--lat 覆盖）
DEFAULT_LNG, DEFAULT_LAT = 116.3975, 39.9087

# iOS 快捷指令把验证码写到这里（和哈啰共用同一个目录，两边短信都能用）
ICLOUD_DIR = os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/evwatch')
# ⚠️ 文件名不能随便：这个目录里还放着 tower_token.txt 和几份说明，
# 「取最新文件」会抓错东西。验证码固定用这个文件名，快捷指令里照填。
CODE_FILE = os.path.join(ICLOUD_DIR, 'verify_code.txt')


def _rsa(text: str) -> str:
    """JSEncrypt 等价：PKCS#1 v1.5 + base64"""
    if RSA is None:
        raise SystemExit('缺少 pycryptodome，先装：\n  pip install -i '
                         'https://pypi.tuna.tsinghua.edu.cn/simple pycryptodome')
    key = RSA.import_key(base64.b64decode(PUBLIC_KEY))
    out = PKCS1_v1_5.new(key).encrypt(text.encode())
    b64 = base64.b64encode(out).decode()
    assert len(out) == 128 and len(b64) == 172, 'RSA 密文长度不对，公钥可能被改过'
    return b64


def code_sign(cellphone: str, timestamp: int) -> str:
    """发码签名：md5( md5(手机号) + 毫秒时间戳 )，已与抓包 [611] 对齐"""
    inner = hashlib.md5(cellphone.encode()).hexdigest()
    return hashlib.md5((inner + str(timestamp)).encode()).hexdigest()


def send_code_payload(cellphone: str, timestamp: int | None = None) -> dict:
    ts = timestamp or int(time.time() * 1000)
    return {'cellphone': cellphone, 'vailType': 1, 'timestamp': ts,
            'sign': code_sign(cellphone, ts)}


def login_payload(cellphone: str, code: str, open_id: str,
                  lng: float = DEFAULT_LNG, lat: float = DEFAULT_LAT) -> dict:
    """注意：coordinateY = 经度，coordinateX = 纬度（抓包里就是这样反着的）"""
    return {
        'cellphone': _rsa(cellphone),
        'code': code,
        'loginType': 1,
        'type': 1,
        'miniProgramOpenId': open_id,
        'invitationCode': '',
        'appId': _rsa(APP_ID_PLAIN),
        'coordinateY': lng,
        'coordinateX': lat,
    }


def post(path: str, payload: dict, token: str | None = None,
         device_id: str = DEFAULT_DEVICE_ID, timeout: float = 15) -> dict:
    """登录链路专用 POST：和 tower.Tower 不同，这里允许没有 token"""
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, separators=(',', ':')).encode(),
        method='POST',
        headers={
            'content-type': 'application/json;charset=UTF-8',
            'deviceId': device_id,
            'platform': 'wechat',
            'User-Agent': UA,
            'Referer': f'https://servicewechat.com/{APPID}/84/page-frame.html',
            **({'Authorization': f'Bearer {token}'} if token else {}),
        })
    with _urlopen(req, timeout=timeout, stale_retry=False) as r:
        raw = r.read()
        if (r.headers.get('Content-Encoding') or '').lower() == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    txt = raw.decode('utf-8', 'replace').strip()
    obj = json.loads(txt) if txt.startswith('{') else {}
    if obj.get('code') != 200:
        raise TowerError(obj.get('code'), obj.get('msg'))
    return obj.get('data') or {}


def send_code(cellphone: str, device_id: str = DEFAULT_DEVICE_ID) -> dict:
    return post('/auth/mobile/user/getValidateCode',
                send_code_payload(cellphone), device_id=device_id)


def login_sms(cellphone: str, code: str, open_id: str, device_id: str = DEFAULT_DEVICE_ID,
              lng: float = DEFAULT_LNG, lat: float = DEFAULT_LAT) -> dict:
    return post('/auth/mobile/user/login',
                login_payload(cellphone, code, open_id, lng, lat), device_id=device_id)


# ---------------------------------------------------------------- session

def load_session(path: str) -> dict:
    try:
        return json.load(open(path, encoding='utf-8'))
    except Exception:
        return {}


def save_session(path: str, **fields) -> dict:
    s = load_session(path)
    s.update({k: v for k, v in fields.items() if v is not None})
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(s, fh, ensure_ascii=False, indent=2)
    return s


def detect_open_id(root: str = paths.TOWER_CAPTURE_DIR) -> str | None:
    """从抓包里挖微信 openid（登录报文里的 miniProgramOpenId）。"""
    for f in glob.glob(os.path.join(root, '*login*')) + glob.glob(os.path.join(root, '*')):
        if not os.path.isfile(f):
            continue
        try:
            data = open(f, 'rb').read()
        except OSError:
            continue
        m = re.search(rb'"miniProgramOpenId"\s*:\s*"([A-Za-z0-9_\-]{10,})"', data)
        if m:
            return m.group(1).decode()
    return None


def new_device_id() -> str:
    return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def detect_mobile(root: str = paths.TOWER_CAPTURE_DIR) -> str | None:
    """从抓包响应里捞手机号（getUserInfo / login 响应都有 cellphone）"""
    for f in glob.glob(os.path.join(root, '*')):
        if not os.path.isfile(f):
            continue
        try:
            m = re.search(rb'"cellphone"\s*:\s*"(1\d{10})"', open(f, 'rb').read())
        except OSError:
            continue
        if m:
            return m.group(1).decode()
    return None


# ---------------------------------------------------------------- token 收件箱（iCloud）

TOKEN_INBOX = os.path.join(ICLOUD_DIR, 'tower_token.txt')
JWT_RE = re.compile(r'eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}')
DEV_RE = re.compile(r'deviceId["\']?\s*[:=]\s*["\']?([A-Za-z0-9]{4,20})', re.I)


def parse_token(text: str) -> str | None:
    """从任意粘贴内容里抠出 JWT。

    能吃的格式：裸 token / `Bearer xxx` / `Authorization: Bearer xxx` /
    curl 的 `-H 'Authorization: Bearer xxx'` / 整份 HTTP 请求抓包。
    """
    if not text:
        return None
    m = JWT_RE.search(text)
    if m:
        return m.group(0)
    # 有些抓包工具会把超长 header 折行，硬拼一下再试
    joined = re.sub(r'\s+', '', text)
    m = JWT_RE.search(joined)
    return m.group(0) if m else None


def parse_token_file(text: str) -> tuple[str | None, str | None]:
    """抠 (token, deviceId)。

    ⚠️ 实测：**token 必须和登录时那个 deviceId 配对**，只给 token 用别的 deviceId 会 303。
    所以收件箱里最好两样都有 —— 手机端「Copy as cURL」整段存下来最省事。
    """
    tok = parse_token(text)
    dev = DEV_RE.search(text or '')
    return tok, (dev.group(1) if dev else None)


def read_token_inbox(path: str = TOKEN_INBOX) -> tuple[str | None, str | None, float]:
    """读 iCloud 上的 token 文件，返回 (token, deviceId, mtime)；没有就是 (None, None, 0)"""
    try:
        mt = os.path.getmtime(path)
        with open(path, encoding='utf-8', errors='ignore') as fh:
            tok, dev = parse_token_file(fh.read())
            return tok, dev, mt
    except OSError:
        return None, None, 0.0


def find_capture_token(root: str = paths.TOWER_CAPTURE_DIR) -> tuple[str, str, str] | None:
    """从抓包里捞一份 Authorization（优先用抓包 token，短信只是兜底）。

    返回 (token, device_id, 文件路径)；找最新的、带 Authorization 的 charge-api 请求。
    """
    if not os.path.isdir(root):
        return None
    cands = []
    for name in os.listdir(root):
        if 'request_' not in name or 'charge-api' not in name:
            continue
        p = os.path.join(root, name)
        try:
            cands.append((os.path.getmtime(p), p))
        except OSError:
            continue
    for _, p in sorted(cands, reverse=True):
        data = open(p, 'rb').read()
        m = re.search(rb'Authorization:\s*Bearer\s+(\S+)', data)
        if not m:
            continue
        d = re.search(rb'deviceId:\s*(\S+)', data)
        return m.group(1).decode(), (d.group(1).decode() if d else DEFAULT_DEVICE_ID), p
    return None


# ---------------------------------------------------------------- 命令

def cmd_token_inbox(args, session: dict) -> int:
    """看看 iCloud 收件箱里有没有新 token（iPhone 上更新 token 的入口）"""
    tok, dev, mt = read_token_inbox(args.token_inbox)
    print(f'收件箱：{args.token_inbox}')
    if not tok:
        print('  状态：空（或文件里没有 JWT）')
        print('  iPhone 上把「Authorization + deviceId」存成这个文件名即可：')
        print(f'    iCloud 云盘 / evwatch / {os.path.basename(args.token_inbox)}')
        print('  提示：Proxyman / Stream 里「Copy as cURL」整段存下来最省事（两个头都在里面）')
        return 1
    age_min = (time.time() - mt) / 60 if mt else None
    print(f"  状态：有 token（{redact.token(tok)}，JWT {token_info(tok)}）")
    if age_min is not None:
        print(f'  文件更新于 {age_min:.0f} 分钟前')
    print(f"  deviceId：{dev or '（文件里没有，将沿用 session 里的）'}")
    cur = session.get('authorization')
    if cur and cur == tok:
        print('  与当前 session 相同，无需更新')
        return 0
    # 先验后用
    use_dev = dev or session.get('device_id') or DEFAULT_DEVICE_ID
    try:
        info = Tower(dict(session, authorization=tok, device_id=use_dev)).user_info() or {}
    except TowerError as e:
        print(f'  验证失败：{e}（不会写入 session）')
        if e.code == 303:
            print('  → 可能是 token 与 deviceId 不配对：deviceId 也要一起更新（抓整段 cURL 最稳）')
        return 1
    print(f"  验证通过（用户 {info.get('username') or info.get('id')}）")
    if getattr(args, 'apply', False):
        save_session(args.session, authorization=tok, device_id=use_dev,
                     obtained_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                     token_type='JWT', source='icloud')
        print(f'  已写入 {args.session}')
    else:
        print('  加 --apply 才会写入 session')
    return 0


def cmd_use_capture(args, session: dict) -> int:
    """导入一份 token —— 首选方式（一次管半年）。

    来源优先级：--token > --file > iCloud 收件箱 > 本地抓包
    """
    token = args.token
    device_id = args.device_id
    src = '--token'
    dev_from_src = False
    if not token and args.file:
        try:
            text = open(args.file, encoding='utf-8', errors='ignore').read()
        except OSError as e:
            print(f'读不了 {args.file}：{e}')
            return 2
        token, dev = parse_token_file(text)
        if not token:
            print(f'{args.file} 里没找到 JWT')
            return 2
        device_id = device_id or dev
        dev_from_src = bool(dev)
        src = args.file
        print(f'取自文件：{src}' + (f'，deviceId {dev}' if dev else '（文件里没 deviceId）'))
    if not token:
        tok, dev, mt = read_token_inbox(args.token_inbox)
        if tok:
            token, src = tok, f'iCloud（{(time.time() - mt) / 60:.0f} 分钟前更新）'
            device_id = device_id or dev
            dev_from_src = bool(dev)
            print(f'取自 {src}' + (f'，deviceId {dev}' if dev else '（文件里没 deviceId，沿用 session 的）'))
    if not token:
        hit = find_capture_token(args.capture_dir)
        if not hit:
            print(f'没找到 token。三条路任选：')
            print('  --token <JWT>                    直接粘')
            print(f'  --file <文件>                    指定文件')
            print(f'  iPhone 存到 {args.token_inbox}')
            return 2
        token, dev_from_cap, path = hit
        device_id = device_id or dev_from_cap
        dev_from_src = True
        src = os.path.basename(path)
        print(f'取自抓包：{src}')
    device_id = device_id or session.get('device_id') or DEFAULT_DEVICE_ID

    # 先验后用：抓包 token 可能已被挤掉，验不过就不覆盖现有登录态
    cand = dict(session, authorization=token, device_id=device_id)
    try:
        info = Tower(cand).user_info() or {}
    except TowerError as e:
        print(f'  验证失败：{e}（没有写入 {args.session}，现有登录态保持不变）')
        if e.code == 303:
            if dev_from_src:
                print('  → 这份 token 已被别处登录挤掉（deviceId 是对上的）；'
                      '换一份新抓包，或走短信兜底：relogin --mobile 手机号')
            else:
                print('  → 大概率是缺 deviceId：token 必须和登录时那个 deviceId 配对。'
                      'iPhone 上用 Proxyman「Copy as cURL」整段存进收件箱，两个头就都有了。')
        return 1

    # 顺手把短信兜底要用的两项补上（use-capture 建出来的 session 也能自愈）
    extra = {}
    if not session.get('open_id'):
        oid = detect_open_id()
        if oid:
            extra['open_id'] = oid
    if not (args.mobile or session.get('mobile')):
        mob = detect_mobile()
        if mob:
            extra['mobile'] = mob

    save_session(args.session, authorization=token, device_id=device_id,
                 obtained_at=time.strftime('%Y-%m-%d %H:%M:%S'), token_type='JWT',
                 source=src, **extra)
    print(f'  已写入 {args.session}（deviceId {redact.device_id(device_id)}）')
    if extra:
        print(f"  顺带补全：{'、'.join(extra)}（303 时可直接短信兜底）")
    print(f'  登录态：JWT {token_info(token)}')
    print(f"  验证：getUserInfo OK（用户 {info.get('username') or info.get('id')}）")
    return 0


def cmd_send_code(args, session: dict) -> int:
    device_id = args.device_id or session.get('device_id') or DEFAULT_DEVICE_ID
    print(f'向 {redact.phone(args.mobile)} 发送验证码…')
    data = send_code(args.mobile, device_id)
    msg = data.get('message') or data
    print(f'  {msg}')
    print(f'  deviceId: {redact.device_id(device_id)}（登录时要用同一个）')
    return 0


def cmd_login(args, session: dict) -> int:
    open_id = args.open_id or session.get('open_id') or detect_open_id()
    if not open_id:
        print('缺 open_id：抓一次登录报文（含 miniProgramOpenId），或用 --open-id 指定。')
        return 2
    device_id = args.device_id or session.get('device_id') or DEFAULT_DEVICE_ID
    code = args.code or args.code_value
    if not code:
        print('需要验证码：--code，或 --code-file / --code-dir / --code-cmd（配合 send-code 用）')
        return 2

    print(f'登录中…（openid {redact.openid(open_id)}，deviceId {redact.device_id(device_id)}）')
    data = login_sms(args.mobile, code, open_id, device_id, args.lng, args.lat)
    token_map = data.get('tokenMap') or {}
    user = data.get('userDtoCache') or {}
    token = token_map.get('access_token') or user.get('token')
    if not token:
        print(f'登录返回里没有 token：{json.dumps(data, ensure_ascii=False)[:300]}')
        return 1

    save_session(args.session,
                 authorization=token,
                 device_id=user.get('deviceId') or device_id,
                 open_id=open_id,
                 mobile=user.get('cellphone') or args.mobile,
                 user_id=user.get('id'),
                 old_token=user.get('token'),
                 obtained_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                 token_type='JWT')
    print(f'  已写入 {args.session}')
    print(f'  登录态：JWT {token_info(token)}')

    # 立刻验一下，确认 token 真能用
    try:
        info = Tower(load_session(args.session)).user_info() or {}
        print(f"  验证：getUserInfo OK（用户 {info.get('username') or info.get('id')}）")
    except TowerError as e:
        print(f'  验证失败：{e}（token 已保存，但接口不认，可能需要重试）')
        return 1
    return 0


def cmd_relogin(args, session: dict) -> int:
    """发码 → 等码 → 登录，一条命令搞定（可接 iCloud 短信转发全自动）"""
    device_id = args.device_id or session.get('device_id') or DEFAULT_DEVICE_ID
    open_id = args.open_id or session.get('open_id') or detect_open_id()
    if not open_id:
        print('缺 open_id：先用 --open-id 指定一次（抓包里 miniProgramOpenId 那串）。')
        return 2

    t0 = time.time()
    print(f'向 {redact.phone(args.mobile)} 发送验证码…')
    data = send_code(args.mobile, device_id)
    print(f"  {data.get('message') or data}")

    from evwatch.login import wait_code          # 复用哈啰那套「手输 / 文件 / 目录 / 命令」取码
    cargs = _code_args(args)                    # 没指定来源时默认固定文件 verify_code.txt
    if cargs.code_file:
        print(f'  取码来源：文件 {cargs.code_file}')
    elif cargs.code_dir:
        print(f'  取码来源：目录 {cargs.code_dir}（最新的非说明文件）')
    code = wait_code(cargs, t0)
    if not code:
        print('没拿到验证码，放弃。')
        return 1
    print(f'  验证码：{code}')

    args.code = code
    return cmd_login(args, session)


def _code_args(args):
    """把 watch 的 argv 适配成 login.wait_code 认识的样子。

    取码策略（默认）：
      1. 优先固定文件 `verify_code.txt` —— 快捷指令照这个名字写，最稳；
      2. 没有这个文件时才退回「扫目录里最新的文件」（兼容旧快捷指令），
         且会跳过 tower_token.txt / 说明*.md，避免把 token 或教程当验证码。
    """
    class NS:
        pass
    ns = NS()
    ns.code = getattr(args, 'code', None)
    ns.code_file = getattr(args, 'code_file', None)
    ns.code_dir = getattr(args, 'code_dir', None)
    ns.code_cmd = getattr(args, 'code_cmd', None)
    ns.timeout = getattr(args, 'code_timeout', 180) or 180
    # 铁塔自己扫目录时只跳过 token 文件（自己的 verify_code.txt 要能读到）
    ns.code_skip = {'tower_token.txt'}
    if not (ns.code or ns.code_file or ns.code_dir or ns.code_cmd):
        if os.path.exists(CODE_FILE):
            ns.code_file = CODE_FILE
        elif os.path.isdir(ICLOUD_DIR):
            ns.code_dir = ICLOUD_DIR
    return ns


def auto_relogin(args, session_path: str = paths.config('tower_session.json')) -> str | None:
    """给 tower.py watch 用：遇到 303 时自动「发码 → 等码 → 登录」，返回新 token。

    拿不到验证码（没配取码来源 / 超时）就返回 None，让调用方继续按失效处理。
    """
    session = load_session(session_path)
    mobile = getattr(args, 'relogin_mobile', None) or session.get('mobile')
    if not mobile:
        print('   → 自动续期：没有手机号（--relogin-mobile 或 session 里的 mobile）')
        return None
    open_id = session.get('open_id') or detect_open_id()
    if not open_id:
        print('   → 自动续期：缺 open_id，先手动跑一次 tower_login.py relogin --open-id ...')
        return None
    device_id = session.get('device_id') or DEFAULT_DEVICE_ID

    print(f'   → 自动续期：向 {redact.phone(mobile)} 发验证码…')
    t0 = time.time()
    data = send_code(mobile, device_id)
    print(f"     {data.get('message') or data}")
    from evwatch.login import wait_code
    code = wait_code(_code_args(args), t0)
    if not code:
        print('   → 自动续期：没拿到验证码')
        return None
    data = login_sms(mobile, code, open_id, device_id)
    user = data.get('userDtoCache') or {}
    token = (data.get('tokenMap') or {}).get('access_token') or user.get('token')
    if not token:
        print('   → 自动续期：登录返回里没有 token')
        return None
    save_session(session_path, authorization=token, open_id=open_id,
                 device_id=user.get('deviceId') or device_id,
                 mobile=user.get('cellphone') or mobile,
                 user_id=user.get('id'),
                 obtained_at=time.strftime('%Y-%m-%d %H:%M:%S'),
                 token_type='JWT')
    print(f'   → 自动续期成功：JWT {token_info(token)}')
    return token


def cmd_icloud_setup(args) -> int:
    """建目录 + 写一份「铁塔短信」取码说明（快捷指令还是要你在手机上点几下建）"""
    os.makedirs(ICLOUD_DIR, exist_ok=True)
    guide = os.path.join(ICLOUD_DIR, '说明-铁塔.md')
    text = f"""# 铁塔充电验证码转发（iOS 快捷指令）

目的：让 `tower_login.py` 能自己拿到 4 位验证码，实现「短信兜底」全自动续期。

## 一、手机端（iOS「快捷指令」App）

1. 新建「个人自动化」→ 触发条件选 **信息** → 「信息包含」填 `验证码`（铁塔的短信正文含这个词）
2. 动作：**获取快捷指令输入** → **匹配文本**，正则 `\\d{{4}}`
3. 再一个动作：**存储文件** → 关闭「询问保存位置」→ 路径选 `iCloud 云盘 / evwatch /`
   **文件名必须填 `verify_code.txt`**，并打开「覆盖」
4. 关掉「运行前询问」，保存

> ⚠️ 文件名**不能随便起**：这个目录里还放着 `tower_token.txt` 和三份 `说明*.md`。
> 脚本虽然会跳过这些已知文件，但你自己另起名字的文件它没法识别，
> 最稳的做法就是固定叫 `verify_code.txt`（脚本优先读这个文件）。

## 二、Mac 端验证

```bash
ls -l "{ICLOUD_DIR}"
python3 evwatch/tower_login.py relogin --mobile <手机号>   # 会自动等这个文件
```

## 三、注意

- iCloud 同步有 3~30 秒延迟，脚本默认最多等 180 秒。
- 看到 `verify_code.txt.icloud` 这种**占位文件**说明还没下载到本地：打开一次
  「访达 → iCloud 云盘 → evwatch」即可。
- 脚本只认「发码之后被改写过」的这个文件（比对 mtime），不会误用上一次的旧验证码；
  没有 `verify_code.txt` 时才会退回「扫目录里最新的文件」（跳过 `tower_token.txt` / `说明*.md`）。
"""
    with open(guide, 'w', encoding='utf-8') as fh:
        fh.write(text)
    token_guide = os.path.join(ICLOUD_DIR, '说明-token.md')
    token_text = f"""# 在 iPhone 上更新铁塔 token

Mac 端点开一次也是可以的，但用 iPhone 更方便：抓到 token 后丢进 iCloud，Mac 自动接。

## 一、手机端一次性配置（快捷指令）

1. 新建快捷指令「存铁塔token」，动作：
   - **获取剪贴板**
   - **存储文件** → 关闭「询问保存位置」→ 路径 `iCloud 云盘 / evwatch /`
     文件名填 `tower_token.txt`，打开「覆盖」
2. 建议加进共享表单或桌面小组件，方便一键调用。

## 二、日常更新（3 步）

1. iPhone 上用 Proxyman / Stream 抓一次 `charge-api.chinatowercom.cn` 的请求
2. **长按该请求 →「Copy as cURL」**（⚠️ 别只复制 Authorization，`deviceId` 也要一起带，
   实测 token 和 deviceId 是配对的，只给 token 会 303）
3. 运行快捷指令 → 文件落到 `{TOKEN_INBOX}`

> 手工也行：文件里写两行就够，脚本能解析
> ```
> deviceId: abc123
> Authorization: Bearer eyJ...
> ```

## 三、Mac 端自动接

- 跑着 `watch` 时：每 2 分钟看一次收件箱，发现更新的 token 会**自动验证并热替换**（连 deviceId 一起换），
  不用重启；验证不过就继续用旧的，同一个坏 token 只提示一次
- 手动一次：`python3 evwatch/tower_login.py use-capture`（优先读这个收件箱）
- 只看不写：`python3 evwatch/tower_login.py token-inbox`（加 `--apply` 才写入 session）

> 能吃的格式很杂：裸 JWT、`Bearer xxx`、`Authorization: Bearer xxx`、
> curl 的 `-H 'Authorization: Bearer xxx'`、整份 HTTP 抓包文本都行。
> 脚本会先调一次 `getUserInfo` 验证，**验证通过才写入**，失效的 token 不会覆盖现有登录态。
"""
    with open(token_guide, 'w', encoding='utf-8') as fh:
        fh.write(token_text)

    print(f'目录：{ICLOUD_DIR}')
    print(f'说明：{guide}（手机「文件」App 里也能看到）')
    print(f'说明：{token_guide}')
    print(f'token 收件箱：{TOKEN_INBOX}')
    print('\n下一步：')
    print('  1) 手机上按「说明-token.md」建快捷指令，之后抓包 → 一键存 token')
    print(f'  2) Mac 上：python3 evwatch/tower_login.py use-capture')
    print(f'  3) 或直接跑 watch，会自动热更新')
    return 0


def cmd_whoami(args, session: dict) -> int:
    token = session.get('authorization') or ''
    if not token:
        print(f'{args.session} 里没有 authorization，先跑 relogin。')
        return 2
    print(f'登录态：JWT {token_info(token)}')
    print(f"deviceId：{redact.device_id(session.get('device_id'))}   openid：{redact.openid(session.get('open_id'))}")
    print(f"取得时间：{session.get('obtained_at')}")
    try:
        info = Tower(session).user_info() or {}
        print(f"接口验证：OK（用户名 {info.get('username')}，uid {info.get('id')}）")
        return 0
    except TowerError as e:
        print(f'接口验证：失败 {e}')
        if e.code == 303:
            print('  → 被别的设备挤下线了：重跑 relogin（登录后别再用手机打开小程序）')
        return 1


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(
        description='铁塔充电登录态：优先用抓包 token，短信登录兜底')
    ap.add_argument('cmd', choices=['use-capture', 'token-inbox', 'send-code', 'login',
                                    'relogin', 'whoami', 'icloud-setup'])
    ap.add_argument('--token', help='use-capture：直接粘 Authorization（Bearer 后面那串）')
    ap.add_argument('--file', help='use-capture：从文件里读（抓包文本 / 只存了 token 的 txt 都行）')
    ap.add_argument('--capture-dir', default=paths.TOWER_CAPTURE_DIR, help='use-capture：抓包目录')
    ap.add_argument('--token-inbox', default=TOKEN_INBOX,
                    help='iCloud 上的 token 收件箱（默认 tower_token.txt）')
    ap.add_argument('--apply', action='store_true', help='token-inbox：验证通过后写入 session')
    ap.add_argument('--mobile', help='手机号（send-code / login / relogin 必填）')
    ap.add_argument('--code', help='验证码（4 位）')
    ap.add_argument('--code-file', help='验证码落地文件')
    ap.add_argument('--code-dir', help=f'验证码目录（默认 {ICLOUD_DIR}）')
    ap.add_argument('--code-cmd', help='执行命令取验证码')
    ap.add_argument('--timeout', type=int, default=180, help='等验证码的超时（秒）')
    ap.add_argument('--open-id', help='微信 openid（默认从 session 或抓包里取）')
    ap.add_argument('--device-id', help='deviceId（默认沿用 session 里的）')
    ap.add_argument('--lng', type=float, default=DEFAULT_LNG, help='经度（登录接口的 coordinateY）')
    ap.add_argument('--lat', type=float, default=DEFAULT_LAT, help='纬度（登录接口的 coordinateX）')
    ap.add_argument('--session', default=paths.config('tower_session.json'))
    args = ap.parse_args()
    args.code_value = None

    session = load_session(args.session)
    if args.cmd in ('send-code', 'login', 'relogin') and not args.mobile:
        args.mobile = session.get('mobile')
    if args.cmd in ('send-code', 'login', 'relogin') and not args.mobile:
        print('需要 --mobile（或先在 tower_session.json 里写好 mobile）')
        return 2

    try:
        if args.cmd == 'use-capture':
            return cmd_use_capture(args, session)
        if args.cmd == 'token-inbox':
            return cmd_token_inbox(args, session)
        if args.cmd == 'send-code':
            return cmd_send_code(args, session)
        if args.cmd == 'login':
            return cmd_login(args, session)
        if args.cmd == 'relogin':
            return cmd_relogin(args, session)
        if args.cmd == 'icloud-setup':
            return cmd_icloud_setup(args)
        return cmd_whoami(args, session)
    except TowerError as e:
        print(f'接口报错：{e}')
        return 1
    except KeyboardInterrupt:
        print('\n已取消')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
