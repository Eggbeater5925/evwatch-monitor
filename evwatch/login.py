"""
login.py — 小哈智电登录态自动续期（短信验证码登录）

为什么短信这条路走得通：
  api.hellobike.com 的登录接口是【明文 JSON、无 signature/timestamp/nonce】
  690 user.account.sendCodeV2   {mobile, ssid}            → 发码
  697 user.account.weixinMobileLogin {mobile, code, ssid} → 换 token/ticket

而一键登录（566 weixinEasyLogin）需要 wx.login() 产生的 wechatLoginCode，
那是微信客户端 + 该小程序 appid 才能生成的一次性票据，**脚本无法伪造**。

用法：
  # 1) 半自动：发码 → 你手输 → 写回 session.json（最简单）
  python evwatch/login.py relogin --mobile 13800138000

  # 2) 全自动：从短信转发的落地文件里轮询验证码
  python evwatch/login.py relogin --mobile 138... --code-file /path/to/sms.txt --timeout 180

  # 3) 全自动：外部命令吐验证码（邮件/接口/Bark 都行）
  python evwatch/login.py relogin --mobile 138... --code-cmd "curl -s http://xxx/lastcode"

  # 4) 只发码 / 只登录 / 只看请求长什么样
  python evwatch/login.py send-code --mobile 138...
  python evwatch/login.py login --mobile 138... --code 7434
  python evwatch/login.py send-code --mobile 138... --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch.hb_crypto import UA, APPID  # noqa: E402
from evwatch.net import urlopen as _urlopen  # noqa: E402
from evwatch import paths  # noqa: E402
from evwatch import redact  # noqa: E402

API = 'https://api.hellobike.com/api'
SYSTEM_CODE = 'LB64'
ICLOUD_DIR = os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/evwatch')

SHORTCUT_GUIDE = """\
# 用 iPhone 快捷指令把验证码转发到 iCloud

目标：短信到达手机 → 自动抽出验证码 → 写进 iCloud 的 `evwatch/` 目录 → Mac 上的脚本读它。

## 一、手机端建快捷指令

1. 「快捷指令」App → 底部「自动化」→ 右上「+」→ 创建个人自动化
2. 触发条件选「**信息**」：
   - 信息包含：`验证码`（或发件人填 `1069` 开头也行）
3. 动作里加三步：
   ```
   ① 获取信息快捷输入           （自动化自带「快捷输入」变量就是那条短信）
   ② 匹配文本：快捷输入
        正则：\\d{4,6}
   ③ 存储文件
        存储到：iCloud Drive → evwatch
        文件名：code.txt
        如果已存在：覆盖（没有这个选项就保持默认，脚本会读目录里最新的文件）
   ```
4. 关掉「运行前询问」，保存

## 二、Mac 端验证

```bash
ls -l "$HOME/Library/Mobile Documents/com~apple~CloudDocs/evwatch"
```

## 三、注意

- iCloud 同步有 3~30 秒延迟，脚本默认最多等 180 秒，够用。
- 如果 Mac 上看到 `code.txt.icloud` 这种**占位文件**，说明还没下载到本地：
  打开一次「访达 → iCloud 云盘 → evwatch」即可，或让脚本多等一轮。
- 目录里现在不止这一件事：还有 `tower_token.txt`（铁塔 token）、`verify_code.txt`（铁塔验证码）
  和几份 `说明*.md`。脚本扫目录时会**跳过**这些已知文件，不会把 JWT 里的数字当验证码。
- 脚本取的是「发码之后被改动的**最新**文件」，不依赖固定文件名（哈啰这条链路留着的兼容行为），
  也不会误用上一次的旧验证码（会比对修改时间）。
"""



def make_ssid(prefix_len: int = 15) -> str:
    """抓包里的 ssid 形如 AbCdEf123456789_2025-01-01（随机串 + 下划线 + 日期），每次登录用新的"""
    rnd = ''.join(random.choices(string.ascii_letters + string.digits, k=prefix_len))
    return f"{rnd}_{time.strftime('%Y-%m-%d')}"


def _post(action: str, payload: dict, env: str = 'pro') -> dict:
    body = json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode()
    req = urllib.request.Request(
        f'{API}?{action}', data=body, method='POST',
        headers={
            'content-type': 'application/json',
            'User-Agent': UA,
            'Referer': f'https://servicewechat.com/{APPID}/12/page-frame.html',
        })
    # 发码/登录都是「不幂等」的 POST（重放会再发一条短信），所以禁用出口失效重试
    with _urlopen(req, timeout=15, stale_retry=False) as r:
        raw = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    obj = json.loads(raw.decode())
    if obj.get('code') != 0:
        raise RuntimeError(f"code={obj.get('code')} msg={obj.get('msg')}")
    return obj.get('data') or {}


def send_code_payload(mobile: str, ssid: str) -> dict:
    return {'action': 'user.account.sendCodeV2', 'systemCode': SYSTEM_CODE, 'env': 'pro',
            'mobile': mobile, 'ssid': ssid}


def login_payload(mobile: str, code: str, ssid: str) -> dict:
    return {'action': 'user.account.weixinMobileLogin', 'platform': 1, 'env': 'pro',
            'systemCode': SYSTEM_CODE, 'channel': 57, 'mobile': mobile,
            'code': code, 'ssid': ssid}


def send_code(mobile: str, ssid: str) -> dict:
    return _post('user.account.sendCodeV2', send_code_payload(mobile, ssid))


def login_sms(mobile: str, code: str, ssid: str) -> dict:
    return _post('user.account.weixinMobileLogin', login_payload(mobile, code, ssid))


# ---------------------------------------------------------------- 取验证码
def _extract_code(text: str) -> str | None:
    """从短信正文里抠验证码。
    短信转发的内容常带日期/时间，所以不能简单取第一个数字：
      1) 先找「验证码/校验码/code」后面的 4~6 位
      2) 否则取最后一个独立的 4~6 位数字（排除日期里的年份、时间片段）
    """
    text = text or ''
    m = re.search(r'(?:验证码|校验码|动态码|确认码|code)[^0-9]{0,12}(\d{4,6})(?![\d\-:])', text, re.I)
    if m:
        return m.group(1)
    cands = re.findall(r'(?<![\d\-])(\d{4,6})(?![\d\-])', text)
    cands = [c for c in cands if not (len(c) == 4 and c.startswith(('19', '20')))]  # 排掉年份
    return cands[-1] if cands else None


DEFAULT_SKIP = ('tower_token.txt', 'verify_code.txt')


def _newest_file(d: str, since: float, skip: set | tuple | None = None) -> str | None:
    """目录里发码之后被写过的最新文件（避开 iCloud 未下载的 .icloud 占位符）

    这个目录现在不只放验证码：还有 tower_token.txt（铁塔 token）和几份 说明*.md。
    扫目录时必须跳过它们，否则会把 JWT 里某段数字当成验证码。
    铁塔侧会传自己的 skip（只跳 token 文件），这样它自己的 verify_code.txt 还能被读到。
    """
    skip = DEFAULT_SKIP if skip is None else skip
    best: tuple[float, str] | None = None
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for name in names:
        if name.startswith('.') or name.endswith('.icloud'):
            continue
        if name in skip or name.startswith('说明') or name.endswith('.md'):
            continue
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        try:
            m = os.path.getmtime(p)
        except OSError:
            continue
        if m < since - 2:
            continue
        if best is None or m > best[0]:
            best = (m, p)
    return best[1] if best else None


def wait_code(args, sent_at: float) -> str | None:
    if args.code:
        return args.code
    if args.code_dir:
        deadline = time.time() + args.timeout
        print(f'等待验证码出现在 {args.code_dir} …（最多 {args.timeout}s）')
        while time.time() < deadline:
            p = _newest_file(args.code_dir, sent_at, getattr(args, 'code_skip', None))
            if p:
                code = _extract_code(open(p, encoding='utf-8', errors='ignore').read())
                if code:
                    print(f'   来自 {os.path.basename(p)}')
                    return code
            time.sleep(2)
        return None
    if args.code_file:
        deadline = time.time() + args.timeout
        print(f'等待验证码写入 {args.code_file} …（最多 {args.timeout}s）')
        while time.time() < deadline:
            try:
                # 只认发码之后更新的文件，避免捞到上一次的旧验证码
                if os.path.getmtime(args.code_file) >= sent_at - 2:
                    code = _extract_code(open(args.code_file, encoding='utf-8', errors='ignore').read())
                    if code:
                        return code
            except FileNotFoundError:
                pass
            time.sleep(2)
        return None
    if args.code_cmd:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            try:
                out = subprocess.run(args.code_cmd, shell=True, capture_output=True, text=True, timeout=10).stdout
                code = _extract_code(out)
                if code:
                    return code
            except Exception:
                pass
            time.sleep(3)
        return None
    return _extract_code(input('输入收到的验证码: ').strip())


# ---------------------------------------------------------------- session
def save_session(session_path: str, mobile: str, data: dict) -> dict:
    old = {}
    if os.path.exists(session_path):
        try:
            old = json.load(open(session_path, encoding='utf-8'))
        except Exception:
            old = {}
    old.update({
        'token': data.get('token', ''),
        'ticket': data.get('ticket', ''),
        'mobile': data.get('mobile', mobile),
        'guid': data.get('guid', ''),
        'userNewId': data.get('userNewId', ''),
        'obtained_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    })
    old.pop('battery_no', None)          # 账号可能变了，重新取
    old.setdefault('city_code', '010')
    old.setdefault('env', 'pro')
    old.setdefault('system_code', SYSTEM_CODE)
    old.setdefault('version', '2.1.1')
    old.setdefault('app_version', '8.0.79')
    old.setdefault('chaos_env', 'pro-1.1.3-beta.1')
    with open(session_path, 'w', encoding='utf-8') as fh:
        json.dump(old, fh, ensure_ascii=False, indent=2)
    return old


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['send-code', 'login', 'relogin', 'icloud-setup'])
    ap.add_argument('--mobile', help='手机号（icloud-setup 时可省略）')
    ap.add_argument('--code', help='验证码（4~6 位）')
    ap.add_argument('--code-file', help='轮询这个文件里的验证码（短信转发落地）')
    ap.add_argument('--code-dir', help='轮询这个目录里最新的文件（iCloud 转发推荐）')
    ap.add_argument('--code-cmd', help='执行命令从标准输出取验证码')
    ap.add_argument('--timeout', type=int, default=180)
    ap.add_argument('--ssid', help='默认自动生成，一般不用管')
    ap.add_argument('--session', default=paths.config('session.json'))
    ap.add_argument('--dry-run', action='store_true', help='只打印请求体，不发')
    args = ap.parse_args()

    ssid = args.ssid or make_ssid()

    if args.cmd == 'icloud-setup':
        os.makedirs(ICLOUD_DIR, exist_ok=True)
        guide = os.path.join(ICLOUD_DIR, '说明.md')
        with open(guide, 'w', encoding='utf-8') as fh:
            fh.write(SHORTCUT_GUIDE)
        print(f'已建目录：{ICLOUD_DIR}')
        print(f'已写入说明：{guide}（手机上「文件」App 里也能看到）\n')
        print(SHORTCUT_GUIDE)
        if args.mobile:
            print('\n配好快捷指令后，直接跑：')
            print(f'  python3 evwatch/login.py relogin --mobile {redact.phone(args.mobile)} --code-dir "{ICLOUD_DIR}"')
        return 0

    if args.cmd in ('send-code', 'login', 'relogin') and not args.mobile:
        print('缺少 --mobile')
        return 2

    if args.cmd == 'send-code':
        p = send_code_payload(args.mobile, ssid)
        if args.dry_run:
            print(json.dumps(redact.payload(p), ensure_ascii=False, separators=(',', ':')))
            print('（手机号 / 签名已打码；实际请求用的是你传入的真实号码）')
            return 0
        print('发送结果:', send_code(args.mobile, ssid))
        print(f'ssid={redact.ssid(ssid)}（登录时要用同一个）')
        return 0

    if args.cmd == 'login':
        if not args.code:
            print('缺少 --code')
            return 2
        p = login_payload(args.mobile, args.code, ssid)
        if args.dry_run:
            print(json.dumps(redact.payload(p), ensure_ascii=False, separators=(',', ':')))
            print('（手机号 / 签名已打码；实际请求用的是你传入的真实号码）')
            return 0
        data = login_sms(args.mobile, args.code, ssid)
        sess = save_session(args.session, args.mobile, data)
        print(f"登录成功：userNewId={sess.get('userNewId')} 写入 {args.session}")
        return 0

    # relogin：发码 → 取码 → 登录 → 写回
    if args.dry_run:
        print(json.dumps(redact.payload(send_code_payload(args.mobile, ssid)),
                         ensure_ascii=False, separators=(',', ':')))
        print(json.dumps(redact.payload(login_payload(args.mobile, '0000', ssid)),
                         ensure_ascii=False, separators=(',', ':')))
        print('（手机号 / 签名已打码；实际请求用的是你传入的真实号码）')
        return 0
    print('发送验证码…')
    t0 = time.time()
    send_code(args.mobile, ssid)
    print('已发。')
    code = wait_code(args, t0)
    if not code:
        print('没拿到验证码，退出')
        return 1
    data = login_sms(args.mobile, code, ssid)
    sess = save_session(args.session, args.mobile, data)
    print(f"续期成功：userNewId={sess.get('userNewId')}  "
          f"token={redact.token(sess.get('token'))} 已写入 {args.session}")
    print('下一步：python evwatch/battery_watch.py ping')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
