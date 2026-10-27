"""
huiming.py — 荟铭充电桩数据源（wx.szhuilaidian.com，深圳市荟铭科技）

与麦希充（pile.py）的差异：
  * 麦希充 token 是 JWT，7 天过期，能手机号直换
  * 荟铭 token 是 UUID（profile 接口里返回），没有 exp；抓包里也**没有登录接口**
  * 荟铭给实时功率 charging_w 和已充电量 electrics，麦希充这两项一直是 0
  * 荟铭能远程结束充电（closeCharge），但没有「结束原因」字典，只有状态码

接口（全部来自 2026-10-23 18:31 抓包，详见 荟铭-接口分析.md）：
  profile         token                     → 用户信息（token 也在这里返回）
  hasUsingOrder   token [macno]             → 有没有进行中的订单（最轻，判断在不在充）
  details         order_id + ctime + token  → 订单全字段：功率/电量/金额/状态
  closeCharge     order_id + token          → 远程结束充电（唯一的写操作）
  getOrderList    page/pagesize/status      → 订单列表
  getDeviceInfo   macno + token             → 设备 + 插座列表
  getSocketInfo   macno + socket_id + token → 单个插座 + 电价
  index           lng/lat/page/pagesize     → 附近站点

状态码（抓包实测只有这两个）：
  131 = 充电中（finish_time 为 0）
  666 = 已结束（finish_time 有值）

⚠️ 除 close_charge() 外全部只读；close_charge 只在 watch 判满且没加 --no-auto-stop 时调用。
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from .net import urlopen as _urlopen

BASE = 'https://wx.szhuilaidian.com'
APPID = 'wx9e8dc02a60df8d1f'
MP_VERSION = '72'
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.79(0x18004f30) '
      'NetType/WIFI Language/zh_CN')

# 订单状态
CHARGING = 131
FINISHED = 666
STATUS_TEXT = {CHARGING: '充电中', FINISHED: '已结束'}


def status_text(v) -> str:
    try:
        return STATUS_TEXT.get(int(v), f'未知状态({v})')
    except (TypeError, ValueError):
        return f'未知状态({v})'


class HmError(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__(f'code={code} msg={msg}')


def token_invalid(e: Exception) -> bool:
    """token 失效判定。

    抓包里没抓到失效响应，只能按 msg 关键词兜底；宁可漏判也别把网络错误当失效。
    """
    if isinstance(e, HmError):
        m = str(e.msg or '')
        if any(k in m for k in ('登录', '未登录', 'token失效', 'token 失效', '重新登录')):
            return True
        return e.code in (401, 403)
    return False


def _post(path: str, params: dict, timeout: float = 10) -> object:
    """POST form-urlencoded。和麦希充不同：成功码是 code == 1，不是 0。"""
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f'{BASE}{path}', data=data, method='POST',
        headers={
            'appid': APPID,
            'content-type': 'application/x-www-form-urlencoded',
            'User-Agent': UA,
            'Referer': f'https://servicewechat.com/{APPID}/{MP_VERSION}/page-frame.html',
        },
    )
    with _urlopen(req, timeout=timeout) as r:
        raw = r.read().decode('utf-8', 'replace')
    try:
        obj = json.loads(raw)
    except ValueError:
        raise HmError(-1, f'响应不是 JSON：{raw[:120]}')
    code = obj.get('code')
    if code != 1:
        raise HmError(code, obj.get('msg') or '')
    return obj.get('data')


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ────────────────────────── 只读接口

def profile(token: str) -> dict:
    """用户信息。注意：新 token 也在这个响应里（data.userinfo.token）"""
    return _post('/vv/usercenter/api/user/profile', {'token': token}) or {}


def has_using(token: str, macno: str = '') -> dict:
    """有没有进行中的订单。返回 {has: 0/1, info: {order_id, ctime, using_time}}

    这是判断「在不在充」最轻的接口，不用拉订单详情。
    """
    p = {'token': token}
    if macno:
        p['macno'] = macno
    return _post('/vv/order/api/index/hasUsingOrder', p) or {}


def details(token: str, order_id, ctime=None) -> dict:
    """订单全字段。ctime 可以传 'undefined'（抓包里就这么干的），说明服务端不校验。"""
    p = {'order_id': str(order_id), 'token': token}
    if ctime is not None:
        p['ctime'] = str(ctime)
    return _post('/vv/order/api/index/details', p) or {}


def orders(token: str, page: int = 1, pagesize: int = 10, status: str = '') -> list[dict]:
    """订单列表。status='' 全部，'666' 已结束"""
    d = _post('/vv/order/api/index/getOrderList',
              {'page': page, 'pagesize': pagesize, 'status': status, 'token': token}) or {}
    return d.get('list') or []


def device_info(token: str, macno: str) -> dict:
    """设备 + 插座列表（socket[]）"""
    return _post('/vv/device/api/index/getDeviceInfo', {'macno': macno, 'token': token}) or {}


def socket_info(token: str, macno: str, socket_id) -> dict:
    """单个插座的详情 + 电价"""
    return _post('/vv/device/api/index/getSocketInfo',
                 {'macno': macno, 'socket_id': str(socket_id), 'token': token}) or {}


def nearby(token: str, lng: float, lat: float, pagesize: int = 100) -> list[dict]:
    """附近站点（需要经纬度；本项目只在 sim/调试里用）"""
    d = _post('/vv/device/api/index/index',
              {'status': 1, 'lng': lng, 'lat': lat, 'page': 1,
               'pagesize': pagesize, 'token': token}) or {}
    return d.get('list') or []


# ────────────────────────── 唯一的写操作

def close_charge(token: str, order_id) -> dict:
    """远程结束充电。调用前请确认 order_id 是本人的、正在进行的订单。"""
    return _post('/vv/order/api/index/closeCharge',
                 {'order_id': str(order_id), 'token': token}) or {}


# ────────────────────────── 组合

def is_active(o: dict) -> bool:
    """充电中：status == 131 且还没 finish_time"""
    try:
        return int(o.get('status')) == CHARGING and not int(o.get('finish_time') or 0)
    except (TypeError, ValueError):
        return False


def snapshot(token: str, order_id=None) -> dict:
    """一份完整快照。

    优先用 hasUsingOrder 找在充订单（轻），找不到再取最近一单（用来报「刚结束」）。
    """
    has = has_using(token)
    active = bool(has.get('has'))
    info = has.get('info') or {}

    oid = str(info.get('order_id') or '') if active else ''
    ctime = info.get('ctime') if active else None
    if order_id:
        oid = str(order_id)
        ctime = None

    if not oid:
        rows = orders(token, pagesize=1)
        if not rows:
            return {'ts': int(time.time()), 'active': False, 'idle': True}
        row = rows[0]
        oid = str(row.get('id') or '')
        ctime = row.get('ctime')
        if not oid:
            return {'ts': int(time.time()), 'active': False, 'idle': True}

    d = details(token, oid, ctime)
    out = {
        'ts': int(time.time()),
        'idle': False,
        'active': is_active(d),
        'orderId': oid,
        'number': d.get('number'),
        'status': d.get('status'),
        'statusText': status_text(d.get('status')),
        'power': _f(d.get('charging_w')),
        'energy': _f(d.get('electrics')),
        'money': _f(d.get('orders_money')),
        'realMoney': _f(d.get('realprice')),
        'usedTime': int(_f(d.get('use_times'))),
        'startTime': int(_f(d.get('start_time'))),
        'finishTime': int(_f(d.get('finish_time'))),
        'ctime': int(_f(d.get('ctime'))),
        'hour': _f(d.get('hour')),
        'title': d.get('title') or '',
        'address': d.get('address') or '',
        'socket': d.get('socket_number'),
        'macno': d.get('macno') or '',
        'elecPrice': d.get('elec_price') or {},
        'raw': d,
    }
    # 已结束的单：时长要用 finish_time - start_time，不能用「现在 - 开始」
    if out['startTime']:
        end = out['ts'] if out['active'] else (out['finishTime'] or out['ts'])
        out['elapsed'] = max(0, end - out['startTime'])
    else:
        out['elapsed'] = None
    return out


def price_hint(p: dict) -> str:
    """把电价结构拼成一句话：电费 0.69 + 服务费按功率分档 0.45~1.5"""
    ep = p.get('elecPrice') or {}
    bits = []
    if ep.get('elec_fee') is not None:
        bits.append(f'电费 {ep["elec_fee"]} 元/度')
    power = ep.get('power') or []
    if power:
        lo = power[0].get('price')
        hi = power[-1].get('price')
        bits.append(f'服务费 {lo}~{hi} 元/度（按功率 {len(power)} 档）')
    return ' + '.join(bits)
