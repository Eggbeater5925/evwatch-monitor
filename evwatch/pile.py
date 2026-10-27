"""
pile.py — 麦希充充电桩数据源（深圳北电云 chargeplatformapp）

与小哈智电的差异：
  * 小哈：AES + HmacSHA1 签名，加密响应，数据是「电池电量」
  * 麦希充：明文 form-urlencoded，token 放在 body，数据是「充电订单 + 实时功率」

所以本模块不碰 hb_crypto，只做表单 POST + JSON 解析。

接口（全部来自抓包实测，详见 麦希充-接口分析.md）：
  getPersonalInfo  token + 分页        → 我的订单列表（判断有没有进行中的订单）
  getPushOrder     orderId + token     → 下单页数据，含实时功率 power / 档位 region
  getUsedTime      token               → 当前充电已用时长（秒）
  orderDetail      orderId（无需 token）→ 设备、订单全字段、结束原因、实际用电
  deduntInfo       token               → 钱包余额

⚠️ 只读。本模块不含下单、退款、支付相关的任何写操作。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request

from .net import urlopen as _urlopen

BASE = 'https://chargeplatformapp.beidiancloud.cn/app/business'
APPID = 'wx79ae23876b6643f6'
MP_VERSION = '347'
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 26_6 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.79(0x18004f30) '
      'NetType/WIFI Language/zh_CN')

# 订单结束原因字典（/app/business/getDataByKey?param=business_muser_endType，28 条实测）
END_TYPES = {
    '01': '超过最大功率上限', 'ee': '扫描结束', 'ff': '异常订单', '40': '远程停止',
    '00': '充电中', '02': '充电器过热断电/拔插头', '03': '停电', '04': '金额用完',
    '05': '充电时长用完', '06': '充满自停', '07': '电量用完', '08': '远程跳闸',
    '09': '主动关闭', '0a': '过压跳闸', '0b': '功率变化', '0c': '没接负载',
    '0d': '失败订单', '0e': '充电时长用完', '15': '充满自停', '16': '已达到充电时长上限',
    '20': '紧急开关打开', '21': '桩接地异常', '22': '充电枪连接异常',
    '23': '车辆电源管理系统主动结束充电', '30': '充满自停', '31': '充满自停',
    '50': '线路故障', '51': '箱门打开,请联系管理人员',
}

# 这些结束原因属于「故障」，不是正常充满，值得单独告警
BAD_ENDS = {'01', 'ff', '02', '03', '08', '0a', '0c', '0d', '20', '21', '22', '50', '51'}

# 同一个字段两种形态：getPersonalInfo 给代码 '0c'，orderDetail 直接给中文'没接负载'
CODE_BY_LABEL = {v: k for k, v in END_TYPES.items()}


def end_code(v) -> str:
    """结束原因统一成字典代码；认不出就原样返回"""
    s = str(v or '').strip()
    if not s or s in END_TYPES:
        return s
    return CODE_BY_LABEL.get(s, s)


class PileError(Exception):
    def __init__(self, code, msg, token_state=None):
        self.code = code
        self.msg = msg
        self.token_state = token_state
        super().__init__(f'code={code} msg={msg}')


def token_invalid(e: Exception) -> bool:
    """token 失效判定：tokenState 非 200，或 code 落在登录类错误上"""
    if isinstance(e, PileError):
        if e.token_state is not None and e.token_state != 200:
            return True
        return e.code in (401, 403, 500, 401001)
    return False


HEADERS = {
    'User-Agent': UA,
    'Referer': f'https://servicewechat.com/{APPID}/{MP_VERSION}/page-frame.html',
    'Accept': '*/*',
}


def _send(req, timeout: float = 15) -> dict:
    """发请求 + 解 JSON + 合并 data 层；code!=0 抛 PileError"""
    with _urlopen(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            import gzip
            raw = gzip.decompress(raw)
    txt = raw.decode('utf-8', 'replace').strip()
    try:
        obj = json.loads(txt)
    except json.JSONDecodeError:
        raise PileError(-1, f'响应不是 JSON：{txt[:120]}')
    state = obj.get('tokenState')
    if obj.get('code') != 0:
        raise PileError(obj.get('code'), obj.get('msg'), state)
    data = obj.get('data')
    if state is None and isinstance(data, dict):
        state = data.get('tokenState')
    if state is not None and state != 200:
        raise PileError(obj.get('code', 0), f'token 已失效（tokenState={state}）', state)
    if isinstance(data, dict):
        merged = dict(obj)
        merged.update(data)
        return merged
    return obj


def _post(path: str, fields: dict, timeout: float = 15, raw: bool = False) -> dict:
    """表单 POST。raw=True 时不检查 code（getOrderStatus 用 801 表示「已结束」）"""
    body = urllib.parse.urlencode({k: v for k, v in fields.items() if v is not None}).encode()
    req = urllib.request.Request(
        f'{BASE}/{path}', data=body, method='POST',
        headers={**HEADERS, 'content-type': 'application/x-www-form-urlencoded'},
    )
    if raw:
        with _urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    return _send(req, timeout)


def _get(path: str, params: dict, timeout: float = 15) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f'{BASE}/{path}?{qs}', method='GET', headers=HEADERS)
    return _send(req, timeout)


# ───────────────── 登录 / 在充 / 启停（2026-10-23 18:10 新抓包补上） ─────────────────

def login(phone: str) -> dict:
    """小程序自身的登录接口：GET checkPhoneNumber?phoneNumber=，返回新 token。

    ⚠️ 它**没有验证码**（见分析文档 §7 P0），所以本函数只在你显式给出本人手机号时才会被调用，
    用来替代「7 天手动抓包」。不要拿它去换别人的手机号。
    """
    d = _get('checkPhoneNumber', {'phoneNumber': phone})
    if not d.get('token'):
        raise PileError(-1, '登录响应里没有 token')
    return d


def using_orders(token: str) -> list[dict]:
    """进行中的订单列表（比 getPersonalInfo 轻，专门用来判断「现在在不在充」）"""
    d = _post('getUsingOrderList', {'token': token})
    return d.get('orderList') or []


# getOrderStatus 的业务码：801 = 订单已结束
ORDER_ENDED = 801


def order_status(token: str, order_id: str) -> tuple[int, str]:
    """订单实时状态。返回 (code, msg)；801 表示已结束，不是错误。"""
    obj = _post('getOrderStatus', {'token': token, 'orderId': order_id}, raw=True)
    return int(obj.get('code') or 0), obj.get('msg') or ''


def order_progress(token: str, order_id: str) -> dict:
    """充电进度。数据来源只有 getPushOrder 里的 order 段。

    实测（2026-10-23 18:08-18:09 那一单，注意**当时没插车**）：
      * power / elf 全程 0 —— 但这是因为没接负载，不能据此断定桩不提供
      * totalPrice 是**实时累加**的（1.00 → 4.26），可当进度用
      * startTime 在充电中就有，可以用来算真实已充时长
      * status: 9 刚下单 / 0 充电中 / 1 已结束
    """
    d = push_order(token, order_id)
    o = d.get('order') or {}
    out = {
        'orderId': order_id,
        'status': o.get('status'),
        'totalPrice': _f(o.get('totalPrice')),
        'realPrice': _f(o.get('realPrice')),
        'usedTime': _f(o.get('usedTime')),
        'usedElectric': _f(o.get('usedElectric')),
        'startTime': o.get('startTime'),
        'endTime': o.get('endTime'),
        'power': _f(d.get('power')),
        'endType': end_code(o.get('endType')),
    }
    out['endText'] = END_TYPES.get(out['endType'], out['endType'] or '')
    return out


def stop_order(token: str, order_id: str) -> dict:
    """远程结束充电。返回 {first,last}（电量读数快照）"""
    return _post('stopOrder', {'token': token, 'orderId': order_id})
    data = obj.get('data')
    if state is None and isinstance(data, dict):
        state = data.get('tokenState')
    if state is not None and state != 200:
        raise PileError(obj.get('code', 0), f'token 已失效（tokenState={state}）', state)
    # 这个后端的两个风格并存：
    #   getPersonalInfo 把 total/rows 摊在顶层，getPushOrder/orderDetail/deduntInfo 塞在 data 里
    # 统一合并成一层，调用方不用关心
    if isinstance(data, dict):
        merged = dict(obj)
        merged.update(data)
        return merged
    return obj


def orders(token: str, page_size: int = 10, page_num: int = 1) -> list[dict]:
    """我的订单列表，按接口返回顺序（最新在前）"""
    d = _post('getPersonalInfo', {'token': token, 'pageSize': page_size, 'pageNum': page_num})
    return d.get('rows') or []


def push_order(token: str, order_id: str) -> dict:
    """下单/充电页数据：orderProduct(价格) + power(实时功率 W) + region(功率档) + order"""
    return _post('getPushOrder', {'token': token, 'orderId': order_id})


def used_time(token: str) -> float | None:
    """当前充电已用时长（秒）"""
    try:
        d = _post('getUsedTime', {'token': token})
    except PileError:
        return None
    v = d.get('usedTime')
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def wallet(token: str) -> dict:
    return _post('deduntInfo', {'token': token})


def order_detail(order_id: str) -> dict:
    """订单详情。这个接口抓包里没带 token，只传 orderId。"""
    return _post('orderDetail', {'orderId': order_id})


def jwt_payload(token: str) -> dict:
    """解 JWT 的 payload。

    抓包里那两个 token 看着不一样（一个 eyJ0eXAi... 开头，一个是 eyW9iAHv...），
    但**两个都是标准 JWT**，payload 都是 {"phoneNumber": "...", "exp": ...}。
    eyW9iAHv 只是 base64 出来长得不像 JSON 而已，别被表象骗了（第一版就判断错了）。
    """
    try:
        seg = token.split('.')[1]
        seg += '=' * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception:
        return {}


def token_exp(token: str) -> int | None:
    """到期时间戳（秒）；解不出返回 None"""
    try:
        return int(jwt_payload(token).get('exp'))
    except (TypeError, ValueError):
        return None


def token_left(token: str) -> float | None:
    """还剩多少秒到期（负数=已过期）；解不出返回 None"""
    exp = token_exp(token)
    return None if exp is None else exp - time.time()


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_active(row: dict) -> bool:
    """判定这一单是否还在进行。

    实测三个信号，按可靠性排序：
      * endType：字典里 '00' 就是「充电中」，其它值都是已结束 —— 最可靠，
        getPersonalInfo 的每一行都带它
      * endTime：只有 orderDetail 才有，列表接口不返回
      * status：'0' 进行中 / '1' 已完成（实测结束单为 1）
    """
    et = end_code(row.get('endType'))
    if et:
        return et == '00'
    if (str(row.get('endTime') or '')).strip():
        return False
    return str(row.get('status')) == '0'


def pick_order(rows: list[dict]) -> dict | None:
    """优先取进行中的那一单，没有就取最新一单"""
    for r in rows:
        if _is_active(r):
            return r
    return rows[0] if rows else None


def _low_watt(region: str | None) -> int | None:
    """'151-300W' → 151；'0-150W' → 0"""
    import re
    m = re.match(r'\s*(\d+)', region or '')
    return int(m.group(1)) if m else None


def price_hint(push: dict) -> str:
    """把 orderProduct 里的电费/服务费拼成一句话（服务费按当前功率档取）"""
    op = push.get('orderProduct') or {}
    elec = _f(op.get('timePrice'))
    bits = []
    if elec:
        bits.append(f'电费 {elec:.3f} 元/度')
    raw = op.get('morerangePrice')
    if raw:
        try:
            mp = json.loads(raw)
        except (TypeError, ValueError):
            mp = {}
        low = _low_watt(push.get('region'))
        svc = None
        if low is not None:
            for p in mp.get('priceInfo') or []:
                try:
                    if int(p.get('lowPowerLimit') or 0) <= low <= int(p.get('topPower') or 0):
                        svc = p.get('servicePrice')
                        break
                except (TypeError, ValueError):
                    continue
        if svc:
            bits.append(f'服务费 {svc} 元/度（{push.get("region")} 档）')
    return ' + '.join(bits)


def snapshot(token: str, order_id: str | None = None, order_row: dict | None = None) -> dict:
    """取一次快照。

    轮询成本：进行中的订单 2~3 个请求（getPersonalInfo → getPushOrder → getUsedTime），
    空闲时只有 1 个（getPersonalInfo）。
    """
    row = order_row
    if order_id and row is None:
        try:
            row = next((r for r in orders(token) if r.get('orderId') == order_id), None)
        except PileError:
            raise
        except Exception:
            row = None
    if not order_id:
        rows = orders(token)
        row = pick_order(rows)
        if not row:
            return {'ts': int(time.time()), 'orderId': None, 'active': False, 'idle': True}
        order_id = row.get('orderId')

    active = _is_active(row or {})
    out = {
        'ts': int(time.time()),
        'orderId': order_id,
        'active': active,
        'idle': False,
        'soc': None,                      # 充电桩不对外暴露电池 SOC，只能看功率
        'power': None, 'region': None,
        'usedTime': _f((row or {}).get('usedTime')),
        'usedElectric': _f((row or {}).get('usedElectric')),
        'totalPrice': _f((row or {}).get('totalPrice')),
        'realPrice': _f((row or {}).get('realPrice')),
        'endType': (row or {}).get('endType'),
        'priceType': (row or {}).get('priceType'),
    }

    if active:
        try:
            p = push_order(token, order_id)
            out['power'] = _f(p.get('power'))
            out['region'] = p.get('region')
            out['priceHint'] = price_hint(p)
            op = p.get('orderProduct') or {}
            out['productName'] = op.get('productName')
        except PileError:
            raise
        except Exception:
            pass
        ut = used_time(token)
        if ut is not None:
            out['usedTime'] = ut
    else:
        # 已结束：详情里才有结束原因对应的用电量与金额
        try:
            d = order_detail(order_id)
            o = d.get('order') or {}
            dev = d.get('device') or {}
            out.update({
                'usedTime': _f(o.get('usedTime')) if _f(o.get('usedTime')) is not None else out['usedTime'],
                'usedElectric': _f(o.get('usedElectric')),
                'totalPrice': _f(o.get('totalPrice')),
                'realPrice': _f(o.get('realPrice')),
                'endType': end_code(o.get('endType') or out.get('endType')),
                'startTime': o.get('startTime'),
                'endTime': o.get('endTime'),
                'deviceName': dev.get('deviceName'),
                'deviceAddress': dev.get('deviceAddress'),
                'refundStatus': o.get('refundStatus'),
                'paid': o.get('paid'),
            })
        except Exception as e:
            print(f'   orderDetail 失败：{e}')

    out['endType'] = end_code(out.get('endType'))
    out['endText'] = END_TYPES.get(out['endType'], out['endType'] or '')
    return out


def fmt_dur(sec) -> str:
    if sec is None:
        return '未知'
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f'{h} 小时 {m} 分'
    if m:
        return f'{m} 分 {s} 秒'
    return f'{s} 秒'
