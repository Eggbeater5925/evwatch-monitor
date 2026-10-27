"""
tower.py — 中国铁塔充电（charge-api.chinatowercom.cn）：电池端说停了就结束桩

接口来自小程序 wxf295d9e03b73e077 的抓包（解包：wx-mp-mcp），报文是**明文 JSON**，
只有 Bearer JWT 鉴权，没有哈啰那套 AES + HMAC 签名，所以模块很薄。

**这个脚本存在的理由**：桩的「充满自停」不可信 —— 电池 BMS 已经报「不在充电」了，
桩还在按时长继续计费（实测会多吃近一小时服务费）。所以判据以**哈啰电池端**为准，
电池一停就调 endCharge 把桩侧订单结束，桩侧电流只作兜底。

用到的接口（都在抓包里验证过）：
  POST /charge/core/toc/charging/list               {}                      ← 进行中的充电（最轻，~570B，轮询首选）
  POST /charge/core/toc/charging/detail/by/plugNum  {"plugNum":"..."}      ← 同一份数据，按插口查
  POST /charge/primary/toc/order/query/charging/list {"cardUid":""}        ← 订单视角（startTime/endTime）
  POST /charge/core/toc/charge/endCharge            {"plugId","orderNum","openDoor"}
  POST /charge/primary/toc/order/query/detail       {"state","orderNum","costAllMoney","rid"}
  POST /charge/bridge/toc/charge/scanQrCode         {"plugNum":"..."}      ← plugId / 余额 / 站点
  POST /charge/core/toc/prepaid/money/details       {"stationId":N}        ← fullStopMoney / startChargingMoney
  POST /charge/bridge/toc/charge/start              {plugId,time,unit,...} ← 开充（客户端 siteOrder 页走这个）

关键字段（charging/list 的 resData）：
  current        电流 A
  voltage        220V（交流桩，固定）
  power          功率 W（客户端 <1000 显示 W，否则转 kW；开充瞬间是 0）
  chargingDegree 已充电量 kWh
  chargingTimeTotal 已充时长，**单位分钟**（客户端「已充时长」直接用它）
  costMoney      已扣金额；paymentMoney 预付金额
  chargeType     **0 = 充满自停，5 = 按时长**（客户端 siteOrder 的按钮就是这个）
  endType        0=到时自动结束  2=人工/接口结束（抓包里 472 之后就是 2）

客户端确实有「充满自停」选项（`chargeType=0`、预扣站点 `fullStopMoney`），
但它的「满」是**桩侧判断**、判得晚，实测电池停了还继续扣钱 —— 所以别指望它，
用 `watch` 以电池端为准提前结束。

登录态（tower_session.json 里的 authorization）：
  * **抓包 token 优先**：JWT 的 exp 约半年，一份抓包能用很久
  * 但它是**单点登录** —— 手机上再进一次小程序，手上这份立刻作废：
    `code=303 账号已在其他设备登录`（或 401）
  * 所以策略是「抓包优先、短信兜底」：
      抓包   → `python3 evwatch/tower_login.py use-capture`（从最近一份请求里抠 Authorization）
      兜底   → `python3 evwatch/tower_login.py relogin --mobile 手机号`（验证码取 iCloud 转发文件）
      watch  → 遇 303 自动走短信兜底（`--no-relogin` 可关，半小时最多一次）

用法：
  python3 evwatch/tower.py status                 # 现在在充吗？充到哪了
  python3 evwatch/tower.py watch                  # 常驻：电池端一停 → 立刻结束桩（推荐）
  python3 evwatch/tower.py watch --dry-run        # 只判定不真停（第一次先这么跑）
  python3 evwatch/tower.py watch --pile-only      # 不读电池，退回桩侧电流兜底
  python3 evwatch/tower.py start --plug HBLF2000xxxx --yes                 # 按时长开充
  python3 evwatch/tower.py start --plug HBLF2000xxxx --full --yes          # 开「充满自停」（桩判得晚）
  python3 evwatch/tower.py stop                   # 立刻结束当前这单（人工）
  python3 evwatch/tower.py list                   # 进行中/最近订单
  python3 evwatch/tower.py sim                    # 离线演示判定逻辑
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import json
import os
import sys
import time
import urllib.request
from collections import deque

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch.net import describe as net_describe  # noqa: E402
from evwatch.net import urlopen as _urlopen  # noqa: E402
from evwatch.notify import dingtalk  # noqa: E402
from evwatch import paths  # noqa: E402
from evwatch import redact  # noqa: E402

BASE = 'https://charge-api.chinatowercom.cn:18443'
APPID = 'wxf295d9e03b73e077'
# iOS 快捷指令把验证码写这里（短信兜底取码用，和哈啰共用）
ICLOUD_DIR = os.path.expanduser('~/Library/Mobile Documents/com~apple~CloudDocs/evwatch')
# iOS 上抓包拿到的 Authorization 丢这里，watch 会自动热替换
TOKEN_INBOX = os.path.join(ICLOUD_DIR, 'tower_token.txt')
UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 26_6_1 like Mac OS X) AppleWebKit/605.1.15 '
      '(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.79(0x18004f30) NetType/WIFI '
      'Language/zh_CN')

OK = 200
KICKED = 303          # 账号已在其他设备登录
UNAUTHORIZED = (301, 302, 401, 403)


class TowerError(Exception):
    def __init__(self, code, msg):
        self.code = code
        self.msg = msg
        super().__init__(f'code={code} msg={msg}')


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def end_charge_payload(plug_id, order_num: str, open_door: bool = True) -> dict:
    """结束充电的请求体（抓包里就是这三个字段）"""
    return {'plugId': _int(plug_id), 'orderNum': order_num,
            'openDoor': 1 if open_door else 0}


# chargeType：客户端 siteOrder 页里只有一个用处 —— 点「充满自停」= 0，选 1/2/3/10/20 小时 = 5
CHARGE_TYPE_FULL = 0      # 充满自停（预扣 fullStopMoney，桩自己判断满，多退少补）
CHARGE_TYPE_TIME = 5      # 按时长（抓包里那单就是 chargeType=5 + time=2 + unit=h）


def start_payload(plug_id, *, full: bool = False, hours: int = 2, pay_money=2,
                  payment_method: int = 1, pay_type: int = 2,
                  charge_type: int | None = None, site_type: int = 1) -> dict:
    """开始充电的请求体。

    字段顺序与客户端 `startCharge()` 里构造的一致：
      {plugId, time, unit:"h", sourceType:1, paymentMethod, payType, payMoney,
       chargeType, type, startType:1}

    充满自停（客户端 `doClick('充满自停')`）：
      chargeType = 0，`selectTime = 0`（非微信支付分支），payMoney = fullStopMoney
    按时长：chargeType = 5，time = 小时数
    """
    ct = CHARGE_TYPE_FULL if full else (CHARGE_TYPE_TIME if charge_type is None else charge_type)
    return {
        'plugId': str(plug_id),           # 客户端这里传的是字符串（endCharge 里才是数字）
        'time': 0 if full else int(hours),
        'unit': 'h',
        'sourceType': 1,
        'paymentMethod': int(payment_method),
        'payType': int(pay_type),
        'payMoney': pay_money,
        'chargeType': ct,
        'type': 2 if site_type == 7 else 1,   # 客户端：7==站点type ? 2 : 1
        'startType': 1,
    }


# ────────────────────────────── 接口客户端 ──────────────────────────────

class Tower:
    def __init__(self, session: dict, timeout: float = 15):
        token = (session.get('authorization') or '').strip()
        if token.lower().startswith('bearer '):
            token = token[7:].strip()
        if not token:
            raise TowerError(None, 'tower_session.json 里没有 authorization')
        self.token = token
        self.device_id = session.get('device_id') or 'abc123'
        self.host = session.get('host') or BASE
        self.timeout = timeout

    def post(self, path: str, payload=None):
        data = json.dumps(payload if payload is not None else {}, separators=(',', ':')).encode()
        req = urllib.request.Request(
            self.host + path, data=data, method='POST',
            headers={
                'content-type': 'application/json;charset=UTF-8',
                'deviceId': self.device_id,
                'Authorization': f'Bearer {self.token}',
                'platform': 'wechat',
                'User-Agent': UA,
                'Referer': f'https://servicewechat.com/{APPID}/84/page-frame.html',
            })
        with _urlopen(req, timeout=self.timeout) as r:
            raw = r.read()
            if (r.headers.get('Content-Encoding') or '').lower() == 'gzip':
                raw = gzip.decompress(raw)
        txt = raw.decode('utf-8', 'replace').strip()
        if not txt:
            return None
        try:
            obj = json.loads(txt)
        except json.JSONDecodeError:
            raise TowerError(None, f'返回不是 JSON：{txt[:120]}')
        if obj.get('code') != OK:
            raise TowerError(obj.get('code'), obj.get('msg'))
        return obj.get('data')

    # —— 读 ——
    def charging(self) -> dict:
        """进行中的充电（没有就是 None）"""
        return self.post('/charge/core/toc/charging/list')

    def charging_by_plug(self, plug_num: str) -> dict:
        return self.post('/charge/core/toc/charging/detail/by/plugNum', {'plugNum': plug_num})

    def charging_orders(self) -> list:
        d = self.post('/charge/primary/toc/order/query/charging/list', {'cardUid': ''})
        return d if isinstance(d, list) else []

    def order_detail(self, order_num: str, state='1', cost_all_money=0, rid=None) -> dict:
        return self.post('/charge/primary/toc/order/query/detail',
                         {'state': str(state), 'orderNum': order_num,
                          'costAllMoney': cost_all_money, 'rid': rid})

    def user_info(self) -> dict:
        return self.post('/charge/core/toc/user/getUserInfo')

    def scan(self, plug_num: str) -> dict:
        """扫码信息：plugId / 站点 / 余额 / 费率模板 —— 开充前必须先拿 plugId"""
        return self.post('/charge/bridge/toc/charge/scanQrCode', {'plugNum': plug_num})

    def station_config(self, station_id) -> dict:
        """站点计费配置：fullStopMoney（充满自停预扣）/ startChargingMoney / switchStatus"""
        return self.post('/charge/core/toc/prepaid/money/details',
                         {'stationId': _int(station_id)}) or {}

    # —— 写 ——
    def end_charge(self, plug_id, order_num: str, open_door: bool = True) -> dict:
        """结束充电。openDoor=1 会顺手把仓门打开（抓包里小程序就是这么发的）。"""
        return self.post('/charge/core/toc/charge/endCharge',
                         end_charge_payload(plug_id, order_num, open_door))

    def start_charge(self, payload: dict) -> dict:
        """开始充电。走 bridge 网关（抓包里客户端 siteOrder 页用的就是这个）。"""
        return self.post('/charge/bridge/toc/charge/start', payload)



# ────────────────────────────── 快照归一化 ──────────────────────────────

def snap_from_detail(data: dict) -> dict | None:
    """把 charging/list 或 charging/detail 的返回压成一个扁平快照；没在充就是 None

    ⚠️ 两个开关（在 **data 这一层**，不在 resData 里）决定「这些数能不能信」，
    客户端自己也是看它们决定要不要渲染（siteOrder 页 `applyChargingDetail`）：
      isReadCurrent   false → 桩没上报实时读数，current/voltage/power 只是推算值，
                             小程序会直接隐藏「实时电压/电流/功率」三行
      isDisplayDegree false → 桩没上报充电度数，chargingDegree 恒为 0，没有意义，
                             小程序会隐藏「已充电量」
    实测（2026-10-23 福田某桩）：两个都是 false，current 稳定在 5.37A（= 1180W/220V），
    电池侧却是 66.98V×16.99A≈1.14kW —— 数值自洽，但桩侧那三个数不是实测。
    """
    if not isinstance(data, dict):
        return None
    res = data.get('resData')
    if not res:
        return None
    now_ms = res.get('currentTime') or int(time.time() * 1000)
    start_ms = res.get('startTime') or now_ms
    return {
        'orderNum': res.get('orderNum'),
        'plugNum': res.get('plugNum'),
        'plugId': res.get('plugId'),
        'station': res.get('stationName'),
        'startTs': start_ms / 1000.0,
        'elapsed': max(0.0, (now_ms - start_ms) / 1000.0),
        'minutes': _f(res.get('chargingTimeTotal')),   # 单位：分钟（客户端「已充时长」直接用）
        'current': _f(res.get('current')),
        'voltage': _f(res.get('voltage')),
        'power': _f(res.get('power')),                 # 单位：W（就是 U×I，不是独立测量值）
        'degree': _f(res.get('chargingDegree')),
        'cost': _f(res.get('costMoney')),
        'prepaid': _f(res.get('paymentMoney')),
        'chargeType': res.get('chargeType'),           # 0 = 充满自停，5 = 按时长
        'endType': res.get('endType'),                 # None=进行中；2=接口/人工结束
        'payType': res.get('payType'),
        'read_current': bool(data.get('isReadCurrent')),    # 桩是否上报实时读数
        'has_degree': bool(data.get('isDisplayDegree')),    # 桩是否上报充电度数
    }


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return '—'
    m = int(sec // 60)
    h, m = divmod(m, 60)
    return f'{h}小时{m}分' if h else f'{m}分钟'


def charge_label(v) -> str:
    """chargeStatus 的数字翻译成人话（detail 里是数字，last 里是字符串，都兼容）"""
    if v is None:
        return '状态未知'
    s = str(v)
    return {'1': '充电中', '-1': '已断电', '0': '未充电'}.get(s, f'未知({s})')


def snap_text(s: dict) -> str:
    bits = [f"插口 {s.get('plugNum')}"]
    # 桩的实测值是 power（小程序「实时功率」就是它）；current = power/220 是换算出来的，
    # isReadCurrent=false 时客户端会把「实时电流」那一行隐藏，所以这里优先说功率。
    if s.get('power'):
        bits.append(f"桩 {s['power'] / 1000:.2f}kW")
    if s.get('read_current') and s.get('current') is not None:
        bits.append(f"{s.get('current')}A / {s.get('voltage')}V")
    if s.get('has_degree') and s.get('degree') is not None:
        bits.append(f"{s['degree']:.2f}度")
    if s.get('chargeType') == CHARGE_TYPE_FULL:
        bits.append(f"充满自停（预扣 ¥{s.get('prepaid') or 0:.2f}）")
    if s.get('cost') is not None:
        bits.append(f"已扣 ¥{s['cost']:.2f}")
    bits.append(f"已充 {fmt_dur(s.get('elapsed'))}")
    return '，'.join(bits)


# ────────────────────────────── 该不该停（以电池端为准）──────────────────────────────

class StopDecider:
    """以**电池端**为准决定何时结束桩侧充电。

    为什么不信桩：桩的「充满自停」判得很晚 —— 电池 BMS 已经报「不在充电」了，
    桩还在按时长继续算服务费（实测会多吃近一小时）。所以判据只能来自电池（哈啰 BMS），
    桩侧电流只作兜底（电池数据拿不到 / 桩不上报时才用）。

    判据（任一成立即可，都要过保温 + 连续确认）：
      A. chargeStatus 从「1 充电中」变成「0/-1」—— 电池自己说停了
      B. SOC >= 100 且电池侧电流 |I| <= full_current 连续 N 次（默认 0.5A：满电后是浮充）
      C. 兜底：桩侧电流跌到 max(min_current, 峰值 × peak_ratio) 以下连续 N 次

    防误杀：
      1. 必须先看到过「充电中」（seen_charging），否则刚插上时 chargeStatus=0 会立刻误判
      2. warmup_min：开充后 N 分钟内不判
      3. confirm：连续 N 次（且电池 reportTime 有前进，同一份缓存不重复计数）
    """

    def __init__(self, confirm=2, warmup_min=3, full_current=0.5, min_current=0.3,
                 peak_ratio=0.25, active_current=1.0, pile_fallback=True,
                 target_soc=0.0, cold_soc=98.0):
        self.confirm = confirm
        self.warmup_min = warmup_min
        self.full_current = full_current
        self.min_current = min_current
        self.peak_ratio = peak_ratio
        self.active_current = active_current
        self.pile_fallback = pile_fallback
        self.target_soc = target_soc          # >0：充到该 SOC 就停（用户主动「够用就停」）
        self.cold_soc = cold_soc              # 冷启动兜底：没见「充电中」但电量已到这个高度
        self.order = None
        self.peak = 0.0
        self.pile_active = False
        self.seen_charging = False
        self.stop_streak = 0
        self.pile_streak = 0
        self.last_report = None
        self.samples = 0

    def reset(self, order=None):
        self.order = order
        self.peak = 0.0
        self.pile_active = False
        self.seen_charging = False
        self.stop_streak = 0
        self.pile_streak = 0
        self.last_report = None
        self.samples = 0

    def pile_threshold(self) -> float:
        return max(self.min_current, self.peak * self.peak_ratio)

    def feed(self, pile: dict, batt: dict | None = None) -> dict | None:
        """pile = 桩侧快照；batt = {'soc','status','current','report_ts'}（拿不到就 None）"""
        if pile.get('orderNum') != self.order:
            self.reset(pile.get('orderNum'))
        self.samples += 1
        cur = pile.get('current')
        elapsed_min = (pile.get('elapsed') or 0) / 60.0
        if cur is not None:
            if cur >= self.active_current:
                self.pile_active = True
                self.peak = max(self.peak, cur)
            if self.pile_active and cur <= self.pile_threshold():
                self.pile_streak += 1
            else:
                self.pile_streak = 0

        base = {'elapsed_min': round(elapsed_min, 1), 'samples': self.samples,
                'pile_current': cur, 'peak': round(self.peak, 2)}

        # —— 电池端 ——
        if batt:
            status = None if batt.get('status') is None else str(batt.get('status'))
            soc = batt.get('soc')
            bcur = batt.get('current')
            ts = batt.get('batt_ts', batt.get('report_ts'))
            fresh = self.last_report is None or ts != self.last_report
            if ts is not None:
                self.last_report = ts

            if status == '1':
                self.seen_charging = True

            detail = dict(base, soc=soc, batt_status=status, batt_current=bcur,
                          batt_age_min=(round((batt.get('age') or 0) / 60, 1)
                                        if batt.get('age') else None))

            if elapsed_min >= self.warmup_min:
                # ① 正常路径：chargeStatus 从 1 变 0/-1（必须先见过「充电中」）
                # ② 冷启动兜底：服务重启 / 断网很久之后才恢复，seen_charging 已丢，
                #    但电池此时报「未充电/断电」且电量很高 —— 不能因为没见过 1 就不管
                # ③ 目标电量：用户主动「够用就停」（--target-soc）
                cold = (not self.seen_charging and status in ('0', '-1')
                        and soc is not None and soc >= self.cold_soc)
                stopped_by_status = (self.seen_charging and status in ('0', '-1')) or cold
                stopped_by_soc = (self.seen_charging and soc is not None and soc >= 100
                                  and bcur is not None and abs(bcur) <= self.full_current)
                hit_target = bool(self.target_soc) and soc is not None and soc >= self.target_soc
                # 「已经停了」这个方向不必等新报告：同一份缓存连着两次仍然报停，就是事实。
                # （原先硬等 reportTime 前进，实测多等 2 分钟、白付 0.11 元）
                age = batt.get('age')
                strong = (status in ('0', '-1') and soc is not None and soc >= self.cold_soc
                          and age is not None and age <= 180)   # 缓存太旧就不当证据
                if (stopped_by_status or stopped_by_soc or hit_target) and (fresh or strong):
                    self.stop_streak += 1
                else:
                    self.stop_streak = 0

                if self.stop_streak >= self.confirm:
                    if hit_target and not (stopped_by_status or stopped_by_soc):
                        why = (f'已充到目标电量 {soc}%'
                               f'（--target-soc {self.target_soc:g}，连续 {self.stop_streak} 次）')
                    elif cold and not self.seen_charging:
                        label = '已断电' if status == '-1' else '未充电'
                        why = (f'重启/断网恢复后首次读到电池已是「{label}」且电量 {soc}%'
                               f'（连续 {self.stop_streak} 次）')
                    elif stopped_by_status:
                        label = '已断电' if status == '-1' else '未充电'
                        why = (f'电池 chargeStatus 已从「充电中」变为「{label}」'
                               f'（连续 {self.stop_streak} 次）')
                    else:
                        why = (f'电池 SOC {soc}%、电流 {bcur}A 已接近 0'
                               f'（连续 {self.stop_streak} 次）')
                    return dict(detail, source='电池端',
                                reason=f'{why}；桩侧还在 {cur}A 继续计时，已结束桩侧订单')

        if not self.pile_fallback or elapsed_min < self.warmup_min:
            return None

        # C. 兜底：桩侧电流跌落
        if self.pile_active and cur is not None and cur <= self.pile_threshold() \
                and self.pile_streak >= max(self.confirm, 2):
            return dict(base, source='桩侧兜底',
                        reason=(f'电池端没给出「已停充」信号（status={batt.get("status") if batt else "无数据"}），'
                                f'退回桩侧判据：电流降至 {cur}A（峰值 {self.peak}A，'
                                f'阈值 {self.pile_threshold():.2f}A），连续 {self.pile_streak} 次'))
        return None

    def warn_no_current(self, elapsed_min: float) -> bool:
        """充了很久却没有有效信号 —— 判不了，尽早告诉人"""
        return (self.samples >= 3 and not self.pile_active and not self.seen_charging) \
            and elapsed_min >= max(20, self.warmup_min * 2)


class ValueStop:
    """「再充一小时值不值」—— 用近 15 分钟斜率外推下一个小时的收益，不划算就提前收。

    背景（2026-10-23 实盘 + 费率截图）：铁塔这个站的服务费是**按小时**收的，而且
    **随功率档位阶梯**（0-100W 0.45 元/时 … 1000-1200W 1.00 元/时 … ≥1200W 1.20 元/时），
    电费另按 0.69 元/度。实测 1.19kW → 服务费 1.00 + 电费 0.82 = 1.82 元/时，
    与账单速率 1.80 元/时吻合。

    于是末端的账非常难看：CV 段功率掉到 600W 时，一小时只充进几个点，却仍要付
    ~1.06 元（0.5 元/%）；而中段只要 0.05 元/%。这就是「剩下的没啥价值」。

    判据（都要成立）：
      1. SOC >= min_soc（默认 95，低于它绝不停）
      2. 今后 lookahead 分钟内预计涨幅 < min_gain（默认 2 个点）
         或 每 1% 的成本 > max_per_pct（默认 0.15 元/%，中段的 3 倍）
      3. 已充 >= min_minutes（默认 30），避免刚上桩误判
      另外：若这段时间内能充满，不拦，让判据 ① 正常收尾。
    """

    def __init__(self, min_soc=95.0, min_gain=2.0, max_per_pct=0.15,
                 lookahead_min=60.0, confirm=2, min_minutes=30.0, window_min=15.0,
                 block_unit=0, pre_stop=5.0):
        self.min_soc = min_soc
        self.min_gain = min_gain
        self.max_per_pct = max_per_pct
        self.lookahead = lookahead_min if lookahead_min and lookahead_min > 0 else 60.0
        self.confirm = confirm
        self.min_minutes = min_minutes
        self.window_min = window_min
        self.block_unit = int(block_unit or 0)   # 0=自动识别；30/60=人工指定整段计费
        self.pre_stop = pre_stop                 # 离边界还剩几分钟时才决定要不要跨
        self.reset(None)

    def reset(self, order=None):
        self.order = order
        self.hist = deque()          # (已充分钟, SOC, 已扣金额)
        self.streak = 0
        self.last_info = None
        # 计费形态识别：整段计费的桩 cost 会「长时间不动、边界一次性跳一整段」
        self.jumps = []              # [(已充分钟, 增量)]
        self.last_cost = None
        self.unit = self.block_unit  # 0 = 还没识别（按实时计费处理）
        self.block_cost = None

    def _rate(self):
        """窗口内 (SOC %/min, 金额 元/min, 覆盖分钟)；样本不足返回 None"""
        if len(self.hist) < 2:
            return None
        t0, s0, c0 = self.hist[0]
        t1, s1, c1 = self.hist[-1]
        span = t1 - t0
        if span < 5:
            return None
        return (s1 - s0) / span, (c1 - c0) / span, span

    def _detect_block(self):
        """从 cost 的跳变间隔认出「整段计费」：30 或 60 分钟跳一次、金额相近。

        实时计费的桩每 2~5 分钟就跳一小笔，间隔绝不会落在 30/60 附近，所以不会误判。
        """
        if self.block_unit or len(self.jumps) < 2:
            return
        (t1, d1), (t2, d2) = self.jumps[-2], self.jumps[-1]
        gap = t2 - t1
        for u in (30, 60):
            if abs(gap - u) <= 3 and d1 > 0 and abs(d1 - d2) <= max(0.25, 0.25 * d1):
                self.unit = u
                self.block_cost = round((d1 + d2) / 2, 2)
                return

    def feed(self, snap: dict, batt: dict | None = None) -> dict | None:
        if snap.get('orderNum') != self.order:
            self.reset(snap.get('orderNum'))
        if not batt or batt.get('soc') is None:
            return None
        soc = float(batt['soc'])
        mins = (snap.get('elapsed') or 0) / 60.0
        cost = float(snap.get('cost') or 0)
        if self.last_cost is not None and cost > self.last_cost + 0.005:
            self.jumps.append((mins, round(cost - self.last_cost, 2)))
            self._detect_block()
        self.last_cost = cost

        self.hist.append((mins, soc, cost))
        while self.hist and mins - self.hist[0][0] > self.window_min:
            self.hist.popleft()

        r = self._rate()
        if mins < self.min_minutes or not r:
            return None
        soc_rate, cost_rate, span = r

        # 到 100% 还要多久（SOC 在满电附近会跳变，外推必须用它截断，否则会算出 +40% 这种鬼话）
        need = ((100.0 - soc) / soc_rate) if soc_rate > 0.01 else None

        if self.unit:
            # 整段计费：段内已经付过钱，继续充是「免费」的；只有临近边界才值得算跨段账
            left = self.unit - (mins % self.unit)
            if need is not None and need <= left:
                # 本段内就能充满，继续（这段钱已经花了）
                self.streak = 0
                self.last_info = {'soc': soc, 'gain': round(100 - soc, 2), 'spend': 0.0,
                                  'per_pct': 0.0, 'soc_rate': round(soc_rate, 3),
                                  'window': round(span, 1), 'lookahead': round(left, 1),
                                  'mode': 'block', 'unit': self.unit,
                                  'left': round(left, 1)}
                return None
            gain = 100.0 - soc
            spend = self.block_cost if self.block_cost is not None \
                else max(0.0, cost_rate) * self.unit
            window = left
        else:
            left = None
            window = min(self.lookahead, need) if need is not None else self.lookahead
            gain = max(0.0, soc_rate) * window
            spend = max(0.0, cost_rate) * window

        per_pct = (spend / gain) if gain > 0.05 else None
        info = {'soc': soc, 'gain': round(gain, 2), 'spend': round(spend, 2),
                'per_pct': (round(per_pct, 3) if per_pct is not None else None),
                'soc_rate': round(soc_rate, 3), 'window': round(span, 1),
                'lookahead': window, 'mode': ('block' if self.unit else 'live'),
                'unit': self.unit, 'left': (round(left, 1) if left is not None else None)}
        self.last_info = info

        if soc >= 100:
            self.streak = 0
            return None
        # 整段计费且离边界还远：段内继续充不额外花钱，先不动
        if self.unit and left > self.pre_stop:
            self.streak = 0
            return None
        if soc < self.min_soc:
            self.streak = 0
            return None
        bad = gain < self.min_gain or (per_pct is not None and per_pct > self.max_per_pct)
        if not bad:
            self.streak = 0
            return None

        self.streak += 1
        if self.streak < self.confirm:
            return None

        if self.unit:
            head = (f'离 {self.unit} 分钟计费点还剩 {left:.0f} 分钟，跨过去要再付约 ¥{spend:.2f}，'
                    f'但按近 {span:.0f} 分钟的速度这一段只能再充 {gain:.1f}%')
        else:
            head = (f'按近 {span:.0f} 分钟的速度，剩下的 {gain:.1f}% 还要 {window:.0f} 分钟'
                    f'（约 ¥{spend:.2f}')
        if per_pct is not None:
            head += f'，{per_pct:.2f} 元/%'
        if not self.unit:
            head += '）'
        return dict(info, source='价值判停',
                    reason=head + f'；当前已经 {soc:.0f}%，不值')


def full_text(snap: dict, verdict: dict) -> str:
    bits = []
    if snap.get('has_degree') and snap.get('degree'):
        bits.append(f"充入 {snap['degree']:.2f} 度")
    bits.append(f"¥{(snap.get('cost') or 0):.2f}")
    bits.append(fmt_dur(snap.get('elapsed')))
    tail = f"，本次 {' / '.join(bits)}" if bits else ''
    soc = verdict.get('soc')
    head = (f"【电池已停止充电·主动结束桩】{snap.get('station')} 插口 {snap.get('plugNum')}"
            + (f"，SOC {soc}%" if soc is not None else ''))
    return f"{head}。判定依据（{verdict.get('source')}）：{verdict['reason']}{tail}"


# ────────────────────────────── 辅助 ──────────────────────────────

def load_json(path: str) -> dict:
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def token_info(token: str) -> str:
    """解 JWT 看有效期（只是给自己看，不验签）"""
    try:
        p = token.split('.')[1]
        p += '=' * (-len(p) % 4)
        exp = json.loads(base64.urlsafe_b64decode(p)).get('exp')
        if not exp:
            return '未知'
        left = exp - time.time()
        when = time.strftime('%Y-%m-%d', time.localtime(exp))
        return f'至 {when}（剩 {left / 86400:.0f} 天）' if left > 0 else f'已于 {when} 过期'
    except Exception:
        return '未知'


def haha_state(session_path: str = paths.config('session.json'), force: bool = True) -> dict | None:
    """读哈啰电池端状态 —— 这是「该不该停」的权威来源。

    返回 {'soc','status','current','volt','batt_ts','age','batteryNo'}，拿不到返回 None。
    force=True 会像小程序「刷新」那样先打一发查询指令，尽量拿到新鲜值。
    """
    try:
        from evwatch.battery_watch import snapshot
        s = snapshot(load_json(session_path), detail=True, force=force)
    except Exception:
        return None
    rt = s.get('reportTime')
    ts = None
    try:
        v = int(rt)
        ts = v / 1000 if v > 1e11 else v        # 毫秒戳 / 秒戳都可能
    except (TypeError, ValueError):
        ts = None
    return {
        'soc': s.get('soc'),
        'status': s.get('chargeStatus'),
        'current': s.get('current'),
        'volt': s.get('volt'),
        'batt_ts': ts,
        'age': (time.time() - ts) if ts else None,
        'batteryNo': s.get('batteryNo'),
    }


def push(session: dict, text: str, args) -> None:
    if getattr(args, 'no_push', False):
        return
    hook = args.dingtalk or session.get('dingtalk_webhook')
    if hook:
        try:
            dingtalk(text, webhook=hook, secret=args.dingtalk_secret or session.get('dingtalk_secret'))
            print('   钉钉已推送')
        except Exception as e:
            print('   钉钉推送失败:', e)
    if args.webhook:
        try:
            _urlopen(urllib.request.Request(
                args.webhook,
                data=json.dumps({'msgtype': 'text', 'text': {'content': text}}).encode(),
                headers={'Content-Type': 'application/json'}), timeout=10)
            print('   企业微信已推送')
        except Exception as e:
            print('   企业微信推送失败:', e)


def load_state(path: str) -> dict:
    try:
        return load_json(path)
    except Exception:
        return {'ended': []}


def save_state(path: str, st: dict) -> None:
    try:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(st, fh, ensure_ascii=False)
    except Exception:
        pass


# ────────────────────────────── 子命令 ──────────────────────────────

def cmd_status(tower: Tower, session: dict) -> int:
    print(f"登录态: JWT {token_info(tower.token)}")
    try:
        snap = snap_from_detail(tower.charging())
    except TowerError as e:
        print(f'查询失败: {e}')
        if e.code in (KICKED,) + UNAUTHORIZED:
            print('  → 铁塔小程序单点登录：手机上再进一次小程序就会把这份挤掉，重新抓一次 Authorization。')
        return 1
    except Exception as e:
        print(f'连不上铁塔接口: {type(e).__name__} {e}')
        print(f'  [net] 尝试过：{net_describe()}')
        return 1
    if not snap:
        print('当前没有进行中的充电。')
        return 0
    print(f"充电中: {snap['station']}  {snap_text(snap)}")
    return 0


def cmd_list(tower: Tower) -> int:
    orders = tower.charging_orders()
    if not orders:
        print('没有进行中的充电订单。')
        return 0
    for o in orders:
        print('进行中:', json.dumps({k: o.get(k) for k in
                                     ('orderNum', 'plugNum', 'stationName', 'startTime',
                                      'endTime', 'state', 'paymentMoney')}, ensure_ascii=False))
    o = orders[0]
    try:
        d = tower.order_detail(o['orderNum'], '1', o.get('paymentMoney') or 0)
        r = (d or {}).get('record') or {}
    except Exception:
        r = {}
    if r:
        print('本单详情:', json.dumps({k: r.get(k) for k in
                                       ('orderNum', 'startTime', 'endTime', 'endType',
                                        'costAllMoney', 'consumption', 'chargeTime')},
                                      ensure_ascii=False))
    return 0


def cmd_stop(tower: Tower, args) -> int:
    snap = snap_from_detail(tower.charging())
    if not snap:
        print('没有进行中的充电，无需结束。')
        return 0
    if args.order and args.order != snap['orderNum']:
        print(f"当前订单是 {snap['orderNum']}，不是 {args.order}")
        return 2
    print(f"结束充电: {snap['station']} {snap_text(snap)}")
    if args.dry_run:
        print('--dry-run：不真的发结束请求')
        return 0
    res = tower.end_charge(snap['plugId'], snap['orderNum'], open_door=not args.no_open_door)
    print(f'接口返回: {json.dumps(res, ensure_ascii=False)}')
    return 0 if _confirm_ended(tower, snap) else 1


def cmd_start(tower: Tower, session: dict, args) -> int:
    """开始充电。--full 用客户端原生的「充满自停」（chargeType=0）。"""
    if not args.plug:
        print('需要 --plug <插口号>（扫桩上二维码能看到，形如 HBLF2000xxxx）')
        return 2
    info = tower.scan(args.plug)
    if not info:
        print('扫码接口返回空，插口号对不上或桩离线。')
        return 1
    plug_id = info.get('plugId')
    balance = _f(info.get('balance'))
    station = info.get('stationName') or ''

    cfg = {}
    sid = info.get('plugSid') or info.get('stationId')
    if sid:
        try:
            cfg = tower.station_config(sid)
        except TowerError as e:
            print(f'  读站点计费配置失败（不挡开充）：{e}')
    full_money = _f(cfg.get('fullStopMoney')) or 5.0
    start_money = _f(cfg.get('startChargingMoney')) or 2.0

    pay = args.money
    if pay is None:
        pay = full_money if args.full else start_money
    if balance is not None:
        pay = min(pay, balance)

    payload = start_payload(plug_id, full=args.full, hours=args.hours, pay_money=pay,
                            payment_method=args.payment_method, pay_type=args.pay_type,
                            site_type=args.site_type)

    print(f"站点   : {station}（{info.get('stationAddress') or '—'}）")
    print(f"插口   : {args.plug} → plugId={plug_id}")
    print(f"余额   : ¥{balance:.2f}" if balance is not None else '余额   : —')
    print(f"模式   : {'充满自停（chargeType=0，桩自己判满，多退少补）' if args.full else f'按时长 {args.hours} 小时（chargeType=5）'}")
    print(f"预扣   : ¥{pay:.2f}（站点充满自停预扣 ¥{full_money:.2f} / 起充 ¥{start_money:.2f}）")
    print(f"报文   : {json.dumps(payload, ensure_ascii=False)}")

    if balance is not None and balance < start_money:
        print(f"⚠️ 余额 ¥{balance:.2f} 低于起充金额 ¥{start_money:.2f}，服务端大概率拒绝")
    if not args.yes:
        print('\n（没加 --yes，只打印报文，没有真的开充）')
        return 0

    res = tower.start_charge(payload)
    print(f"开充成功: {json.dumps(res, ensure_ascii=False)}")
    order = (res or {}).get('orderNum')
    if order:
        print(f'订单号 {order}；可以另开一个终端跑 `tower.py watch` 盯着')
    return 0


def _confirm_ended(tower: Tower, snap: dict, tries: int = 4, gap: float = 2.5) -> bool:
    """停完之后确认真的停了。

    两种「已结束」都算成功：① charging/list 里不再是这一单；② 还是这一单但
    `endType` 已经有值（2 = 接口/人工结束）—— 桩侧结算有延迟，别因此误报「仍在充电」。
    """
    for _ in range(tries):
        time.sleep(gap)
        try:
            now = snap_from_detail(tower.charging())
        except TowerError:
            return False
        if not now or now.get('orderNum') != snap.get('orderNum'):
            return True
        if now.get('endType') is not None:
            return True
    return False


def relogin_capability(session: dict, args) -> tuple[bool, str | None, str | None]:
    """303 时能不能自动走短信兜底：(可用?, 手机号, open_id)。

    注意：`use-capture` 建出来的 session 可能只有 authorization/device_id，没有 open_id，
    这里会去抓包里找一个（同一用户同一小程序恒定），所以照样能兜底。
    """
    if getattr(args, 'no_relogin', False):
        return False, None, None
    mobile = getattr(args, 'relogin_mobile', None) or session.get('mobile')
    oid = session.get('open_id')
    need_probe = not mobile or not oid
    if need_probe:
        try:
            from evwatch.tower_login import detect_mobile, detect_open_id
            if not oid:
                oid = detect_open_id()
            if not mobile:
                mobile = detect_mobile()
        except Exception:
            pass
    has_src = bool(getattr(args, 'code_file', None) or getattr(args, 'code_dir', None)
                   or getattr(args, 'code_cmd', None) or os.path.isdir(ICLOUD_DIR))
    return bool(mobile and oid and has_src), mobile, oid


def inbox_token(path: str = TOKEN_INBOX) -> tuple[str | None, str | None, float]:
    """读 iCloud 上的 token 收件箱（iPhone 抓包后丢这里）。返回 (token, deviceId, mtime)"""
    try:
        from evwatch.tower_login import read_token_inbox
        return read_token_inbox(path)
    except Exception:
        return None, None, 0.0


def save_token(session_path: str, token: str, device_id: str | None = None,
               source: str = 'icloud') -> None:
    """只改 authorization / device_id，其余字段原样保留"""
    try:
        d = load_json(session_path)
    except Exception:
        d = {}
    d.update({'authorization': token, 'source': source,
              'obtained_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    if device_id:
        d['device_id'] = device_id
    try:
        with open(session_path, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f'   写 session 失败：{e}')


def cmd_watch(tower: Tower, session: dict, args) -> int:
    det = StopDecider(confirm=args.confirm, warmup_min=args.warmup_min,
                      full_current=args.full_current, min_current=args.min_current,
                      peak_ratio=args.peak_ratio, active_current=args.active_current,
                      pile_fallback=not args.pile_only,
                      target_soc=args.target_soc, cold_soc=args.cold_soc)
    vs = (ValueStop(min_soc=args.min_soc, min_gain=args.min_gain,
                    max_per_pct=args.max_per_pct, lookahead_min=args.lookahead_min,
                    confirm=args.smart_confirm, min_minutes=args.smart_min_minutes,
                    block_unit=args.billing_unit, pre_stop=args.pre_stop)
          if args.smart_stop else None)
    state = load_state(args.state)
    ended = set(state.get('ended') or [])
    announced = state.get('announced')          # 上次已推送过「开始充电」的单号
    cur_order = None
    batt, batt_at = None, 0.0
    last_batt_key = None
    warned = False
    verdict_logged = set()
    last_line = None
    last_val_key = None
    last_relogin = 0.0
    tok_checked = 0.0
    bad_tok = None

    new = not os.path.exists(args.csv)
    fh = open(args.csv, 'a', newline='', encoding='utf-8')
    w = csv.writer(fh)
    if new:
        w.writerow(['ts', 'orderNum', 'plugNum', 'pile_current', 'voltage', 'degree', 'cost',
                    'elapsed_s', 'soc', 'batt_status', 'batt_current', 'verdict'])

    print(f"===== {time.strftime('%Y-%m-%d %H:%M:%S')} 铁塔 · 电池端说了算 启动 =====")
    print(f"登录态 : 铁塔 JWT {token_info(tower.token)}")
    if args.pile_only:
        print(f"判据   : 仅桩侧（--pile-only）：电流 < max({args.min_current}A, 峰值×{args.peak_ratio}) "
              f"连续 {args.confirm} 次")
    else:
        print(f"判据   : 电池端优先 —— chargeStatus 1→0/-1 连续 {args.confirm} 次，"
              f"或 SOC 100% 且电池电流≤{args.full_current}A")
        print(f"         桩侧仅作兜底（--pile-only 可关掉电池端 / --no-pile-fallback 可关掉兜底）")
    print(f"保温   : 开充后 {args.warmup_min} 分钟内不判；必须先见到「充电中」")
    if args.target_soc:
        print(f"目标   : 充到 {args.target_soc:g}% 就停（--target-soc）")
    print(f"冷启   : 重启/断网恢复后，若电池已停充且电量 ≥{args.cold_soc:g}% 也判停")
    if vs:
        unit = args.billing_unit
        mode = (f'整段 {unit:g} 分钟' if unit else '自动识别（实时计费 / 30 / 60 分钟整段）')
        print(f"止损   : 价值判停开 —— SOC≥{args.min_soc:g}% 后不划算就停；"
              f"计费形态 {mode}，临近边界 {args.pre_stop:g} 分钟内才判跨段")
    else:
        print("止损   : 关闭（价值判停被 --no-smart-stop 关掉了）")
    print(f"动作   : {'仅判定（dry-run）' if args.dry_run else '真实调用 endCharge'}；"
          f"开门 {'开' if not args.no_open_door else '关'}")
    print(f"轮询   : 桩 {args.interval}s / 电池 {args.battery_interval}s / 空闲 {args.idle_interval}s")
    has_hook = bool(args.dingtalk or session.get('dingtalk_webhook') or args.webhook)
    print(f"推送   : {'钉钉/企业微信' if has_hook else '未配置（只写日志和 CSV）'}")
    # 登录态策略：抓包 token 优先（一次管半年），被挤下线时才走短信兜底
    relogin_ready, relogin_mobile, _oid = relogin_capability(session, args)
    print(f"续期   : " + (f'短信兜底开（{redact.phone(relogin_mobile)}，303 时自动发码）'
                          if relogin_ready else '关闭（303 只告警，重抓或手动 relogin）'))
    print(f"token  : " + (f'iCloud 收件箱热更新开（{os.path.basename(args.token_inbox)}，'
                          f'每 {args.token_check}s 看一次）'
                          if not args.no_token_watch else '关闭（--no-token-watch）'))
    print(f"CSV    : {os.path.abspath(args.csv)}")
    if not args.no_open_door:
        print('提示   : 结束时会开门（openDoor=1），人不在现场也没关系，开门本身不收费')

    while True:
        try:
            # iPhone 抓包 → iCloud 收件箱 → 这里自动热替换（先验证，验不过就继续用旧的）
            if not args.no_token_watch and time.time() - tok_checked >= args.token_check:
                tok_checked = time.time()
                newtok, newdev, mt = inbox_token(args.token_inbox)
                if newtok and newtok != tower.token and newtok != bad_tok:
                    try:
                        st = os.path.getmtime(args.session)
                    except OSError:
                        st = 0.0
                    if mt > st:      # 文件比 session 新才算「用户刚更新过」
                        use_dev = newdev or tower.device_id
                        try:
                            Tower(dict(session, authorization=newtok,
                                       device_id=use_dev), timeout=10).user_info()
                        except Exception as e:
                            bad_tok = newtok      # 同一个坏 token 只提示一次
                            hint = '（token 与 deviceId 可能不配对，抓整段 cURL 最稳）' \
                                if '303' in str(e) else ''
                            print(f"{time.strftime('%H:%M:%S')}  iCloud 上的新 token 验证失败"
                                  f"（{e}）{hint}，继续用旧的")
                        else:
                            tower.token = newtok
                            tower.device_id = use_dev
                            session['authorization'] = newtok
                            session['device_id'] = use_dev
                            save_token(args.session, newtok, use_dev)
                            bad_tok = None
                            print(f"{time.strftime('%H:%M:%S')}  ✔ token 已从 iCloud 热更新："
                                  f'JWT {token_info(newtok)}')
                            push(session, f'【电池监控】铁塔 token 已从 iCloud 热更新（JWT {token_info(newtok)}）',
                                 args)

            snap = snap_from_detail(tower.charging())

            if not snap:
                if cur_order:
                    print(f"{time.strftime('%H:%M:%S')}  本单已结束（到时自停 / 充满自停 / 人工结束 之一）")
                    push(session, f'【电池充电已结束】铁塔插口 {cur_order} 的充电已结束'
                                  f'（到时自停、充满自停或人工结束，不是本程序触发的）', args)
                    cur_order = None
                    det.reset()
                    if vs:
                        vs.reset()
                time.sleep(args.idle_interval)
                continue

            if snap['orderNum'] != cur_order:
                cur_order = snap['orderNum']
                det.reset(cur_order)
                if vs:
                    vs.reset(cur_order)
                warned = False
                if cur_order in ended:
                    # 已经结束过的单还在返回（结算/上报有延迟）：别再重复结束、也别再推「开始充电」
                    print(f"{time.strftime('%H:%M:%S')}  {cur_order} 已结束过，等桩侧结算完成…")
                    time.sleep(args.idle_interval)
                    continue
                mode = (f"充满自停（预扣 ¥{snap.get('prepaid') or 0:.2f}，桩自己判通常偏晚）"
                        if snap.get('chargeType') == CHARGE_TYPE_FULL
                        else f"按时长（预扣 ¥{snap.get('prepaid') or 0:.2f}）")
                print(f"{time.strftime('%H:%M:%S')}  发现充电 {snap['station']} {snap_text(snap)} · {mode}")
                if not snap.get('read_current'):
                    print('   桩只上报功率（isReadCurrent=false）：电流是 power/220 的换算值，'
                          '小程序也会隐藏「实时电流」那行；判停仍以电池端为准')
                if not snap.get('has_degree'):
                    print('   桩未上报充电度数（isDisplayDegree=false）：度数恒 0，'
                          '结算以桩侧账单为准')
                if cur_order == announced:
                    # 服务重启过：这一单的「开始充电」早就推过了，别重复打扰
                    print('   （本单的开始推送已发过，重启后不重复推）')
                else:
                    push(session, f"【电池开始充电·铁塔】{snap['station']} 插口 {snap['plugNum']}，{mode}", args)
                    announced = cur_order
                    state['announced'] = announced
                    save_state(args.state, state)

            # 电池端（哈啰 BMS）：真正的判据来源
            if not args.pile_only and time.time() - batt_at >= args.battery_interval:
                batt_at = time.time()
                batt = haha_state(args.haha_session)
                if batt is None:
                    if last_batt_key != 'fail':
                        last_batt_key = 'fail'
                        print(f"{time.strftime('%H:%M:%S')}  · 读不到哈啰电池状态（{args.haha_session} 过期？），"
                              f"先靠桩侧兜底")
                else:
                    key = (batt.get('soc'), batt.get('status'), batt.get('batt_ts'))
                    age = batt.get('age')
                    if key != last_batt_key:
                        if last_batt_key == 'fail':
                            print(f"{time.strftime('%H:%M:%S')}  · 哈啰电池状态恢复读取")
                        last_batt_key = key
                        print(f"{time.strftime('%H:%M:%S')}  · 电池 SOC {batt.get('soc')}% / "
                              f"{charge_label(batt.get('status'))}（chargeStatus={batt.get('status')}）"
                              f" / 电流 {batt.get('current')}A"
                              + (f"（报告 {int(age // 60)} 分钟前）" if age else ''))

            verdict = None if snap['orderNum'] in ended else det.feed(snap, batt)
            # 价值判据：始终喂样本（保持滑动窗口），只有电池判据没结论时才采纳
            vstop = vs.feed(snap, batt) if vs else None
            if verdict is None and vstop is not None:
                verdict = vstop
            if vs and vs.last_info:
                info = vs.last_info
                key = (info['mode'], int(info['gain']), int((info.get('left') or 0) // 5))
                if key != last_val_key:
                    last_val_key = key
                    per = (f"，{info['per_pct']:.2f} 元/%"
                           if info.get('per_pct') is not None else '')
                    if info['mode'] == 'block':
                        print(f"{time.strftime('%H:%M:%S')}  · 估值：整段计费（{info['unit']}分钟），"
                              f"离边界 {info['left']:.0f} 分钟；跨段约 ¥{info['spend']:.2f}"
                              f" 换 +{info['gain']:.1f}%{per}（SOC {info['soc']:.0f}%）")
                    else:
                        print(f"{time.strftime('%H:%M:%S')}  · 估值：接下来 {info['lookahead']:.0f} 分钟"
                              f"预计 +{info['gain']:.1f}%（约 ¥{info['spend']:.2f}{per}，"
                              f"SOC {info['soc']:.0f}%）")
            w.writerow([int(time.time()), snap['orderNum'], snap['plugNum'], snap.get('current'),
                        snap.get('voltage'),
                        snap.get('degree') if snap.get('has_degree') else '',   # 桩不上报就留空
                        snap.get('cost'),
                        int(snap.get('elapsed') or 0),
                        (batt or {}).get('soc'), (batt or {}).get('status'),
                        (batt or {}).get('current'), (verdict or {}).get('reason', '')])
            fh.flush()

            # 桩只上报功率、不直接上报电流时（isReadCurrent=false），别把换算值当实测
            if snap.get('power'):
                head = f"桩 {snap['power'] / 1000:.2f}kW"
            elif snap.get('read_current'):
                head = f"{snap.get('current')}A/{snap.get('voltage')}V"
            else:
                head = '桩无读数'
            deg = f"{(snap.get('degree') or 0):.2f}度 " if snap.get('has_degree') else ''
            line = (f"{head} {deg}¥{(snap.get('cost') or 0):.2f} "
                    f"{fmt_dur(snap.get('elapsed'))}"
                    + (f" | 电池 {batt.get('soc')}%（{charge_label(batt.get('status'))}）" if batt else ''))
            if line != last_line:
                print(f"{time.strftime('%H:%M:%S')}  {line}")
                last_line = line

            if not warned and det.warn_no_current((snap.get('elapsed') or 0) / 60):
                warned = True
                print(f"{time.strftime('%H:%M:%S')}  ⚠️ 既没电池数据、也没看到桩侧电流，判不了；"
                      f"先确认 session.json 是不是过期了（哈啰 code=103）")

            if verdict:
                if snap['orderNum'] in verdict_logged and args.dry_run:
                    time.sleep(args.interval)
                    continue
                verdict_logged.add(snap['orderNum'])
                print(f"{time.strftime('%H:%M:%S')}  ✔ 该停了（{verdict['source']}）：{verdict['reason']}")
                text = full_text(snap, verdict)
                if args.dry_run:
                    print(f'   [dry-run] 本该结束充电并推送：{text}')
                    time.sleep(args.interval)
                    continue
                try:
                    res = tower.end_charge(snap['plugId'], snap['orderNum'],
                                           open_door=not args.no_open_door)
                    print(f'   endCharge 返回: {json.dumps(res, ensure_ascii=False)}')
                except TowerError as e:
                    print(f'   结束失败: {e}')
                    push(session, f'【电池自动结束失败】{snap.get("station")} 插口 {snap.get("plugNum")} '
                                  f'调用 endCharge 失败：{e}（桩还在计费，尽快手动停）', args)
                    time.sleep(args.interval)
                    continue
                ok = _confirm_ended(tower, snap) if args.verify else True
                print('   已确认停止充电' if ok else '   ⚠️ 接口成功但仍在充电中，请人工确认')
                push(session, text + ('' if ok else '（但复查仍在充电，请人工确认）'), args)
                ended.add(snap['orderNum'])
                state['ended'] = sorted(ended)[-50:]
                save_state(args.state, state)
                cur_order = None
                det.reset()
                if vs:
                    vs.reset()
                time.sleep(args.idle_interval)
                continue

            time.sleep(args.interval)

        except TowerError as e:
            if e.code in (KICKED,) + UNAUTHORIZED:
                print(f"{time.strftime('%H:%M:%S')}  登录态失效（{e}）")
                # 配了取码来源就自动短信续期（半小时最多一次，别刷短信）
                if relogin_ready and time.time() - last_relogin > 1800:
                    last_relogin = time.time()
                    try:
                        from evwatch.tower_login import auto_relogin
                        args.relogin_mobile = relogin_mobile
                        tok = auto_relogin(args, args.session)
                        if tok:
                            tower.token = tok
                            continue
                    except Exception as ex:
                        print(f'   自动续期失败：{ex}')
                print('   → 抓包 token 优先、短信兜底：先换一份新抓包（tower_login.py use-capture），'
                      '或跑 `tower_login.py relogin --mobile 手机号`')
                push(session, f'【电池自动结束中断】铁塔登录态失效（{e}），'
                              f'需重新抓 Authorization 或跑 tower_login.py relogin', args)
                time.sleep(900)
                continue
            print(f"{time.strftime('%H:%M:%S')}  接口报错: {e}")
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print('\n已停止')
            return 0
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')}  失败: {e}")
            print(f"   [net] 尝试过：{net_describe()}")
            time.sleep(args.interval)


def cmd_sim(args) -> int:
    """离线演一遍：桩还在按小时跑，但电池端已经停了 → 立刻结束桩。

    场景来自实测：电池 chargeStatus 从 1 变 0（充满或 BMS 主动停），
    桩侧电流却还是 5A / 或者桩只是把电流显示成旧的，桩照样计时收服务费。
    """
    det = StopDecider(confirm=args.confirm, warmup_min=args.warmup_min,
                      full_current=args.full_current, pile_fallback=not args.pile_only)
    rows = [
        # (分钟, 桩电流, 电池SOC, 电池chargeStatus, 电池电流, 是否是新报告)
        (2, 0.0, 92, '0', 0.0, True),      # 刚插上：电池还没进入充电
        (4, 4.9, 93, '1', 5.0, True),      # 开始充
        (10, 5.0, 95, '1', 5.0, True),
        (20, 5.0, 97, '1', 5.0, True),
        (30, 4.6, 99, '1', 4.6, True),
        (40, 4.8, 100, '0', 0.0, True),    # ★ 电池 BMS 报 100% 且停充，但桩仍在跑
        (45, 4.8, 100, '0', 0.0, False),   # 同一份缓存，不重复计数
        (50, 4.9, 100, '0', 0.0, True),    # 新报告：连续 2 次 → 该停了
    ]
    verdict = None
    ts = 1000
    for mins, pcur, soc, st, bcur, fresh in rows:
        if fresh:
            ts += 60
        pile = {'orderNum': 'DEMO', 'plugNum': 'HBLF00000000', 'station': '演示站点',
                'elapsed': mins * 60.0, 'current': pcur, 'voltage': 220.0,
                'degree': round(mins * 0.05, 2), 'cost': round(mins * 0.02, 2), 'prepaid': 1.0,
                'read_current': True, 'has_degree': True}
        batt = {'soc': soc, 'status': st, 'current': bcur, 'batt_ts': ts, 'age': 60}
        v = det.feed(pile, batt)
        mark = ''
        if v:
            verdict = v
            mark = f"  ← 判定该停（{v['source']}）：{v['reason']}"
        print(f"  +{mins:3d}分  桩{pcur:4.1f}A  电池 {soc}%/{st}/{bcur}A{mark}")
        if v:
            break
    if not verdict:
        print('  （演练没触发，检查参数）')
        return 1
    print(f"\n会推送：{full_text(pile, verdict)}")
    return 0


# ────────────────────────────── main ──────────────────────────────

def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser(description='铁塔充电：以电池端为准，电池停了就结束桩')
    ap.add_argument('cmd', choices=['status', 'watch', 'start', 'stop', 'list', 'sim'])
    ap.add_argument('--session', default=paths.config('tower_session.json'), help='铁塔登录态文件')
    ap.add_argument('--haha-session', default=paths.config('session.json'), help='哈啰登录态文件（电池端的权威来源）')
    ap.add_argument('--pile-only', action='store_true', help='不读电池，只靠桩侧电流（兜底模式）')
    ap.add_argument('--no-pile-fallback', dest='pile_fallback', action='store_false',
                    help='不启用桩侧兜底，只认电池端')
    ap.add_argument('--battery-interval', type=int, default=60, help='多久读一次电池状态（秒）')
    ap.add_argument('--interval', type=int, default=60, help='桩侧轮询间隔（秒）')
    ap.add_argument('--idle-interval', type=int, default=120, help='空闲时的轮询间隔（秒）')
    ap.add_argument('--full-current', type=float, default=0.5,
                    help='SOC 100%% 时电池电流低于这个值算「已停充」（A，默认 0.5=浮充）')
    ap.add_argument('--min-current', type=float, default=0.3, help='桩侧兜底：低于这个算没在充（A）')
    ap.add_argument('--active-current', type=float, default=1.0,
                    help='桩侧兜底：必须先见到这个电流（A）')
    ap.add_argument('--confirm', type=int, default=2, help='连续多少次才认定「电池停了」')
    ap.add_argument('--target-soc', type=float, default=0.0,
                    help='充到该 SOC 就停（0=不启用，默认充满为止）。够用就停时用，例如 90')
    ap.add_argument('--cold-soc', type=float, default=98.0,
                    help='冷启动兜底：没见过「充电中」但电量已达该值且已停充，也判停（默认 98）')
    ap.add_argument('--warmup-min', type=float, default=3, help='开充后多久内不判（分钟）')
    ap.add_argument('--peak-ratio', type=float, default=0.25, help='桩侧兜底：跌到峰值的多少算满')
    # —— 价值判停：再占一段不划算就提前收 ——
    ap.add_argument('--no-smart-stop', dest='smart_stop', action='store_false',
                    help='关掉「价值判停」（默认开：SOC 够高且接下来一小时不划算就提前结束）')
    ap.add_argument('--min-soc', type=float, default=95.0,
                    help='价值判停的 SOC 下限，低于此值绝不提前停（默认 95）')
    ap.add_argument('--min-gain', type=float, default=2.0,
                    help='本段剩余时间内预计涨幅低于这个点数就不值得再占一段（默认 2）')
    ap.add_argument('--billing-unit', type=int, default=0,
                    help='计费整段长度：30 / 60；0=自动识别（默认）。识别不到按实时计费处理')
    ap.add_argument('--pre-stop', type=float, default=5.0,
                    help='整段计费时，离边界还剩几分钟内才判「要不要跨段」（默认 5）')
    ap.add_argument('--lookahead-min', type=float, default=60.0,
                    help='往后看多久来估值（分钟，默认 60 = 按小时计费的一个单元）')
    ap.add_argument('--max-per-pct', type=float, default=0.15,
                    help='每 1%% SOC 花超过这个钱就不值（元/%%，默认 0.15，中段约 0.05）')
    ap.add_argument('--smart-confirm', type=int, default=2, help='价值判停连续几次成立才停（默认 2）')
    ap.add_argument('--smart-min-minutes', type=float, default=30.0,
                    help='至少充多少分钟才允许价值判停（默认 30）')
    ap.add_argument('--no-open-door', action='store_true', help='结束时不开仓门')
    ap.add_argument('--no-verify', dest='verify', action='store_false', help='结束后不复查')
    ap.add_argument('--dry-run', action='store_true', help='只判定，不真的结束')
    ap.add_argument('--order', help='stop 时指定订单号（防止停错单）')
    ap.add_argument('--plug', help='start 时必填：插口号（扫码可见，形如 HBLF2000xxxx / DTMFxxxxxxx）')
    ap.add_argument('--full', action='store_true',
                    help='start 用客户端原生的「充满自停」（chargeType=0）；'
                         '注意桩判得很晚、会多吃服务费，通常不如让 watch 盯电池端')
    ap.add_argument('--hours', type=int, default=2, help='按时长开充的小时数，默认 2')
    ap.add_argument('--money', type=float, help='预扣金额（默认取站点配置：充满自停 fullStopMoney / 时长 startChargingMoney）')
    ap.add_argument('--payment-method', type=int, default=1, help='paymentMethod，默认 1；抓包里那单是 0')
    ap.add_argument('--pay-type', type=int, default=2, help='payType，默认 2（余额）')
    ap.add_argument('--site-type', type=int, default=1, help='站点 type（客户端 7→2，其余→1）')
    ap.add_argument('--yes', action='store_true', help='start 时不加就只打印报文，不真的开充')
    ap.add_argument('--state', default=paths.data('.tower_state.json'))
    ap.add_argument('--csv', default=paths.data('tower_charge.csv'))
    ap.add_argument('--dingtalk', help='钉钉机器人 webhook')
    ap.add_argument('--dingtalk-secret', help='钉钉加签密钥')
    ap.add_argument('--webhook', help='企业微信机器人地址')
    ap.add_argument('--no-push', action='store_true', help='不发任何推送（调试/演练用）')
    ap.add_argument('--no-relogin', action='store_true',
                    help='303 时不做短信兜底（默认：能取到码就自动续期）')
    ap.add_argument('--token-inbox', default=TOKEN_INBOX,
                    help='iCloud 上的 token 收件箱（iPhone 更新用），默认 tower_token.txt')
    ap.add_argument('--no-token-watch', action='store_true', help='不监听 iCloud token 收件箱')
    ap.add_argument('--token-check', type=int, default=120, help='多久看一次收件箱（秒）')
    ap.add_argument('--relogin-mobile', help='短信续期用的手机号（默认取 session 里的 mobile）')
    ap.add_argument('--code-file', help='验证码落地文件')
    ap.add_argument('--code-dir', help='验证码目录（默认 iCloud 转发目录）')
    ap.add_argument('--code-cmd', help='执行命令取验证码')
    ap.add_argument('--code-timeout', type=int, default=180, help='等验证码的超时（秒）')
    args = ap.parse_args()

    if args.cmd == 'sim':
        return cmd_sim(args)

    try:
        session = load_json(args.session)
    except FileNotFoundError:
        print(f'找不到 {args.session}。先：')
        print('  cp tower_session.example.json tower_session.json')
        print('  # 然后填入抓包里的 Authorization（Bearer 后面那串）')
        return 2
    except json.JSONDecodeError as e:
        print(f'{args.session} 不是合法 JSON：{e}')
        return 2

    # 推送通道：铁塔 session 里没配，就沿用哈啰 session.json 里的（同一个钉钉机器人）
    try:
        hs = load_json(args.haha_session)
        for k in ('dingtalk_webhook', 'dingtalk_secret'):
            if not session.get(k) and hs.get(k):
                session[k] = hs[k]
    except Exception:
        pass

    try:
        tower = Tower(session)
    except TowerError as e:
        print(f'登录态有问题：{e}')
        return 2

    try:
        if args.cmd == 'status':
            return cmd_status(tower, session)
        if args.cmd == 'list':
            return cmd_list(tower)
        if args.cmd == 'start':
            return cmd_start(tower, session, args)
        if args.cmd == 'stop':
            return cmd_stop(tower, args)
        return cmd_watch(tower, session, args)
    except TowerError as e:
        print(f'接口报错: {e}')
        return 1
    except KeyboardInterrupt:
        print('\n已停止')
        return 0
    except Exception as e:
        print(f'失败: {type(e).__name__} {e}')
        print(f'  [net] 尝试过：{net_describe()}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
