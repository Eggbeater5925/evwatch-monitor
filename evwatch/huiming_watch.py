"""
huiming_watch.py — 荟铭充电桩监听（第三个数据源，与电池 / 铁塔 / 麦希充并行）

它和 pile_watch.py 的分工一样：只监听「这一单在不在充」，判满就远程结束，
**不做开始充电**（下单要选插座、时长、支付方式，属于人做的决定）。

和麦希充的区别：
  * 荟铭给真实功率 charging_w 和电量 electrics → 判满主要靠功率
  * 状态只有 131 充电中 / 666 已结束，没有结束原因字典
  * token 是 UUID，没有 exp，也没有登录接口 → 只能 settoken 手动换

用法：
  python evwatch/huiming_watch.py ping
  python evwatch/huiming_watch.py status
  python evwatch/huiming_watch.py once
  python evwatch/huiming_watch.py watch --interval-active 60
  python evwatch/huiming_watch.py watch --dry-run      # 只打印不推送、不停
  python evwatch/huiming_watch.py sim                  # 离线演示判满
  python evwatch/huiming_watch.py settoken "<token>"
  python evwatch/huiming_watch.py stop                 # 试运行
  python evwatch/huiming_watch.py stop --confirm       # 真的结束
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch import huiming as hm  # noqa: E402
from evwatch.csvsink import CsvSink  # noqa: E402
from evwatch.net import describe as net_describe  # noqa: E402
from evwatch.notify import push as push_any  # noqa: E402
from evwatch import paths  # noqa: E402
from evwatch import redact  # noqa: E402

CSV_HEADER = ['ts', 'orderId', 'active', 'status', 'power', 'energy',
              'money', 'elapsed', 'socket']

# 服务费档位（抓包实测；正常情况下从订单的 elec_price.power 取）
DEFAULT_TIERS = [
    {'min': '0', 'max': '130', 'price': '0.45'},
    {'min': '130', 'max': '200', 'price': '0.5'},
    {'min': '200', 'max': '300', 'price': '0.55'},
    {'min': '300', 'max': '400', 'price': '0.65'},
    {'min': '400', 'max': '500', 'price': '0.75'},
    {'min': '500', 'max': '600', 'price': '0.85'},
    {'min': '600', 'max': '800', 'price': '1.2'},
    {'min': '800', 'max': '1000', 'price': '1.5'},
]
ELEC_FEE = 0.69


# ────────────────────────── 配置与状态

def load_config(path: str, fallback: str | None = None) -> dict:
    """huiming.json 为主；session.json 只借钉钉配置，不借手机号（这家用不上）"""
    cfg: dict = {}
    if fallback and os.path.exists(fallback):
        try:
            sess = json.load(open(fallback, encoding='utf-8'))
            cfg.update({k: v for k, v in sess.items() if k.startswith('dingtalk')})
        except Exception as e:
            print(f'{fallback} 读不了：{e}')
    if os.path.exists(path):
        try:
            cfg.update(json.load(open(path, encoding='utf-8')))
        except Exception as e:
            print(f'{path} 读不了：{e}')
    return cfg


def save_token(path: str, token: str, obtained_at: str) -> dict:
    """只写 huiming.json 自己的键，别把 session.json 的钉钉配置复制进来"""
    try:
        raw = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    except Exception:
        raw = {}
    raw.setdefault('_说明', '荟铭充电 token：抓包 profile 响应里的 data.userinfo.token')
    raw['token'] = token
    raw['obtained_at'] = obtained_at
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
    return raw


def token_desc(cfg: dict) -> str:
    """UUID token 没有 exp，只能说到手多久"""
    t = cfg.get('obtained_at')
    if not t:
        return '未记录取得时间'
    try:
        t0 = time.mktime(time.strptime(t, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return t
    days = (time.time() - t0) / 86400
    return f'取得于 {t}（已用 {days:.1f} 天；UUID 无 exp，失效只能重新抓）'


class HmState:
    """只记「这一单见过什么」，进程重启不丢判满进度"""

    def __init__(self, path: str | None = None):
        self.path = path
        self.active = False
        self.order_id = None
        self.peak_power = 0.0
        self.quiet_since = None
        self.droop_since = None      # 功率开始往下掉的起点
        self.noload_alerted = False
        self.last_end_id = None
        self.last_soc = None        # 上一轮的 SOC，用来判断跨过哪个里程碑
        self.value_announced = False  # 到评估线的账只推一次
        self.checked_id = None        # 已核对过账单的订单，只推一次
        self.load()

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            d = json.load(open(self.path, encoding='utf-8'))
            self.active = bool(d.get('active'))
            self.order_id = d.get('order_id')
            self.peak_power = float(d.get('peak_power') or 0)
            self.quiet_since = d.get('quiet_since')
            self.droop_since = d.get('droop_since')
            self.noload_alerted = bool(d.get('noload_alerted'))
            self.last_end_id = d.get('last_end_id')
            self.last_soc = d.get('last_soc')
            self.value_announced = bool(d.get('value_announced'))
            self.checked_id = d.get('checked_id')
        except Exception:
            pass

    def save(self):
        if not self.path:
            return
        try:
            json.dump({'active': self.active, 'order_id': self.order_id,
                       'peak_power': self.peak_power, 'quiet_since': self.quiet_since,
                       'droop_since': self.droop_since,
                       'noload_alerted': self.noload_alerted,
                       'last_end_id': self.last_end_id,
                       'last_soc': self.last_soc,
                       'value_announced': self.value_announced,
                       'checked_id': self.checked_id},
                      open(self.path, 'w', encoding='utf-8'), ensure_ascii=False)
        except Exception:
            pass

    def feed(self, s: dict) -> list[str]:
        out: list[str] = []
        if s.get('idle'):
            self.active = False
            self.save()
            return out

        now_active = bool(s.get('active'))
        oid = s.get('orderId')

        if oid and oid != self.order_id:
            self.order_id = oid
            self.peak_power = 0.0
            self.quiet_since = None
            self.droop_since = None
            self.noload_alerted = False

        power = float(s.get('power') or 0)
        if power > self.peak_power:
            self.peak_power = power

        if now_active and not self.active:
            out.append(start_text(s))
        if now_active:
            self.active = True
        elif self.active:
            self.active = False
            if oid and oid != self.last_end_id:
                self.last_end_id = oid
                out.append(end_text(s))
                extra = short_money_hint(s)
                if extra:
                    out.append(extra)
        self.save()
        return out


# ────────────────────────── 文案

def _money(v) -> str:
    try:
        return f'¥{float(v):.2f}'
    except (TypeError, ValueError):
        return '¥?'


def _dur(sec) -> str:
    try:
        sec = int(sec or 0)
    except (TypeError, ValueError):
        return '?'
    if sec <= 0:
        return '0 分钟'
    h, m = divmod(sec // 60, 60)
    return f'{h} 小时 {m} 分钟' if h else f'{m} 分钟'


def _where(s: dict) -> str:
    title = s.get('title') or '未知站点'
    sock = s.get('socket')
    return f'{title} {sock} 号插座' if sock is not None else title


def start_text(s: dict) -> str:
    bits = [f'【充电桩·荟铭】开始充电', _where(s)]
    bits.append(f'功率 {s.get("power", 0):.0f}W · 已充 {s.get("energy", 0):.3f} 度 · '
                f'{_money(s.get("money"))}')
    price = hm.price_hint(s)
    if price:
        bits.append(price)
    return '\n'.join(bits)


def progress_text(s: dict) -> str:
    return (f'【充电桩·荟铭】充电中 {_dur(s.get("elapsed"))}\n'
            f'{_where(s)}\n'
            f'功率 {s.get("power", 0):.0f}W · {s.get("energy", 0):.3f} 度 · '
            f'{_money(s.get("money"))}')


def end_text(s: dict) -> str:
    return (f'【充电桩·荟铭】充电结束\n'
            f'{_where(s)}\n'
            f'时长 {_dur(s.get("elapsed"))} · {s.get("energy", 0):.3f} 度 · '
            f'{_money(s.get("money"))}\n'
            f'（{s.get("statusText") or "已结束"}）')


def short_money_hint(s: dict) -> str | None:
    """付了钱但一度电都没充进去 —— 和麦希充那单 ¥1 一样的坑"""
    try:
        money = float(s.get('money') or 0)
        energy = float(s.get('energy') or 0)
    except (TypeError, ValueError):
        return None
    if money > 0 and energy <= 0.001:
        return (f'【充电桩·荟铭】钱可能白花\n'
                f'{_where(s)} 扣了 {_money(money)}，电量 {energy:.3f} 度\n'
                f'建议先在 App 里确认这笔怎么算的。')
    return None


def milestone_text(s: dict, soc: float, mark: float, args) -> str:
    """跨过一个 SOC 里程碑时推的短消息"""
    bits = [f'【充电桩·荟铭】已充到 {mark:g}%', _where(s)]
    bits.append(f'功率 {s.get("power", 0):.0f}W · {s.get("energy", 0):.3f} 度 · '
                f'{_dur(s.get("elapsed"))}')
    if args.soc_target > mark:
        left = args.soc_target - soc
        bits.append(f'当前约 {soc:.0f}%，距 {args.soc_target:g}% 还差 {left:.0f}%')
    else:
        bits.append(f'当前约 {soc:.0f}%，已到目标 {args.soc_target:g}%')
    return '\n'.join(bits)


def noload_text(s: dict) -> str:
    return (f'【充电桩·荟铭】计费了但没充上\n'
            f'{_where(s)} 已经开始 {_dur(s.get("elapsed"))}，功率一直是 0W\n'
            f'检查车有没有插好，或者这个插座是不是坏了。')


# ────────────────────────── 判满

def battery_state(args) -> dict | None:
    """从小哈电池日志（battery.csv）读当前 SOC / 功率。

    这台车充的就是小哈那块电池 —— 实测日志里 66.84V × 12.23A = 817W，
    和充电桩上报的 825W 只差 1%（线路损耗），所以 SOC 可以直接读，不用估。

    返回 {soc, charging, power, ts}；文件不存在或数据太旧返回 None。
    """
    path = str(getattr(args, 'battery_csv', '') or '')
    if not path or not os.path.exists(path):
        return None
    max_age = float(getattr(args, 'battery_max_age', 300) or 300)
    try:
        with open(path, encoding='utf-8') as fh:
            lines = fh.readlines()[-40:]
    except OSError:
        return None
    for line in reversed(lines):
        p = line.strip().split(',')
        if len(p) < 6 or p[0] == 'ts':
            continue
        try:
            ts = int(float(p[0]))
            soc = float(p[2])
            charging = int(float(p[3])) == 1
            cur = float(p[4]) if p[4] else 0.0
            volt = float(p[5]) if p[5] else 0.0
        except (ValueError, IndexError):
            continue
        if time.time() - ts > max_age:
            return None
        return {'soc': soc, 'charging': charging, 'power': cur * volt, 'ts': ts,
                'volt': volt, 'current': cur}
    return None


def soc_estimate(s: dict, st: HmState, args) -> tuple[float | None, str]:
    """估算当前 SOC，返回 (百分比, 依据)。

    这家**不提供电池 SOC**，只能猜。两条路：

    1. 电量法（需要 --capacity-kwh）：
           soc = start_soc + 已充电量 / 容量 × (100 - start_soc)
       容量填电池标称值（比如 72V 40Ah ≈ 2.88 度）。start_soc 是你插上时大概剩多少。

    2. 功率法（不用配置，但只对「会进入恒压」的电池有效）：
       恒流段功率几乎不掉 → 判 80%；进入恒压后功率往下走，
       掉到峰值的 15% → 判 98%。中间线性插值。

    实测这辆车 3.52 小时 2.55 度时功率还有 722W（峰值 820W），全程恒流没进恒压，
    所以功率法对它**判不出 90%**，只能靠电量法。
    """
    b = battery_state(args)
    if b and b.get('charging'):
        mins = (time.time() - b['ts']) / 60
        return b['soc'], (f'电池日志 {b["soc"]:.0f}%'
                          f'（{b["volt"]:.1f}V × {b["current"]:.1f}A = {b["power"]:.0f}W，'
                          f'{mins:.0f} 分钟前）')

    capacity = float(getattr(args, 'capacity_kwh', 0) or 0)
    energy = float(s.get('energy') or 0)

    if capacity > 0:
        start = max(0.0, min(100.0, float(getattr(args, 'start_soc', 0) or 0)))
        soc = start + energy / capacity * (100.0 - start)
        return min(100.0, soc), f'{capacity:g} 度电池、起始 {start:g}%'

    peak = float(st.peak_power or 0)
    power = float(s.get('power') or 0)
    if peak >= float(getattr(args, 'load_watts', 20) or 20) and power > 0:
        ratio = power / peak
        if ratio >= 0.85:
            return 80.0, f'功率仍为峰值 {ratio * 100:.0f}%（按 80% 估）'
        if ratio <= 0.15:
            return 98.0, f'功率降到峰值 {ratio * 100:.0f}%（按 98% 估）'
        return 80.0 + (0.85 - ratio) / 0.70 * 18.0, f'功率 {ratio * 100:.0f}% × 插值'
    return None, '功率不足，无法估算'


def hour_guard(s: dict, args):
    """整点计费守卫：在「还按当前档结算」的最后一刻断开。

    实测规则（牢大确认）：1:00~1:02 之间停，仍按 1 小时收；
    超过 1:02 才算第 2 小时。所以不是「整点前就断」，而是**把 2 分钟宽限用满**。

    做法：
      · 还没到整点，只剩 pre_hour_min 分钟 → 断（保险）
      · 已过整点，超出时间 <= 宽限 - 一个轮询周期 → 再等一轮
      · 已过整点，超出时间落在最后一轮要跳过的区间 → 立刻断
      · 已超出宽限 → 跨档已成事实，不再管
    """
    if getattr(args, 'no_hour_guard', False):
        return None
    elapsed = float(s.get('elapsed') or 0)
    if elapsed < 60:
        return None

    # 当前位于第 k 个小时内（k=0 表示 0:xx，k=1 表示 1:xx）
    k = math.floor(elapsed / 3600.0)
    over = elapsed - k * 3600            # 已过本小时整点多少秒
    left = 3600 - over                   # 距下一个整点还有多少秒
    grace = float(getattr(args, 'grace_min', 2) or 0) * 60
    poll = float(getattr(args, 'interval_active', 60) or 60)
    bill = k + 1                         # 不足 1 小时按 1 小时

    # 1) 还没到整点，但只剩一点点 → 现在断，别越线
    if left <= args.pre_hour_min * 60:
        return (f'已充 {elapsed / 60:.0f} 分钟，距 {bill} 小时整点只剩 '
                f'{left / 60:.1f} 分钟，现在断，按 {bill} 小时结算')

    # 2) 刚过整点，还在宽限内 → 把宽限用满，最后一轮再断
    if over <= grace:
        if over <= grace - poll:
            return None
        return (f'已充 {elapsed / 60:.0f} 分钟（超出 {bill - 1} 小时整点 '
                f'{over / 60:.1f} 分钟，仍在 {grace / 60:g} 分钟宽限内）'
                f'→ 现在断，按 {bill - 1} 小时结算')

    return None                          # 已跨进下一档，断也没用


def _tier_fee(s: dict, power: float):
    """按当前功率查服务费单价"""
    tiers = (s.get('elecPrice') or {}).get('power') or DEFAULT_TIERS
    for t in tiers:
        try:
            if float(t['min']) <= power < float(t['max']):
                return float(t['price'])
        except (KeyError, TypeError, ValueError):
            continue
    return None


def value_verdict(s: dict, st: HmState, args, now: float | None = None,
                  soc: float | None = None):
    """到了评估线之后，算「再充 1% 值不值」。

    不看死百分比，看边际效率：再充 1% 要多少分钟、多少钱。
    功率从 870W 掉到 300W 时，这个数会从 1.9 分钟涨到 5 分钟以上 —— 那才是该停的点。

    返回 None = 还划算，继续充；返回字符串 = 不划算的理由。
    """
    if soc is None:
        soc, _ = soc_estimate(s, st, args)
    if soc is None or soc < args.soc_target:
        return None

    power = float(s.get('power') or 0)
    if power < args.load_watts:
        return None

    cap = float(args.capacity_kwh or 0)
    if cap <= 0:
        return None

    kwh_per_pct = cap / 100.0
    min_per_pct = kwh_per_pct / (power / 1000.0) * 60.0

    if min_per_pct <= args.value_max_min_per_pct:
        return None

    fee = _tier_fee(s, power)
    cost = kwh_per_pct * (ELEC_FEE + (fee or 0))
    return (f'SOC {soc:.0f}%，功率 {power:.0f}W，再充 1% 要 {min_per_pct:.1f} 分钟'
            f'（约 {cost:.3f} 元）> 阈值 {args.value_max_min_per_pct:g} 分钟/%'
            f' —— 继续不划算')


def value_report(s: dict, args, soc: float) -> str:
    """到评估线时推一条：现在停 vs 继续充，各要多久"""
    power = float(s.get('power') or 0)
    cap = float(args.capacity_kwh or 0)
    fee = _tier_fee(s, power)
    lines = [f'【充电桩·荟铭】到 {soc:.0f}%，算一下还值不值',
             _where(s),
             f'当前 {power:.0f}W · {s.get("energy", 0):.3f} 度 · '
             f'{_dur(s.get("elapsed"))} · 服务费 {fee if fee is not None else "?"} 元/度']
    if cap > 0 and power > 0:
        kwh_per_pct = cap / 100.0
        mpp = kwh_per_pct / (power / 1000.0) * 60.0
        left = (100 - soc) * kwh_per_pct
        lines.append(f'再充 1% 约 {mpp:.1f} 分钟；充满还差 {left:.2f} 度')
        if mpp > args.value_max_min_per_pct:
            lines.append(f'阈值 {args.value_max_min_per_pct:g} 分钟/% —— 现在停')
        else:
            lines.append(f'阈值 {args.value_max_min_per_pct:g} 分钟/% —— 还快，继续充')
    return '\n'.join(lines)


def full_reason(s: dict, st: HmState, args, now: float | None = None) -> str | None:
    """返回判满理由；None = 继续充。

    四档（从上到下）：
      1. SOC 估算 ≥ soc_target（电量法 / 功率法，见 soc_estimate）
      2. 功率「有载 → 空载」持续 full_minutes
      3. 已到购买时长 hour
      4. --max-hours 上限
    """
    now = now or time.time()
    if not s.get('active'):
        return None

    power = float(s.get('power') or 0)

    soc, basis = soc_estimate(s, st, args)

    if args.soc_target > 0 and soc is not None and soc >= args.soc_target:
        # 1) 边际效率太差（每 1% 拖太久）→ 立刻断，不等整点
        v = value_verdict(s, st, args, now, soc)
        if v:
            return f'{v}（{basis}）'
        # 2) 边际还行 → 看是不是快到整点，到点前收手，把这一档用满
        g = hour_guard(s, args)
        if g:
            return f'{g}（SOC {soc:.0f}%，{basis}）'
    if args.hard_soc and soc is not None and soc >= 100:
        return f'SOC {soc:.0f}%，已到 100% 上限（{basis}）'

    # 降压：功率从峰值掉下来，说明进入恒压尾声（也可能桩在限功率）。
    # 这一段每度电服务费最便宜，但耗时最长 —— 不值得等。
    if args.droop_ratio > 0 and st.peak_power >= args.load_watts and power > 0:
        if power <= st.peak_power * args.droop_ratio:
            if st.droop_since is None:
                st.droop_since = now
            elif now - float(st.droop_since) >= args.droop_hold * 60:
                pct = power / st.peak_power * 100
                return (f'功率从 {st.peak_power:.0f}W 降到 {power:.0f}W'
                        f'（峰值 {pct:.0f}%），已持续 {args.droop_hold:g} 分钟 —— 进入降压段，不再等')
        else:
            st.droop_since = None

    if args.full_minutes > 0 and st.peak_power >= args.load_watts:
        if power <= args.idle_watts:
            if st.quiet_since is None:
                st.quiet_since = now
            elif now - float(st.quiet_since) >= args.full_minutes * 60:
                return (f'功率从 {st.peak_power:.0f}W 掉到 {power:.0f}W '
                        f'已 {args.full_minutes:g} 分钟（判为充满或已拔车）')
        else:
            st.quiet_since = None

    elapsed = s.get('elapsed') or 0
    hour = s.get('hour') or 0
    if args.max_hours > 0 and elapsed >= args.max_hours * 3600:
        return f'已充 {args.max_hours:g} 小时（--max-hours 上限）'
    if hour and elapsed >= hour * 3600 - args.stop_before_end:
        left = hour * 3600 - elapsed
        return (f'购买 {hour:g} 小时已用到 {elapsed / 60:.0f} 分钟，'
                f'剩 {max(0, left) / 60:.0f} 分钟 —— 按策略收尾，不为最后几个百分点续费')

    return None


# ────────────────────────── 推送

def order_id_of(args, cfg: dict):
    """--order-id 优先，其次配置文件；都没有就交给 snapshot 自己找"""
    return args.order_id or (cfg.get('order_id') or '') or None


def push(cfg: dict, text: str, args) -> bool:
    if getattr(args, 'dry_run', False):
        print('   [dry-run] 不发送')
        return False
    return push_any(
        text,
        dingtalk_webhook=args.dingtalk or cfg.get('dingtalk_webhook'),
        dingtalk_secret=args.dingtalk_secret or cfg.get('dingtalk_secret'),
        wecom_webhook=args.webhook,
    )


# ────────────────────────── 命令

def print_snapshot(s: dict, st: HmState | None = None, args=None,
                   batt: dict | None = None) -> None:
    if s.get('idle'):
        print('当前：没有订单')
        return
    print(f'订单  : {s.get("number")}（id={s.get("orderId")}）')
    print(f'站点  : {_where(s)}   {s.get("address") or ""}')
    print(f'状态  : {s.get("statusText")}'
          + (f'   已充 {_dur(s.get("elapsed"))}' if s.get('active') else ''))
    suffix = '' if s.get('active') else '（结束时最后记录）'
    _p = float(s.get('power') or 0)
    _b = batt if batt is not None else (battery_state(args) if args is not None else None)
    if _b and float(_b.get('current') or 0) > 0.1:
        _v, _i = float(_b['volt']), float(_b['current'])
        _calc = _v * _i
        _implied = _p / _i if _i else 0
        print(f'电压  : {_v:.2f}V × {_i:.2f}A = {_calc:.0f}W（电池端实测）')
        print(f'功率  : {_p:.1f}W（桩报）'
              + (f'，反推桩按 {_implied:.1f}V 算' if _implied else '') + suffix)
        _fee = _tier_fee(s, _p)
        _fee_b = _tier_fee(s, _calc)
        if _fee is not None:
            if _fee_b is not None and _fee > _fee_b:
                print(f'档位  : {_p:.0f}W → {_fee:.2f} 元/度'
                      f'    ⚠️ 电池端 {_calc:.0f}W 只落 {_fee_b:.2f}，被多算一档')
            elif _fee_b is not None and _fee < _fee_b:
                print(f'档位  : {_p:.0f}W → {_fee:.2f} 元/度'
                      f'；电池端 {_calc:.0f}W 会落 {_fee_b:.2f}（桩少算了）')
            else:
                print(f'档位  : {_p:.0f}W → {_fee:.2f} 元/度（两端同档）')
                if abs(_calc - _p) > 20:
                    print(f'        桩比电池端多算 {_p - _calc:.0f}W'
                          f'（{(_p / _calc - 1) * 100:.1f}%），同档内不影响单价')
    else:
        print(f'功率  : {_p:.1f}W{suffix}')
    print(f'电量  : {s.get("energy", 0):.3f} 度')
    if s.get('active'):
        print(f'金额  : {_money(s.get("money"))}（充电中不累加，结束时结算）')
    else:
        print(f'金额  : {_money(s.get("money"))}（实付 {_money(s.get("realMoney"))}）')
    print(f'购买  : {s.get("hour") or 0:g} 小时')
    price = hm.price_hint(s)
    if price:
        print(f'电价  : {price}')
    if st is not None and args is not None and s.get('active'):
        soc, basis = soc_estimate(s, st, args)
        if soc is None:
            print(f'SOC   : 无法估算（{basis}）')
        else:
            print(f'SOC   : 约 {soc:.0f}%（{basis}）')
            if args.soc_target > 0:
                print(f'目标  : {args.soc_target:g}%'
                      + ('  → 可以结束了' if soc >= args.soc_target else '  → 继续充'))


def cmd_sim(args) -> int:
    """离线演示：0W → 790W → 掉到 3W → 判满"""
    print('离线演示（不发请求、不推送）\n')
    # 演示用假数据，不能去读真实的 battery.csv，否则 SOC 会被换成当前实际值
    args.battery_csv = ''
    st = HmState()          # 不传 path，不读本地状态
    fake = [
        (0, 0.0, 0.000, 0.00, '刚下单'),
        (30, 0.0, 0.000, 0.00, '还没插车'),
        (90, 790.0, 0.031, 0.05, '插上了'),
        (600, 812.0, 0.402, 0.62, '正常充电'),
        (1500, 800.0, 1.120, 1.71, '正常充电'),
        (3000, 3.0, 1.480, 2.26, '车快满了，功率掉下来'),
        (3300, 2.0, 1.483, 2.27, '空载第 5 分钟'),
        (3660, 2.0, 1.484, 2.27, '空载第 11 分钟'),
    ]
    for elapsed, power, energy, money, note in fake:
        s = {'active': True, 'orderId': 'demo-1', 'number': 'DEMO0001',
             'title': '演示站点', 'socket': 9, 'hour': 2,
             'power': power, 'energy': energy, 'money': money,
             'elapsed': elapsed, 'statusText': '充电中', 'elecPrice': {}}
        st.peak_power = max(st.peak_power, power)
        reason = full_reason(s, st, args, now=elapsed)
        print(f't={elapsed:5d}s  {power:6.0f}W  {energy:.3f} 度  {money:5.2f} 元  '
              f'→ {reason or "继续充"}   （{note}）')
    print('\n（以上只是判定过程，没有发送任何请求）')
    return 0


def cmd_current(args, cfg: dict) -> int:
    """按服务费档位算「最划算的电流」。

    核心：服务费按功率分档、按度收。**同一档位内电流越大，单价不变、充得越快**，
    所以每个档位都有一个「不超上限的最大整数电流」。

    不同档位之间是「每度贵多少 vs 快多少」的权衡，用 --hours-left 筛掉来不及的。
    """
    # 1) 档位表：优先用当前订单的，拿不到用内置
    tiers = None
    try:
        snap = hm.snapshot(cfg['token'], order_id_of(args, cfg))
    except Exception:
        snap = {}
    if snap.get('elecPrice'):
        tiers = snap['elecPrice'].get('power')
        ELEC = float(snap['elecPrice'].get('elec_fee') or ELEC_FEE)
    else:
        ELEC = ELEC_FEE
    if not tiers:
        tiers = DEFAULT_TIERS

    # 2) 电压：命令行 > 电池日志 > 60V 系统满电 67.2
    b = battery_state(args)
    v = float(args.voltage or (b['volt'] if b else 0) or 67.2)

    # 3) 需要充多少度 / 还剩多少时间
    need = float(args.need_kwh or 0)
    hours_left = float(args.hours_left or 0)
    if (need <= 0 or hours_left <= 0) and snap and not snap.get('idle'):
        energy = float(snap.get('energy') or 0)
        soc = b['soc'] if b else None
        if not need and soc is not None and args.capacity_kwh > 0:
            need = max(0.0, (args.soc_target - soc) / 100 * args.capacity_kwh)
        if not hours_left and snap.get('hour') and snap.get('elapsed') is not None:
            hours_left = max(0.0, float(snap['hour']) * 3600 - float(snap['elapsed'])) / 3600

    print(f'电压  : {v:.1f}V'
          + ('（电池日志实测）' if b else '（默认，可用 --voltage 指定）')
          + f'   满电约 {v * 1.004:.1f}V')
    print(f'电流  : 充电器 {args.min_current:g}~{args.max_current:g}A'
          '（上限受桩 1000W 限制，14A 实测会断开）')
    if need > 0:
        print(f'还需  : {need:.3f} 度（到 {args.soc_target:g}%）')
    if hours_left > 0:
        print(f'剩余  : {hours_left:.2f} 小时（购买时长内）')
    print()
    print(f'{"档位":<12}{"服务费":>7}{"该档最大":>9}{"功率":>8}{"每度成本":>9}'
          f'{"充满需时":>9}  {"来得及":<6}')
    print('-' * 68)

    best_fit = None          # 来得及的档位里，每度最便宜的
    for t in tiers:
        pmin, pmax = float(t['min']), float(t['max'])
        fee = float(t['price'])
        i = int(pmax * args.safety // v)      # 该档允许的最大整数电流
        i = min(i, int(args.max_current))     # 桩/电池上限（实测 13A 以上会触发 1000W 保护）
        if i < args.min_current:              # 低于充电器最低档，这一档用不了
            continue
        power = i * v
        if power < pmin:                       # 档位太窄，整数电流跳过了
            continue
        per_kwh = ELEC + fee
        hours = (need / (power / 1000.0)) if need > 0 and power > 0 else None
        fit = '' if (hours is None or hours_left <= 0) else ('✓' if hours <= hours_left else '✗')
        hs = f'{hours:.2f}h' if hours else '-'
        print(f'{pmin:>4.0f}-{pmax:<6.0f}{fee:>7.2f}{i:>8}A{power:>7.0f}W'
              f'{per_kwh:>9.2f}{hs:>9}  {fit:<6}')
        if hours is not None and hours_left > 0 and hours <= hours_left:
            if best_fit is None or per_kwh < best_fit[0]:
                best_fit = (per_kwh, i, power, fee, hours)

    print()
    # 4) 当前电流落在哪一档
    cur_i = args.current or 0
    if not cur_i and b:
        cur_i = b['current']
    if cur_i:
        cur_power = cur_i * v
        cur_fee = next((float(t['price']) for t in tiers
                        if float(t['min']) <= cur_power < float(t['max'])), None)
        print(f'当前  : {cur_i:.0f}A ≈ {cur_power:.0f}W'
              + (f'，服务费 {cur_fee:.2f} 元/度' if cur_fee is not None else ''))
        # 同档位能提到多少
        for t in tiers:
            if float(t['min']) <= cur_power < float(t['max']):
                i_max = min(int(float(t['max']) * args.safety // v), int(args.max_current))
                if i_max > cur_i:
                    gain = (i_max * v / cur_power - 1) * 100
                    print(f'同档位: 可提到 {i_max}A = {i_max * v:.0f}W，'
                          f'单价不变，充满快 {gain:.0f}%')
                break

    if best_fit:
        per_kwh, i, power, fee, hours = best_fit
        print(f'\n最省且来得及：{i}A = {power:.0f}W，服务费 {fee:.2f}，'
              f'每度 {per_kwh:.2f} 元，约 {hours:.2f} 小时')
    elif need > 0 and hours_left > 0:
        print('\n⚠️ 所有档位都来不及 —— 要么延长购买时长，要么接受充不到目标')

    print('\n注：功率按实测电压算。若充电桩是按「标称电压」算功率（比如按 72V），'
          '\n    同样的电流会算出更大功率，可能触发 1000W 保护 —— 第一次调高时盯着点。')
    return 0


def _battery_window(start_ts: int, finish_ts: int, args):
    """取订单时间段内的电池日志，返回 [(ts, soc, ah, volt), ...]（已排序）"""
    path = str(getattr(args, 'battery_csv', '') or '')
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding='utf-8') as fh:
            for r in csv.DictReader(fh):
                try:
                    ts = int(float(r['ts']))
                except (ValueError, TypeError):
                    continue
                if not (start_ts - 180 <= ts <= finish_ts + 180):
                    continue
                try:
                    ah = float(str(r.get('remainCapacity') or '0').replace('Ah', ''))
                    volt = float(r.get('volt') or 0)
                    soc = float(r.get('soc') or 0)
                except (ValueError, TypeError):
                    continue
                out.append((ts, soc, ah, volt))
    except OSError:
        return []
    out.sort()
    return out


def cmd_price(args, cfg: dict) -> int:
    """按小时段统计实际电价 / 服务费，看有没有峰谷差"""
    token = cfg['token']
    rows = hm.orders(token, pagesize=args.pagesize)
    buckets: dict[str, list] = {}
    samples = 0
    for r in rows:
        try:
            d = hm.details(token, r.get('id'), r.get('ctime'))
        except Exception:
            continue
        if int(d.get('status') or 0) != hm.FINISHED:
            continue
        e = float(d.get('electrics') or 0)
        fee = float(d.get('elec_fee') or 0)
        svc = float(d.get('service_price') or 0)
        st = int(d.get('start_time') or 0)
        if e <= 0.01 or not st:
            continue
        h = time.localtime(st).tm_hour
        key = f'{h // 6 * 6:02d}-{h // 6 * 6 + 6:02d}'
        buckets.setdefault(key, []).append((fee / e, svc / e, (fee + svc) / e))
        samples += 1

    if not samples:
        print('还没有已结算的订单，无法统计')
        return 0

    print(f'样本 {samples} 单（时段按开始时间归到 6 小时一档）\n')
    print(f'{"时段":<10}{"单数":>5}{"电费/度":>10}{"服务费/度":>11}{"合计/度":>10}')
    print('-' * 48)
    for k in sorted(buckets):
        v = buckets[k]
        n = len(v)
        ef = sum(x[0] for x in v) / n
        sf = sum(x[1] for x in v) / n
        tot = sum(x[2] for x in v) / n
        print(f'{k:<10}{n:>5}{ef:>10.3f}{sf:>11.3f}{tot:>10.3f}')

    all_ef = [x[0] for v in buckets.values() for x in v]
    all_sf = [x[1] for v in buckets.values() for x in v]
    print(f'\n电费区间 {min(all_ef):.3f}~{max(all_ef):.3f}'
          f'   服务费区间 {min(all_sf):.3f}~{max(all_sf):.3f}')
    if max(all_ef) - min(all_ef) < 0.03:
        print('→ 各时段电费几乎一致，**没有观察到分时电价**')
    else:
        print('→ 电费有时段差，建议在低价时段充')
    if samples < 8:
        print(f'⚠️ 样本只有 {samples} 单，结论仅供参考；攒够 10 单以上再看更稳')
    return 0


def check_report(d: dict, args) -> str | None:
    """账单核对文本；电池日志不够就返回 None"""
    e = float(d.get('electrics') or 0)
    fee = float(d.get('elec_fee') or 0)
    svc = float(d.get('service_price') or 0)
    st = int(d.get('start_time') or 0)
    ft = int(d.get('finish_time') or 0)
    if e <= 0.01 or not st or not ft:
        return None

    seg = _battery_window(st, ft, args)
    if len(seg) < 2:
        return None

    ah = seg[-1][2] - seg[0][2]
    volt = sum(x[3] for x in seg) / len(seg)
    bwh = ah * volt / 1000.0
    eff = bwh / e * 100

    lines = [f'【充电桩·荟铭】账单核对',
             f'桩端 {e:.3f} 度 · 电费 {fee:.2f} + 服务费 {svc:.2f} = {fee + svc:.2f} 元',
             f'电池端 {seg[0][1]:.0f}% → {seg[-1][1]:.0f}%，+{ah:.2f}Ah ≈ {bwh:.3f} 度',
             f'效率 {eff:.0f}%']
    if eff < 85:
        diff = e - bwh
        lines.append(f'⚠️ 偏低，桩端多算 {diff:.3f} 度（约 {diff * (fee / e):.2f} 元电费）')
    elif eff > 105:
        lines.append('⚠️ 电池端比桩端还多，检查日志时间是否对齐')
    else:
        lines.append('✅ 正常（90%±5%）')
    return '\n'.join(lines)


def cmd_check(args, cfg: dict) -> int:
    """账单核对：桩端计费度数 vs 电池端实际充入度数"""
    token = cfg['token']
    oid = args.order_id or (cfg.get('order_id') or '')
    if oid:
        d = hm.details(token, oid)
        row_id = oid
    else:
        rows = hm.orders(token, pagesize=3)
        if not rows:
            print('没有订单可核对')
            return 0
        # 优先取最近一单已结束的
        d, row_id = None, None
        for r in rows:
            dd = hm.details(token, r.get('id'), r.get('ctime'))
            if int(dd.get('status') or 0) == hm.FINISHED:
                d, row_id = dd, r.get('id')
                break
        if d is None:
            d, row_id = hm.details(token, rows[0].get('id'), rows[0].get('ctime')), rows[0].get('id')

    e = float(d.get('electrics') or 0)
    fee = float(d.get('elec_fee') or 0)
    svc = float(d.get('service_price') or 0)
    st = int(d.get('start_time') or 0)
    ft = int(d.get('finish_time') or 0)
    total = float(d.get('orders_money') or 0)

    print(f'订单    : {d.get("number")}（id={row_id}）')
    print(f'时段    : {time.strftime("%m-%d %H:%M", time.localtime(st))} → '
          f'{time.strftime("%H:%M", time.localtime(ft))}'
          f'（{(ft - st) / 60:.0f} 分钟）')
    if e > 0:
        print(f'单价    : 电费 {fee / e:.3f} 元/度，服务费 {svc / e:.3f} 元/度')

    rep = check_report(d, args)
    if rep:
        print(rep.replace('【充电桩·荟铭】账单核对\n', ''))
    else:
        print('电池端  : 这段时间没有电池日志，无法核对')
    return 0


def cmd_ping(args, cfg: dict) -> int:
    token = cfg.get('token')
    print(f'token: {token_desc(cfg)}')
    p = hm.profile(token)
    u = p.get('userinfo') or {}
    mobile = str(u.get('mobile') or '')
    print(f'账号 : id={u.get("id")} 手机={redact.phone(mobile) or "（无）"} '
          f'余额={_money(u.get("money"))}')
    has = hm.has_using(token)
    print(f'在充 : {"是" if has.get("has") else "否"}'
          + (f'  订单 {has.get("info", {}).get("order_id")}' if has.get('has') else ''))
    rows = hm.orders(token, pagesize=3)
    print(f'订单 : {len(rows)} 条（最近 3 条）')
    for r in rows[:3]:
        print(f'   {r.get("id")}  {hm.status_text(r.get("status"))}  '
              f'{_money(r.get("orders_money"))}  {r.get("area_name") or ""}')
    return 0


def cmd_status(args, cfg: dict) -> int:
    s = hm.snapshot(cfg['token'], order_id_of(args, cfg))
    st = HmState(args.state)
    st.peak_power = max(st.peak_power, float(s.get('power') or 0))
    print_snapshot(s, st, args)
    return 0


def cmd_orders(args, cfg: dict) -> int:
    rows = hm.orders(cfg['token'], pagesize=args.pagesize)
    for r in rows:
        print(f'{r.get("id"):>10}  {hm.status_text(r.get("status")):<6}  '
              f'{_money(r.get("orders_money")):>8}  {r.get("ctime")}  '
              f'{r.get("area_name") or ""}')
    print(f'共 {len(rows)} 条')
    return 0


def cmd_once(args, cfg: dict) -> int:
    s = hm.snapshot(cfg['token'], order_id_of(args, cfg))
    st = HmState(args.state)
    st.peak_power = max(st.peak_power, float(s.get('power') or 0))
    print_snapshot(s, st, args)
    return 0


def cmd_settoken(args, cfg: dict) -> int:
    tok = (args.token or args.value or '').strip().strip('"').strip("'")
    if not tok:
        print('用法：python evwatch/huiming_watch.py settoken "<token>"')
        print('token 在哪：抓包里 profile 响应的 data.userinfo.token')
        return 2
    print('验证中…')
    try:
        p = hm.profile(tok)
        u = p.get('userinfo') or {}
        print(f'验证通过：账号 id={u.get("id")} 余额={_money(u.get("money"))}')
    except hm.HmError as e:
        print(f'验证失败：{e}')
        print('  没有写入。')
        return 1
    except Exception as e:
        print(f'网络异常：{e}')
        if not args.force:
            print('  没验成，但 token 格式没问题。加 --force 强制写入。')
            return 1
    issued = args.issued or time.strftime('%Y-%m-%d %H:%M:%S')
    raw = save_token(args.config, tok, issued)
    print(f'已写入 {args.config}')
    print(f'token: {token_desc(raw)}')
    return 0


def cmd_stop(args, cfg: dict) -> int:
    token = cfg['token']
    oid = args.order_id or cfg.get('order_id')
    if not oid:
        has = hm.has_using(token)
        if not has.get('has'):
            print('当前没有在充订单，无需停止')
            return 0
        oid = (has.get('info') or {}).get('order_id')
    if not args.confirm:
        print(f'[试运行] 将结束订单 {oid}')
        print('确认无误后加 --confirm 真执行')
        return 0
    print(f'结束订单 {oid}…')
    r = hm.close_charge(token, oid)
    print(f'接口返回：{r}')
    time.sleep(3)
    try:
        d = hm.details(token, oid)
        print(f'复查：{hm.status_text(d.get("status"))}')
    except hm.HmError as e:
        print(f'复查失败（不影响已发出的停止请求）：{e}')
    return 0


def cmd_watch(args, cfg: dict) -> int:
    token = cfg['token']
    st = HmState(args.state)
    sink = CsvSink(args.csv, CSV_HEADER)
    fail = 0
    last_progress = 0.0
    last_beat = time.time()
    noload_since = None
    stopped = False

    print(f'===== {time.strftime("%Y-%m-%d %H:%M:%S")} 荟铭充电桩监听启动 =====')
    print(f'token  : {token_desc(cfg)}')
    print(f'轮询   : 空闲 {args.interval}s / 充电中 {args.interval_active}s')
    print(f'自动停 : {"关闭（只监听）" if args.no_auto_stop else "开启"}'
          f'   到 {args.soc_target:g}% 后算性价比：再充 1% > {args.value_max_min_per_pct:g} 分钟就停')
    print(f'        另有：有载→空载 {args.full_minutes:g} 分钟 / 购买时长剩 {args.stop_before_end:g} 秒')
    print(f'整点   : ' + ('关闭' if args.no_hour_guard else
          f'过 {args.soc_target:g}% 后，把整点后的 {args.grace_min:g} 分钟宽限用满再断'
          f'（1:02 内仍按 1 小时）'))
    _b = battery_state(args)
    if _b:
        print(f'SOC    : 直接读电池日志 {_b["soc"]:.0f}%'
              f'（{_b["volt"]:.1f}V × {_b["current"]:.1f}A = {_b["power"]:.0f}W）')
    elif args.capacity_kwh > 0:
        print(f'SOC    : 按 {args.capacity_kwh:g} 度电池、起始 {args.start_soc:g}% 估算')
    else:
        print('SOC    : 没读到电池日志，也没配 --capacity-kwh，只能靠功率插值')
    print(f'降压   : 功率降到峰值 {args.droop_ratio * 100:.0f}% 持续 {args.droop_hold:g} 分钟就结束'
          + ('' if args.droop_ratio > 0 else '（已关闭）'))
    print(f'时长   : 购买时长剩 {args.stop_before_end:g} 秒就收尾（吃满但不超时）')
    print(f'通知   : SOC 里程碑 {args.soc_milestones or "(关闭)"}'
          + (f'，另每 {args.progress_min} 分钟一条进度' if args.progress_min else ''))
    print(f'推送   : {"钉钉" if (args.dingtalk or cfg.get("dingtalk_webhook")) else "未配置"}')
    print(f'CSV    : {os.path.abspath(args.csv)}')

    while True:
        try:
            s = hm.snapshot(token, cfg.get('order_id') or None)
            fail = 0
            now = time.time()

            for msg in st.feed(s):
                print('   -> ' + msg.replace('\n', ' / '))
                push(cfg, msg, args)

            # 订单刚结束：核一次账，桩端计费 vs 电池端实充
            if (not s.get('active') and s.get('finishTime')
                    and s.get('orderId') and st.checked_id != s.get('orderId')):
                st.checked_id = s.get('orderId')
                st.save()
                rep = check_report(s.get('raw') or {}, args)
                if rep:
                    print('   -> 账单核对：' + rep.splitlines()[-1])
                    push(cfg, rep, args)

            if s.get('idle'):
                if args.heartbeat and now - last_beat >= args.heartbeat * 60:
                    print(f'{time.strftime("%H:%M:%S")}  · 空闲（没有订单）')
                    last_beat = now
                stopped = False
                noload_since = None
                time.sleep(args.interval)
                continue

            if s.get('active'):
                if stopped:
                    print(f'{time.strftime("%H:%M:%S")}  · 又检测到在充（可能换了一单）')
                    stopped = False

                _soc, _basis = soc_estimate(s, st, args)
                _soc_txt = f'  SOC≈{_soc:.0f}%' if _soc is not None else ''
                print(f'[{time.strftime("%H:%M:%S")}] 充电中 {_dur(s.get("elapsed"))}  '
                      f'{s.get("power", 0):.0f}W  {s.get("energy", 0):.3f} 度  '
                      f'{_money(s.get("money"))}{_soc_txt}')

                sink.write([s['ts'], s.get('orderId'), 1, s.get('status'),
                            s.get('power'), s.get('energy'), s.get('money'),
                            s.get('elapsed'), s.get('socket')])

                # 计费了但功率一直 0
                if s.get('power', 0) <= 0.5 and (s.get('elapsed') or 0) >= args.noload_after:
                    if not st.noload_alerted:
                        st.noload_alerted = True
                        print('   -> 计费了但没充上（功率 0W）')
                        push(cfg, noload_text(s), args)

                # 到评估线时推一条「继续 vs 停」的账
                _soc, _ = soc_estimate(s, st, args)
                if (_soc is not None and not st.value_announced
                        and args.soc_target > 0 and _soc >= args.soc_target):
                    st.value_announced = True
                    st.save()
                    print(f'   -> 到评估线 {_soc:.0f}%，推一条性价比评估')
                    push(cfg, value_report(s, args, _soc), args)
                if _soc is not None and args.soc_milestones:
                    marks = [float(x) for x in str(args.soc_milestones).split(',')
                             if x.strip()]
                    if st.last_soc is not None:
                        for m in sorted(marks):
                            if st.last_soc < m <= _soc:
                                print(f'   -> 里程碑 {m:g}%（{st.last_soc:.0f}% → {_soc:.0f}%）')
                                push(cfg, milestone_text(s, _soc, m, args), args)
                    st.last_soc = _soc

                # 时间进度（默认关闭，留着给不想按 SOC 推的场景）
                if args.progress_min and now - last_progress >= args.progress_min * 60:
                    last_progress = now
                    push(cfg, progress_text(s), args)

                # 判满
                reason = full_reason(s, st, args, now)
                if reason and not args.no_auto_stop:
                    print(f'{time.strftime("%H:%M:%S")}  · 判满：{reason}')
                    if args.dry_run:
                        print('   [dry-run] 不发停止请求')
                    else:
                        try:
                            hm.close_charge(token, s['orderId'])
                            print('   → 已发送结束请求，3 秒后复查')
                            time.sleep(3)
                            try:
                                chk = hm.details(token, s['orderId'])
                                st_now = int(chk.get('status') or 0)
                                done = st_now == hm.FINISHED or bool(chk.get('finish_time'))
                            except Exception as e2:
                                st_now, done = -1, None
                                print(f'   → 复查失败：{e2}')

                            head = (f'【充电桩·荟铭】已自动结束' if done is True
                                    else '【充电桩·荟铭】结束请求已发，但状态没变')
                            tail = (f'{_dur(s.get("elapsed"))} · {s.get("energy", 0):.3f} 度 · '
                                    f'{_money(s.get("money"))}')
                            if done is False:
                                tail += f'\n复查状态：{hm.status_text(st_now)}，请手动确认'
                            push(cfg, f'{head}\n{_where(s)}\n{reason}\n{tail}', args)
                        except hm.HmError as e:
                            print(f'   → 结束失败：{e}')
                            push(cfg, f'【充电桩·荟铭】自动结束失败：{e}\n请手动处理', args)
                    stopped = True

                nap = args.interval_active
            else:
                # 已结束
                sink.write([s['ts'], s.get('orderId'), 0, s.get('status'),
                            s.get('power'), s.get('energy'), s.get('money'),
                            s.get('elapsed'), s.get('socket')])
                noload_since = None
                nap = args.interval

            if args.heartbeat and now - last_beat >= args.heartbeat * 60:
                print(f'{time.strftime("%H:%M:%S")}  · 心跳  {"在充" if s.get("active") else "已结束"}')
                last_beat = now

        except hm.HmError as e:
            if hm.token_invalid(e):
                print(f'[{time.strftime("%H:%M:%S")}] token 疑似失效：{e}')
                push(cfg, '【充电桩·荟铭】token 失效，监控中断；需重新抓包后 settoken', args)
                time.sleep(max(args.interval, 900))
                continue
            print(f'[{time.strftime("%H:%M:%S")}] 接口报错：{e}')
            nap = args.interval
        except KeyboardInterrupt:
            print('\n已停止')
            return 0
        except Exception as e:
            fail += 1
            print(f'[{time.strftime("%H:%M:%S")}] 失败：{e}')
            print(f'   [net] 尝试过：{net_describe()}')
            nap = min(args.interval * (2 ** min(fail, 4)), 3600)

        try:
            time.sleep(nap)
        except KeyboardInterrupt:
            print('\n已停止')
            return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['ping', 'status', 'orders', 'once', 'watch',
                                    'sim', 'settoken', 'stop', 'current',
                                    'price', 'check'])
    ap.add_argument('value', nargs='?', help='settoken 用：新 token')
    ap.add_argument('--config', default=paths.config('huiming.json'))
    ap.add_argument('--fallback-session', default=paths.config('session.json'),
                    help='只从这里借 dingtalk_webhook / dingtalk_secret')
    ap.add_argument('--csv', default=paths.data('huiming.csv'))
    ap.add_argument('--state', default=paths.data('.huiming_state.json'))
    ap.add_argument('--order-id', help='盯死某一单（调试用）')
    ap.add_argument('--interval', type=int, default=300, help='空闲轮询（秒）')
    ap.add_argument('--interval-active', type=int, default=60, help='充电中轮询（秒）')
    ap.add_argument('--heartbeat', type=int, default=30, help='每 N 分钟一行心跳（0=关）')
    ap.add_argument('--progress-min', type=int, default=0,
                    help='每 N 分钟推一条进度（0=不推，默认靠 SOC 里程碑）')
    ap.add_argument('--soc-milestones', default='50,60,70,80,85,90',
                    help='SOC 每跨过一个值就推一条，逗号分隔（空=关闭）')
    ap.add_argument('--noload-after', type=int, default=180, help='开始 N 秒后功率仍 0W 就告警')
    ap.add_argument('--load-watts', type=float, default=20, help='有载判定：≥N 瓦算在充')
    ap.add_argument('--idle-watts', type=float, default=8, help='空载判定：≤N 瓦算没充')
    ap.add_argument('--full-minutes', type=float, default=10, help='空载持续 N 分钟判满（0=关）')
    ap.add_argument('--capacity-kwh', type=float, default=0,
                    help='电池容量（度），如 72V40Ah≈2.88；填了才能按电量估 SOC')
    ap.add_argument('--start-soc', type=float, default=0,
                    help='插上时大概剩多少电（0-100），配合 --capacity-kwh')
    ap.add_argument('--soc-target', type=float, default=90,
                    help='到这里开始算「再充 1%% 值不值」（不是断电点；0=关闭）')
    ap.add_argument('--value-max-min-per-pct', type=float, default=3,
                    help='再充 1%% 超过 N 分钟就判不划算并结束（默认 3）')
    ap.add_argument('--hard-soc', action='store_true',
                    help='到 100%% 强制停（默认关，靠边际效率判）')
    ap.add_argument('--pre-hour-min', type=float, default=0,
                    help='还没到整点、只剩 N 分钟时提前断（默认 0=不提前，靠宽限兜底）')
    ap.add_argument('--grace-min', type=float, default=2,
                    help='整点后的宽限分钟数：实测 1:02 内仍按 1 小时（默认 2）')
    ap.add_argument('--no-hour-guard', action='store_true',
                    help='关闭整点保护（只在平台确实按整点向上取整时才需要）')
    ap.add_argument('--battery-csv', default=paths.data('battery.csv'),
                    help='小哈电池日志；读得到就直接用真实 SOC（0=不读）')
    ap.add_argument('--battery-max-age', type=float, default=300,
                    help='电池日志超过 N 秒就当过期，不采用')
    ap.add_argument('--droop-ratio', type=float, default=0.9,
                    help='功率降到峰值的 N 倍就判「开始降压」（0=关闭）')
    ap.add_argument('--droop-hold', type=float, default=2,
                    help='降压持续 N 分钟才结束，避免抖动误判')
    ap.add_argument('--stop-before-end', type=float, default=60,
                    help='购买时长剩 N 秒就收尾（默认 60，即 1 小时 59 分停）')
    ap.add_argument('--max-hours', type=float, default=0, help='超过 N 小时强制停（0=不限）')
    ap.add_argument('--no-auto-stop', action='store_true', help='只监听，永不自动停')
    ap.add_argument('--dry-run', action='store_true', help='不推送、不发停止请求')
    ap.add_argument('--dingtalk')
    ap.add_argument('--dingtalk-secret')
    ap.add_argument('--webhook')
    ap.add_argument('--token', help='settoken 用')
    ap.add_argument('--issued', help='settoken 用：token 取得时间')
    ap.add_argument('--force', action='store_true', help='settoken 网络验不过也写入')
    ap.add_argument('--pagesize', type=int, default=10)
    ap.add_argument('--voltage', type=float, help='current 用：电压（默认读电池日志）')
    ap.add_argument('--current', type=float, help='current 用：当前设置的电流')
    ap.add_argument('--need-kwh', type=float, default=0, help='current 用：还需充多少度')
    ap.add_argument('--hours-left', type=float, default=0, help='current 用：还剩多少小时')
    ap.add_argument('--safety', type=float, default=0.98,
                    help='占档位上限的比例（默认 0.98，留 2%% 余量）')
    ap.add_argument('--max-current', type=float, default=13,
                    help='可用最大电流（充电器支持 17A，但桩 1000W 限制只能到 13A）')
    ap.add_argument('--min-current', type=float, default=2,
                    help='充电器最低电流档（实测 2A）')
    ap.add_argument('--confirm', action='store_true', help='stop 用：真执行')
    args = ap.parse_args()

    if args.cmd == 'sim':
        return cmd_sim(args)

    cfg = load_config(args.config, args.fallback_session)

    if args.cmd == 'settoken':
        return cmd_settoken(args, cfg)

    token = cfg.get('token')
    if not token:
        print(f'找不到 token。先抓包，然后：')
        print(f'  python evwatch/huiming_watch.py settoken "<profile 响应里的 data.userinfo.token>"')
        return 2

    if args.cmd == 'current':
        return cmd_current(args, cfg)
    if args.cmd == 'price':
        return cmd_price(args, cfg)
    if args.cmd == 'check':
        return cmd_check(args, cfg)
    if args.cmd == 'ping':
        return cmd_ping(args, cfg)
    if args.cmd == 'status':
        return cmd_status(args, cfg)
    if args.cmd == 'orders':
        return cmd_orders(args, cfg)
    if args.cmd == 'once':
        return cmd_once(args, cfg)
    if args.cmd == 'stop':
        return cmd_stop(args, cfg)
    if args.cmd == 'watch':
        return cmd_watch(args, cfg)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
