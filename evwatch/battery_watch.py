"""
battery_watch.py — 小哈智电 电池电量实时轮询

数据来源：swp.li.user.battery.last / swp.li.user.battery.detail
电量字段：batteryLife（百分比整数），另有 remainCapacity / endurance / chargeStatus / reportTime

前置：session.json 里填 token + ticket（抓包 566 或 697 的响应里拿，见分析报告 §3）

用法：
  python evwatch/battery_watch.py once                # 取一次
  python evwatch/battery_watch.py once --detail       # 取一次(含健康度/续航/循环)
  python evwatch/battery_watch.py watch --interval 90 --low 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch.hb_crypto import HOST, UA, APPID, CRYPTO_HINT, AES as _AES, encrypt, sign, decrypt  # noqa: E402
from evwatch.notify import push as push_any  # noqa: E402
from evwatch.csvsink import CsvSink  # noqa: E402
from evwatch.net import urlopen as _urlopen, describe as net_describe  # noqa: E402
from evwatch import paths  # noqa: E402
from evwatch import redact  # noqa: E402

DEFAULTS = {
    'env': 'pro',
    'system_code': 'LB64',
    'version': '2.1.1',
    'app_version': '8.0.79',
    'city_code': '0755',
    'chaos_env': 'pro-1.1.3-beta.1',
}

CSV_HEADER = ['ts', 'batteryNo', 'soc', 'chargeStatus', 'current', 'volt',
              'remainCapacity', 'endurance', 'reportTime']

# code=103 = 登录信息已失效, 请重新登录（实测：token 或 ticket 任一被改动都会返回它）
LOGIN_EXPIRED = 103


class ApiError(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__(f'code={code} msg={msg}')


def post(session: dict, action: str, payload: dict) -> dict:
    """发一条加密+签名的 POST，返回解密后的 dict；code!=0 抛 ApiError"""
    plain = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
    cipher = encrypt(plain, session.get('env', 'pro'))
    sig, ts, nonce = sign(plain)

    req = urllib.request.Request(
        f'{HOST}/api?{action}',
        data=cipher.encode(),
        method='POST',
        headers={
            'content-type': 'application/json',
            'chaos': 'true',
            'x-chaos-env': session.get('chaos_env', DEFAULTS['chaos_env']),
            'systemCode': session.get('system_code', DEFAULTS['system_code']),
            'timestamp': ts,
            'nonce': nonce,
            'signature': sig,
            'User-Agent': UA,
            'Referer': f'https://servicewechat.com/{APPID}/12/page-frame.html',
        },
    )
    with _urlopen(req, timeout=15) as r:
        raw = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    txt = raw.decode('utf-8', 'replace').strip()
    obj = json.loads(txt) if txt.startswith('{') else json.loads(decrypt(txt, session.get('env', 'pro')))
    if obj.get('code') != 0:
        raise ApiError(obj.get('code'), obj.get('msg'))
    return obj


def base_payload(session: dict, action: str) -> dict:
    return {
        'systemCode': session.get('system_code', DEFAULTS['system_code']),
        'version': session.get('version', DEFAULTS['version']),
        'appVersion': session.get('app_version', DEFAULTS['app_version']),
        'action': action,
        'cityCode': session.get('city_code', DEFAULTS['city_code']),
    }


def battery_list(session: dict) -> list[dict]:
    p = base_payload(session, 'swp.li.user.battery.list')
    p.update({'token': session['token'], 'ticket': session['ticket'], 'chaosChangeBattery': True})
    return post(session, 'swp.li.user.battery.list', p).get('data') or []


def battery_last(session: dict, battery_no: str) -> dict:
    p = base_payload(session, 'swp.li.user.battery.last')
    p['waitBatteryLifeCmdResultFlag'] = True
    p['batteries'] = [{'batteryNo': battery_no}]
    p.update({'token': session['token'], 'ticket': session['ticket'], 'chaosChangeBattery': True})
    res = post(session, 'swp.li.user.battery.last', p)
    data = res.get('data') or []
    if isinstance(data, dict):
        data = [data]
    return (data or [{}])[0]


def battery_detail(session: dict, battery_no: str) -> dict:
    p = base_payload(session, 'swp.li.user.battery.detail')
    p['batteryNo'] = battery_no
    p.update({'token': session['token'], 'ticket': session['ticket'], 'chaosChangeBattery': True})
    return post(session, 'swp.li.user.battery.detail', p).get('data') or {}


def battery_status(session: dict, battery_no: str) -> dict:
    """实时电气参数（对应小程序「我的电池 → 状态」页）：总电压/电流/温度/单体电压"""
    p = base_payload(session, 'swp.li.user.battery.status')
    p['batteryNo'] = battery_no
    p.update({'token': session['token'], 'ticket': session['ticket'], 'chaosChangeBattery': True})
    return post(session, 'swp.li.user.battery.status', p).get('data') or {}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def snapshot(session: dict, detail: bool = True, force: bool = True) -> dict:
    """取一次快照。

    force=True 时先打一发 battery.last（带 waitBatteryLifeCmdResultFlag），
    这是小程序里"刷新"按钮做的事：请服务端给电池下发查询指令。
    电池睡了就不应答，服务端照样返回缓存 —— 所以它不能保证拿到实时值。
    """
    no = session.get('battery_no')
    if not no:
        lst = battery_list(session)
        if not lst:
            raise ApiError(401001, '用户未绑定电池')
        no = lst[0]['batteryNo']
        session['battery_no'] = no

    out = {'ts': int(time.time()), 'batteryNo': no}
    last, st = {}, {}
    if force:
        try:
            st = battery_status(session, no)
        except ApiError:
            raise
        except Exception:
            st = {}
    if st:
        out.update({
            'volt': _f(st.get('voltage')),          # 真实总电压(V)
            'current': _f(st.get('current')),       # 单位 A
            'envTemp': _f(st.get('envTemperature')),
            'mosTemp': _f(st.get('mosTemperature')),
            'cellMax': st.get('cellMaxVoltage'),
            'cellMin': st.get('cellMinVoltage'),
            'cellVoltages': st.get('cellVoltageList'),
        })

    if detail:
        try:
            d = battery_detail(session, no)
            if d:
                out.update({
                    'soc': d.get('batteryLife') if d.get('batteryLife') is not None else last.get('batteryLife'),
                    'chargeStatus': d.get('chargeStatus') if d.get('chargeStatus') is not None else last.get('chargeStatus'),
                    'current': out.get('current', d.get('current')),
                    'volt': out.get('volt', d.get('volt')),
                    'ah': d.get('ah'),
                    'soh': d.get('soh'),
                    'remainCapacity': d.get('remainCapacity'),
                    'endurance': d.get('endurance'),
                    'chargeCount': d.get('chargeCount'),
                    'totalMileages': d.get('totalMileages'),
                    'state': d.get('status'),
                    'reportTime': d.get('batteryLifeTimestamp') or d.get('reportTime') or last.get('reportTime'),
                })
                return out
        except ApiError:
            raise
        except Exception as e:
            print(f'   detail 失败，回退 last: {e}')

    if not last:
        raise ApiError(401001, '电池无数据（可能离线/已归还）')
    out.update({
        'soc': last.get('batteryLife'),
        'chargeStatus': last.get('chargeStatus'),
        'reportTime': last.get('reportTime'),
        'current': None,
        'volt': None,
    })
    return out


# chargeStatus 枚举（来自小程序 pages/home 渲染逻辑）：
#   "1"  → 充电中
#   "-1" → 已断电（充电器断开/断电）
#   "0"  → 未充电（正常静置；页面上不显示任何标签）
# 注意：battery.last 里是字符串 "0"，battery.detail 里是数字 0 —— 统一转 str 再比
CHARGE_LABEL = {'1': '充电中', '-1': '已断电', '0': '未充电'}


def cs(s: dict) -> str:
    v = s.get('chargeStatus')
    return '未知' if v is None else str(v)


def is_charging(s: dict) -> bool:
    return cs(s) == '1'


def state_text(s: dict) -> str:
    return CHARGE_LABEL.get(cs(s), f'未知({cs(s)})')


def session_age(session: dict) -> str:
    """显示这份登录态已经用了多久（session.json 里记 obtained_at 才有意义）"""
    t = session.get('obtained_at')
    if not t:
        return '未记录'
    try:
        ts = time.mktime(time.strptime(t, '%Y-%m-%d %H:%M:%S'))
        return f'{t}（已用 {(time.time() - ts) / 86400:.1f} 天）'
    except Exception:
        return str(t)


def eta_minutes(s: dict) -> int | None:
    """粗略估算还要充多久。

    小程序的"预计充电时长"没有对应接口字段（detail/param/status 都没有），
    是前端用「剩余容量 ÷ 充电电流」算的。这里用同样思路：
        剩余 Ah = 标称容量 × (100 - 电量)/100
        时长(h) = 剩余 Ah / 当前电流(A)
    注意磷酸铁锂是 CC-CV 充电，末段恒压会明显变慢，所以这是偏乐观的估算。
    """
    soc, cur = s.get('soc'), s.get('current')
    ah = s.get('ah') or 45
    if soc is None or cur is None or cur <= 0.1:
        return None
    if soc >= 100:
        return 0
    remain_ah = ah * (100 - min(soc, 100)) / 100
    return int(remain_ah / cur * 60)


def eta_text(s: dict) -> str:
    mins = eta_minutes(s)
    if mins is None:
        return ''
    if mins <= 0:
        return '已充满'
    h, m = divmod(mins, 60)
    dur = f'{h} 小时 {m} 分钟' if h else f'{m} 分钟'
    return f'还需约 {dur}充满（按 {s.get("current")}A 估算）'


def _extras(s: dict) -> str:
    """拼一句附加信息：电流/功率/总电压/温度/剩余容量/续航（有啥写啥）"""
    bits = []
    cur, volt = s.get('current'), s.get('volt')
    if cur is not None:
        bits.append(f"电流 {cur}A")
        if volt and cur:
            bits.append(f"约 {abs(cur) * volt:.0f}W")
    if volt is not None:
        bits.append(f"{volt}V")
    if s.get('envTemp') is not None:
        bits.append(f"环境 {s['envTemp']:.0f}℃")
    if s.get('mosTemp') is not None:
        bits.append(f"MOS {s['mosTemp']:.0f}℃")
    if s.get('remainCapacity'):
        bits.append(f"剩余 {s['remainCapacity']}")
    if s.get('endurance'):
        bits.append(f"续航 {s['endurance']}")
    return ('（' + '，'.join(bits) + '）') if bits else ''


def alert_text(s: dict) -> str:
    """低电量告警（必须含关键词「电池」）"""
    return (f"【电池低电量】电池 {s['batteryNo']} 电量 {s['soc']}%"
            f"，当前{state_text(s)}{_extras(s)}")


def milestone_text(s: dict, milestone: int, charging_min: int | None = None) -> str:
    """充电里程碑文案：80 / 90 / 100"""
    head = '【电池已充满】' if milestone >= 100 else '【电池充电进度】'
    body = f"电池 {s['batteryNo']} 已充到 {s['soc']}%"
    if charging_min is not None:
        body += f"（本次充电约 {charging_min} 分钟）"
    eta = eta_text(s)
    tail = f"，{eta}" if eta and milestone < 100 else ''
    return f"{head}{body}，当前{state_text(s)}{_extras(s)}{tail}"


def start_text(s: dict) -> str:
    """开始充电：这时候给 ETA 最有用"""
    eta = eta_text(s)
    at = ''
    mins = eta_minutes(s)
    if mins:
        at = f"，约 {time.strftime('%H:%M', time.localtime(time.time() + mins * 60))} 充满"
    return (f"【电池开始充电】电池 {s['batteryNo']} 当前 {s['soc']}%"
            f"{('，' + eta) if eta else ''}{at}{_extras(s)}")


def charge_end_text(s: dict, prev_soc, minutes: int | None) -> str:
    """充电结束：chargeStatus 从 1 变成 0 / -1"""
    tail = f"，本次充电约 {minutes} 分钟" if minutes else ''
    return (f"【电池充电结束】电池 {s['batteryNo']} 充电已结束，"
            f"当前电量 {s['soc']}%（{state_text(s)}）{tail}")


def log_ticket_expiry(session: dict) -> None:
    """ticket 失效时记一笔，用来积累真实寿命数据（append-only）"""
    try:
        t0 = time.mktime(time.strptime(session.get('obtained_at', ''), '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return
    now = time.time()
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')},"
            f"issued={session.get('obtained_at')},"
            f"lived_h={(now - t0) / 3600:.2f}\n")
    try:
        paths.ensure_dirs()
        with open(paths.log('ticket_expiry.log'), 'a', encoding='utf-8') as fh:
            fh.write(line)
    except Exception:
        pass


def push(session: dict, text: str, args) -> bool:
    """推送：钉钉优先，其次企业微信；两个都没配就只打日志"""
    return push_any(
        text,
        dingtalk_webhook=args.dingtalk or session.get('dingtalk_webhook'),
        dingtalk_secret=args.dingtalk_secret or session.get('dingtalk_secret'),
        wecom_webhook=args.webhook,
    )


class ChargeState:
    """充电状态机：喂快照，吐「该推什么消息」。

    state_path 给了就把状态落盘：进程崩溃/机器重启后接着来，不会把 80% 再推一遍。
    """

    def __init__(self, low: float = 20, milestones=(80, 90, 100), state_path: str | None = None,
                 start_push: bool = True):
        self.low = low
        self.milestones = sorted(milestones)
        self.charging: bool | None = None
        self.charge_start: int | None = None
        self.hit: set[int] = set()
        self.low_alerted = False
        self.last_soc = None
        self.state_path = state_path
        self.start_push = start_push
        self._load()

    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            d = json.load(open(self.state_path, encoding='utf-8'))
            self.charging = d.get('charging')
            self.charge_start = d.get('charge_start')
            self.hit = set(d.get('hit') or [])
            self.low_alerted = bool(d.get('low_alerted'))
            self.last_soc = d.get('last_soc')
        except Exception:
            pass

    def save(self):
        if not self.state_path:
            return
        try:
            json.dump({'charging': self.charging, 'charge_start': self.charge_start,
                       'hit': sorted(self.hit), 'low_alerted': self.low_alerted,
                       'last_soc': self.last_soc},
                      open(self.state_path, 'w', encoding='utf-8'), ensure_ascii=False)
        except Exception:
            pass

    def feed(self, s: dict) -> list[str]:
        out: list[str] = []
        soc = s.get('soc')
        now = is_charging(s)

        # 充电状态跳变
        if self.charging is not None and now != self.charging:
            if now:
                self.charge_start = s['ts']
                self.hit = set()
                if self.start_push:
                    out.append(start_text(s))
            else:
                mins = int((s['ts'] - self.charge_start) / 60) if self.charge_start else None
                out.append(charge_end_text(s, self.last_soc, mins))
                self.charge_start = None
        if self.charging is None and now:
            self.charge_start = s['ts']
        self.charging = now

        if soc is not None:
            top = max(self.milestones)
            if now:
                crossed = [m for m in self.milestones if soc >= m and m not in self.hit]
                if crossed:
                    self.hit.update(crossed)          # 一次跨多档只推最高那档，避免连发
                    mins = int((s['ts'] - self.charge_start) / 60) if self.charge_start else None
                    out.append(milestone_text(s, crossed[-1], mins))
            elif soc >= top and top not in self.hit:
                # 没在充电却满了（比如插着但充电器已断电），也只提醒一次
                self.hit.add(top)
                out.append(milestone_text(s, top))

            if soc <= self.low and not self.low_alerted and not now:
                out.append(alert_text(s))
                self.low_alerted = True
            elif soc > self.low + 5:
                self.low_alerted = False

        self.last_soc = soc
        self.save()
        return out


def mask_phone(p: str) -> str:
    """日志里不出现完整手机号：134****5358（规则统一在 evwatch/redact.py）"""
    return redact.phone(p)


def auto_relogin(session: dict, args, session_path: str) -> bool:
    """登录态失效后自动短信续期。需要 --relogin-mobile + (--code-file | --code-cmd)。"""
    from evwatch.login import login_sms, make_ssid, save_session, send_code, wait_code

    ssid = make_ssid()
    print(f'   → 自动续期：向 {mask_phone(args.relogin_mobile)} 发验证码…')
    t0 = time.time()
    send_code(args.relogin_mobile, ssid)
    code = wait_code(args, t0)
    if not code:
        print('   → 没拿到验证码，续期失败')
        return False
    data = login_sms(args.relogin_mobile, code, ssid)
    sess = save_session(session_path, args.relogin_mobile, data)
    session.update(sess)
    print(f"   → 续期成功：token={sess.get('token', '')[:8]}…")
    return True


def main() -> int:
    # launchd 把 stdout 重定向到文件时是块缓冲（4KB 才落盘），日志会长时间空白。
    # 改成行缓冲，每行立即写盘。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['ping', 'once', 'watch', 'list', 'notify', 'sim'])
    ap.add_argument('--session', default=paths.config('session.json'))
    ap.add_argument('--interval', type=int, default=90)
    ap.add_argument('--low', type=float, default=20, help='低电量阈值（仅非充电时提醒）')
    ap.add_argument('--milestones', default='80,90,100', help='充电里程碑，逗号分隔，默认 80,90,100')
    ap.add_argument('--csv', default=paths.data('battery.csv'))
    ap.add_argument('--interval-active', type=int, default=0,
                    help='充电或有电流时的轮询间隔（默认取 interval 的 1/3，最小 30s）')
    ap.add_argument('--no-start-push', action='store_true',
                    help='不推「开始充电」那条（默认推，因为里面带预计充满时间）')
    ap.add_argument('--no-force', action='store_true',
                    help='不要每次都触发主动查询（省一次请求，但可能更晚拿到新值）')
    ap.add_argument('--state', default=paths.data('.charge_state.json'), help='充电状态文件，重启后不重复推送')
    ap.add_argument('--heartbeat', type=int, default=10,
                    help='每 N 分钟打一行心跳日志（0=关闭），重定向到文件时用来确认活着')
    ap.add_argument('--relogin-mobile', help='登录态失效时自动短信续期（需配合 --code-file/--code-cmd）')
    ap.add_argument('--code-file', help='短信转发的落地文件，轮询取验证码')
    ap.add_argument('--code-dir', help='轮询该目录里最新的文件（iCloud 转发推荐）')
    ap.add_argument('--code-cmd', help='执行命令取验证码')
    ap.add_argument('--dingtalk', help='钉钉机器人 webhook，覆盖 session.json 里的配置')
    ap.add_argument('--dingtalk-secret', help='钉钉机器人「加签」密钥（没开加签就不用传）')
    ap.add_argument('--webhook', help='企业微信机器人地址')
    ap.add_argument('--text', default='【电池】测试消息', help='notify 子命令要发的文字')
    ap.add_argument('--send', action='store_true', help='sim 时真的推送（默认只打印）')
    args = ap.parse_args()

    try:
        session = json.load(open(args.session, encoding='utf-8'))
    except FileNotFoundError:
        if args.cmd == 'sim':          # sim 是离线演示，不需要登录态
            session = {}
        else:
            print(f'找不到 {args.session}。先做这两步：')
            print('  cp session.example.json session.json')
            print('  # 然后填入 token / ticket（见 README「拿一份登录态」）')
            return 2
    except json.JSONDecodeError as e:
        print(f'{args.session} 不是合法 JSON：{e}')
        return 2
    milestones = sorted({int(x) for x in str(args.milestones).split(',') if x.strip()})

    if _AES is None and args.cmd in ('ping', 'list', 'once', 'watch'):
        print('⚠️  ' + CRYPTO_HINT)
        print('   当前解释器：' + sys.executable)
        return 1

    if args.cmd == 'sim':
        st = ChargeState(args.low, milestones)
        t0 = int(time.time()) - 3600
        seq = [(18, '0'), (20, '1')] + [(s, '1') for s in range(24, 101, 4)] + [(100, '0'), (100, '-1')]
        for i, (soc, cst) in enumerate(seq):
            sample = {
                'ts': t0 + i * 90, 'batteryNo': 'DEMO0001', 'soc': soc, 'chargeStatus': cst,
                'current': 5.2 if cst == '1' else 0.0, 'volt': 65.20,
                'envTemp': 27.0, 'mosTemp': 26.0,
                'remainCapacity': f'{soc * 0.45:.2f}Ah', 'endurance': f'{int(soc * 0.97)}km',
            }
            for msg in st.feed(sample):
                print(f"[{soc:3d}% {cs(sample)}] {msg}")
                if args.send:
                    push(session, msg, args)
        print('\n（模拟结束：以上是实际会推送的消息，未发送）' if not args.send else '\n（已真实发送）')
        return 0

    if args.cmd == 'notify':
        push(session, args.text, args)
        return 0

    if args.cmd == 'ping':
        print(f'登录态取得时间: {session_age(session)}')
        try:
            lst = battery_list(session)
            print(f'登录态: 有效 (code=0)；名下电池 {len(lst)} 块')
            for b in lst:
                print('  ' + json.dumps(b, ensure_ascii=False))
            if not lst:
                print('  说明: 当前没有租借中的电池（已归还/到期），不是登录态问题')
        except ApiError as e:
            if e.code == LOGIN_EXPIRED:
                print(f'登录态: 已失效 (code={e.code} {e.msg})')
                log_ticket_expiry(session)
                print('  处理: 用手机重开一次小程序并重新抓包，换掉 session.json 里的 token / ticket')
                if args.dingtalk or session.get('dingtalk_webhook'):
                    push(session, f'【电池监控中断】哈啰登录态已失效（code={e.code}），'
                                  f'需重新抓包更新 token/ticket', args)
            else:
                print(f'登录态: 未知 (code={e.code} {e.msg})')
        return 0

    if args.cmd == 'list':
        try:
            for b in battery_list(session):
                print(json.dumps(b, ensure_ascii=False))
        except ApiError as e:
            print(f'失败: {e}')
            return 1
        return 0

    if args.cmd == 'once':
        try:
            print(json.dumps(snapshot(session, True), ensure_ascii=False, indent=2))
        except ApiError as e:
            print(f'失败: {e}')
            return 1
        return 0

    st = ChargeState(args.low, milestones, state_path=args.state,
                     start_push=not args.no_start_push)

    # 启动横幅：重定向到文件时也要能立刻看到「我活着」
    print(f'===== {time.strftime("%Y-%m-%d %H:%M:%S")} 启动 =====')
    print(f'解释器 : {sys.executable}')
    print(f'会话   : {session_age(session)}')
    active_default = args.interval_active or max(30, args.interval // 3)
    print(f'轮询   : 静默 {args.interval}s / 活跃 {active_default}s   '
          f'低电阈值: {args.low}%   里程碑: {milestones}')
    print(f'推送   : {"钉钉" if (args.dingtalk or session.get("dingtalk_webhook")) else "未配置"}'
          f'   续期: {"开启 " + mask_phone(args.relogin_mobile) if args.relogin_mobile else "关闭"}')
    print(f'CSV    : {os.path.abspath(args.csv)}')

    sink = CsvSink(args.csv, CSV_HEADER)
    login_alerted = False
    last_print = None
    last_relogin = 0.0
    last_beat = time.time()
    last_change = time.time()
    idle_nap = args.interval
    active_nap = args.interval_active or max(30, args.interval // 3)
    nap = idle_nap
    mode = None

    while True:
        try:
            s = snapshot(session, detail=True, force=not args.no_force)
            login_alerted = False
            rt = s.get('reportTime')
            if isinstance(rt, float):
                rt = int(rt)                       # 别让 1.7927E+12 进 CSV
            if sink.write([s['ts'], s['batteryNo'], s['soc'], cs(s), s.get('current'),
                           s.get('volt'), s.get('remainCapacity'), s.get('endurance'), rt]):
                # 文件被外部替换（换名/清空/重写）— 2026-10-23 静默丢了 9h44m 数据
                print(f'{time.strftime("%H:%M:%S")}  · CSV 被外部替换，已重开（数据不再丢）')
                push(session, '【电池】CSV 文件被外部替换，已自动重开，数据未丢', args)

            # 有电流/在充电 -> 电池醒着、上报密，轮询也加密；静置就降频
            cur = s.get('current') or 0
            active = is_charging(s) or abs(cur) > 0.05
            new_mode = 'active' if active else 'idle'
            if new_mode != mode:
                mode = new_mode
                nap = active_nap if active else idle_nap
                print(f"{time.strftime('%H:%M:%S')}  · 轮询切换为"
                      f"{'活跃' if active else '静默'}模式（{nap}s）")
            nap = active_nap if active else idle_nap

            key = (s['soc'], cs(s))
            if key != last_print:
                print(f"{time.strftime('%H:%M:%S')}  {s['batteryNo']}  {s['soc']}%  "
                      f"{state_text(s)}"
                      + (f"  电流 {s['current']}A / {s['volt']}V" if s.get('current') is not None else '')
                      + (f"  {s['envTemp']:.0f}℃" if s.get('envTemp') is not None else ''))
                last_print = key
                last_change = time.time()

            # 心跳：哪怕电量没变，也定期证明进程活着
            if args.heartbeat and time.time() - last_beat >= args.heartbeat * 60:
                last_beat = time.time()
                quiet = int((time.time() - last_change) / 60)
                print(f"{time.strftime('%H:%M:%S')}  · 心跳  soc={s['soc']}%  "
                      f"{state_text(s)}"
                      + (f"  电流 {s['current']}A" if s.get('current') is not None else '')
                      + f"  （已 {quiet} 分钟无变化）")

            for msg in st.feed(s):
                print('   -> ' + msg)
                push(session, msg, args)

        except ApiError as e:
            if e.code == 401001:      # 用户未绑定电池：换电/归还期间会持续出现
                session.pop('battery_no', None)   # 丢掉缓存，下次重新取列表
                print(f'[{time.strftime("%H:%M:%S")}] 当前没有绑定电池（已归还/到期），等待中…')
            elif e.code == LOGIN_EXPIRED:
                print(f'[{time.strftime("%H:%M:%S")}] 登录态失效 (code=103)')
                log_ticket_expiry(session)
                if args.relogin_mobile and (args.code_file or args.code_dir or args.code_cmd) \
                        and time.time() - last_relogin > 1800:      # 半小时最多试一次，别刷短信
                    last_relogin = time.time()
                    if auto_relogin(session, args, args.session):
                        continue
                if not login_alerted:
                    push(session, '【电池监控中断】哈啰登录态已失效（code=103），'
                                  '需重新抓包更新 token/ticket', args)
                    login_alerted = True
                time.sleep(max(args.interval, 900))
                continue
            else:
                print(f'[{time.strftime("%H:%M:%S")}] 接口报错: {e}')
        except KeyboardInterrupt:
            print('\n已停止')
            return 0
        except Exception as e:
            print(f'[{time.strftime("%H:%M:%S")}] 失败: {e}')
            print(f'   [net] 尝试过：{net_describe()}')
        try:
            time.sleep(nap)
        except KeyboardInterrupt:
            print('\n已停止')
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
