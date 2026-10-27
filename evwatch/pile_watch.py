"""
pile_watch.py — 麦希充充电桩监控（第二数据源，与小哈电池监控并行）

它盯的是「这一单到底有没有在充」，而不是电池电量 —— 充电桩接口不给你 SOC，
但给你实时功率。所以判据是功率，不是百分比。

三种值得推的情况（都来自实盘踩坑）：
  1. 开始充电         —— 带当前功率、档位、单价
  2. 功率长时间为 0   —— 桩已计费但没接负载（2026-10-23 实测：¥1 白花、0 度电）
  3. 充电结束         —— 结束原因（28 条字典全解析）+ 用电量 + 实付；故障类原因单独标注

用法：
  python evwatch/pile_watch.py ping                 # 验 token + 最近一单
  python evwatch/pile_watch.py orders               # 订单列表
  python evwatch/pile_watch.py once                 # 一份完整快照
  python evwatch/pile_watch.py watch --interval 300 --interval-active 60
  python evwatch/pile_watch.py sim                  # 离线演示状态机，不联网
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch import pile  # noqa: E402
from evwatch.csvsink import CsvSink  # noqa: E402
from evwatch.net import describe as net_describe  # noqa: E402
from evwatch.notify import push as push_any  # noqa: E402
from evwatch.net import urlopen as _urlopen  # noqa: E402
from evwatch import paths  # noqa: E402
from evwatch import redact  # noqa: E402
import urllib.request  # noqa: E402

HEADER = ['ts', 'orderId', 'active', 'status', 'elapsed_s', 'totalPrice',
          'usedTime', 'usedElectric', 'endType']

# 桩自己报「充满自停」的那几个 endType
FULL_ENDS = {'06', '15', '30', '31'}


def rotate_csv_if_needed(path: str, header: list[str]) -> None:
    """表头换了就把旧文件挪走，别让两种列混在一个文件里"""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return
    try:
        first = open(path, encoding='utf-8').readline().strip()
    except Exception:
        return
    if first != ','.join(header):
        bak = path + '.old'
        try:
            os.replace(path, bak)
            print(f'   CSV 表头变了，旧文件已转存 {bak}')
        except Exception as e:
            print(f'   旧 CSV 转存失败：{e}')


def elapsed_seconds(p: dict, started_at: int | None) -> int | None:
    """真实已充时长：优先用桩给的 startTime，退化为我们第一次看到它的时间"""
    st = p.get('startTime')
    if st:
        try:
            return int(time.time() - time.mktime(time.strptime(st, '%Y-%m-%d %H:%M:%S')))
        except Exception:
            pass
    return None if not started_at else int(time.time() - started_at)


def track_power(st, p: dict, args, now: float | None = None) -> None:
    """维护「本单见过的峰值功率」和「掉到空载的起点」。

    判满不能光看当前功率是 0 —— 没插车时它一直是 0。必须先见过有载（peak_power），
    再看它是不是掉下来并且持续不动。
    """
    now = time.time() if now is None else now
    pw = p.get('power')
    if pw is None:
        return
    if pw >= args.load_watts:
        st.peak_power = max(st.peak_power or 0, pw)
        st.quiet_since = None
    elif st.peak_power and pw <= args.idle_watts:
        if not st.quiet_since:
            st.quiet_since = int(now)
    elif pw > args.idle_watts:
        st.quiet_since = None


def stop_reason(p: dict, elapsed: int | None, st, args, now: float | None = None) -> str | None:
    """要不要自动停。返回原因字符串；None = 继续充"""
    now = time.time() if now is None else now
    if args.no_auto_stop or p.get('status') == 1:
        return None
    if p.get('endType') in FULL_ENDS:
        return f"桩报「{p.get('endText')}」"
    if st.peak_power and st.quiet_since and args.full_minutes:
        quiet = now - st.quiet_since
        if quiet >= args.full_minutes * 60:
            return (f"功率从 {st.peak_power:.0f}W 掉到 ≤{args.idle_watts:g}W 已 "
                    f"{quiet / 60:.0f} 分钟（判为充满或已拔车）")
    if args.max_money and (p.get('totalPrice') or 0) >= args.max_money:
        return f"累计金额 {_money(p.get('totalPrice'))} 到上限"
    if args.max_hours and elapsed and elapsed / 3600 >= args.max_hours:
        return f"已充 {elapsed / 3600:.2f} 小时到上限"
    return None


# ────────────────────────── 推送 ──────────────────────────
def push(session: dict, text: str, args) -> bool:
    """推送：钉钉优先，其次企业微信（与 battery_watch 同一实现）"""
    if getattr(args, 'dry_run', False):
        print('   [dry-run] 不发送')
        return False
    return push_any(
        text,
        dingtalk_webhook=args.dingtalk or session.get('dingtalk_webhook'),
        dingtalk_secret=args.dingtalk_secret or session.get('dingtalk_secret'),
        wecom_webhook=args.webhook,
    )


# ────────────────────────── 文案 ──────────────────────────
def _money(v) -> str:
    return f'¥{v:.2f}' if isinstance(v, (int, float)) else '未知'


def start_text(s: dict) -> str:
    bits = [f"订单 {s['orderId']}"]
    if s.get('deviceName'):
        bits.append(s['deviceName'])
    tail = []
    if s.get('power') is not None:
        tail.append(f'当前 {s["power"]:.0f}W')
    if s.get('region'):
        tail.append(s['region'])
    if s.get('priceHint'):
        tail.append(s['priceHint'])
    return '【电池充电桩】开始充电：' + '，'.join(bits) + ('（' + '，'.join(tail) + '）' if tail else '')


def idle_power_text(s: dict) -> str:
    return (f"【电池充电桩】已计费但没充上电：订单 {s['orderId']} 功率 0W，"
            f"已过 {pile.fmt_dur(s.get('usedTime'))}。检查充电器是否插好/是否被拔，"
            f"否则会像 10-23 那单一样 ¥1 换 0 度电。")


def end_text(s: dict) -> str:
    head = '【电池充电桩】充电结束'
    if (s.get('endType') or '') in pile.BAD_ENDS:
        head = '【电池充电桩】充电异常结束'
    bits = [f"订单 {s['orderId']}", f"原因：{s.get('endText') or '未知'}"]
    if s.get('usedTime') is not None:
        bits.append(f"时长 {pile.fmt_dur(s['usedTime'])}")
    if s.get('usedElectric') is not None:
        bits.append(f"用电 {s['usedElectric']:.2f} 度")
    bits.append(f"订单金额 {_money(s.get('totalPrice'))}")
    if s.get('realPrice') is not None:
        bits.append(f"实扣 {_money(s['realPrice'])}")
    return head + '：' + '，'.join(bits)


def refund_hint(s: dict) -> str | None:
    """付了钱但一度电没充到 —— 值得单独提醒一句去核退款"""
    total, used = s.get('totalPrice'), s.get('usedElectric')
    if total and total > 0 and used is not None and used <= 0 and s.get('refundStatus') == 0:
        return (f"【电池充电桩】钱可能白花：订单 {s['orderId']} 付了 {_money(total)}，"
                f"用电 0 度，退款状态 0（未退）。去小程序「我的-订单」申请退款或找客服。")
    return None


# ────────────────────────── 状态机 ──────────────────────────
class PileState:
    """喂快照，吐该推什么。状态落盘，重启不重推。"""

    def __init__(self, state_path: str | None = None, noload_after: int = 180):
        self.active: bool | None = None
        self.order_id: str | None = None
        self.started_at: int | None = None
        self.noload_alerted = False
        self.last_end_id: str | None = None
        self.peak_power: float | None = None
        self.quiet_since: int | None = None
        self.state_path = state_path
        self.noload_after = noload_after
        self._load()

    def _load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            d = json.load(open(self.state_path, encoding='utf-8'))
            self.active = d.get('active')
            self.order_id = d.get('order_id')
            self.started_at = d.get('started_at')
            self.noload_alerted = bool(d.get('noload_alerted'))
            self.last_end_id = d.get('last_end_id')
            self.peak_power = d.get('peak_power')
            self.quiet_since = d.get('quiet_since')
        except Exception:
            pass

    def save(self):
        if not self.state_path:
            return
        try:
            json.dump({'active': self.active, 'order_id': self.order_id,
                       'started_at': self.started_at, 'noload_alerted': self.noload_alerted,
                       'last_end_id': self.last_end_id, 'peak_power': self.peak_power,
                       'quiet_since': self.quiet_since},
                      open(self.state_path, 'w', encoding='utf-8'), ensure_ascii=False)
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

        # 换了一单（新订单）→ 重置本轮标志
        if oid and oid != self.order_id:
            self.order_id = oid
            self.started_at = s['ts'] if now_active else None
            self.noload_alerted = False

        # 开始充电
        if now_active and not self.active and oid != self.last_end_id:
            self.started_at = s['ts']
            self.noload_alerted = False
            out.append(start_text(s))

        # 计费了但功率长期为 0
        if now_active and not self.noload_alerted:
            power = s.get('power')
            base = self.started_at or s['ts']
            if power is not None and power <= 0 and s['ts'] - base >= self.noload_after:
                out.append(idle_power_text(s))
                self.noload_alerted = True

        # 充电结束
        if not now_active and oid and oid != self.last_end_id:
            if self.active:
                # 正常跳变：结束通知 + 可能的退款提示
                out.append(end_text(s))
                hint = refund_hint(s)
                if hint:
                    out.append(hint)
            else:
                # 启动时这一单就已经结束了：不刷结束通知，但「钱花了没充上」值得补一条
                hint = refund_hint(s)
                if hint:
                    out.append(hint)
            self.last_end_id = oid

        self.active = now_active
        self.save()
        return out


# ────────────────────────── 主程序 ──────────────────────────
def load_config(path: str, fallback: str | None = None) -> dict:
    """主配置 pile.json；fallback（session.json）提供全局项：钉钉 + 手机号。

    手机号全项目只存一处 —— session.json 的 mobile。pile.json 里**不再写 phone**：
    两个文件各存一份，改一个忘一个；而且 pile.json 是充电桩专用文件，
    没必要再带一份身份信息。

    优先级（后面的覆盖前面的）：
        session.json 的 mobile  →  pile.json 的 phone（旧配置兼容）  →  环境变量 EVWATCH_PHONE
    """
    cfg: dict = {}
    src = ''

    if fallback and os.path.exists(fallback):
        try:
            sess = json.load(open(fallback, encoding='utf-8'))
        except Exception as e:
            print(f'{fallback} 读不了：{e}')
        else:
            cfg.update({k: v for k, v in sess.items() if k.startswith('dingtalk')})
            mobile = str(sess.get('mobile') or '').strip()
            if mobile:
                cfg['phone'] = mobile
                src = f'{os.path.basename(fallback)} 的 mobile'

    if os.path.exists(path):
        try:
            own = json.load(open(path, encoding='utf-8'))
        except Exception as e:
            print(f'{path} 读不了：{e}')
        else:
            if str(own.get('phone') or '').strip():
                src = f'{os.path.basename(path)} 的 phone（旧配置，建议挪到 session.json）'
            cfg.update(own)

    env_phone = os.environ.get('EVWATCH_PHONE', '').strip()
    if env_phone:
        cfg['phone'] = env_phone
        src = '环境变量 EVWATCH_PHONE'

    if src:
        cfg['_phone_from'] = src
    return cfg


def token_age(cfg: dict) -> str:
    t = cfg.get('obtained_at')
    if not t:
        return '未记录'
    try:
        ts = time.mktime(time.strptime(t, '%Y-%m-%d %H:%M:%S'))
        return f'{t}（已用 {(time.time() - ts) / 86400:.1f} 天）'
    except Exception:
        return str(t)


def token_desc(cfg: dict) -> str:
    """token 还剩多久 —— JWT 里写着 exp，不用猜"""
    exp = pile.token_exp(cfg.get('token') or '')
    if not exp:
        return f'取得 {token_age(cfg)}（有效期未知）'
    left = exp - time.time()
    end = time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))
    if left <= 0:
        return f'{end} 已过期（{-left / 3600:.1f} 小时前），要重新抓包'
    return f'{end} 到期，还剩 {left / 86400:.2f} 天'


def log_token_expiry(cfg: dict) -> None:
    """失效时记一笔，积累真实寿命（和 battery 的 ticket_expiry.log 一个思路）"""
    t = cfg.get('obtained_at')
    if not t:
        return
    try:
        t0 = time.mktime(time.strptime(t, '%Y-%m-%d %H:%M:%S'))
    except Exception:
        return
    try:
        paths.ensure_dirs()
        with open(paths.log('pile_token.log'), 'a', encoding='utf-8') as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},issued={t},"
                     f"lived_h={(time.time() - t0) / 3600:.2f}\n")
    except Exception:
        pass


def print_snapshot(s: dict) -> None:
    if s.get('idle'):
        print('当前没有订单')
        return
    print(f"订单 {s['orderId']}  {'充电中' if s.get('active') else '已结束'}")
    if s.get('deviceName'):
        print(f"  设备   : {s['deviceName']}  {s.get('deviceAddress') or ''}")
    if s.get('power') is not None:
        print(f"  功率   : {s['power']:.0f}W  {s.get('region') or ''}")
    if s.get('priceHint'):
        print(f"  单价   : {s['priceHint']}")
    print(f"  时长   : {pile.fmt_dur(s.get('usedTime'))}")
    if s.get('usedElectric') is not None:
        print(f"  用电   : {s['usedElectric']:.2f} 度")
    print(f"  金额   : {_money(s.get('totalPrice'))}" +
          (f"  实扣 {_money(s.get('realPrice'))}" if s.get('realPrice') is not None else ''))
    if not s.get('active'):
        print(f"  结束   : {s.get('endText') or '未知'}  ({s.get('endType')})")


def cmd_sim(args) -> int:
    """离线演示判满逻辑，不联网（真实场景：有载 → 功率掉下来 → 判满 → 停）"""
    st = PileState(None)
    st.peak_power = None
    st.quiet_since = None
    t0 = 1793000000.0
    seq = [
        (0, 0, 1.00, '刚下单，还没插车'),
        (30, 0, 1.00, '仍然空载（不判满，因为没见过有载）'),
        (90, 132, 1.20, '插上了，正常充电'),
        (600, 148, 1.85, '正常充电'),
        (1500, 3, 2.40, '车充满了 → 功率掉到 3W'),
        (1500 + 5 * 60, 2, 2.42, '空载第 5 分钟'),
        (1500 + 11 * 60, 2, 2.45, '空载第 11 分钟 → 触发判满'),
    ]
    for t, pw, money, note in seq:
        p = {'status': 0, 'power': pw, 'totalPrice': money,
             'endType': '', 'endText': '', 'usedTime': 0}
        track_power(st, p, args, now=t0 + t)
        reason = stop_reason(p, t, st, args, now=t0 + t)
        print(f"t={t:5d}s  {pw:4d}W  ¥{money:.2f}  → {reason or '继续充'}   （{note}）")
    print('\n（以上只是判定过程，没有发送任何请求）')
    return 0


def cmd_login(args, cfg: dict) -> int:
    """用手机号换一份新 token（小程序自身的登录接口 checkPhoneNumber）

    手机号不硬编码，也不单独存在 pile.json：
      1. --phone 显式传入
      2. session.json 的 "mobile"（全局配置，推荐）
      3. 环境变量 EVWATCH_PHONE
    """
    phone = (args.phone or cfg.get('phone') or '').strip()
    if not phone:
        print('找不到手机号。三选一：')
        print('  1) session.json 里写 "mobile": "13800138000"   ← 全局配置，推荐')
        print('  2) python evwatch/pile_watch.py login --phone 13800138000')
        print('  3) 环境变量 EVWATCH_PHONE=13800138000')
        print('只能换你自己手机号的 token。')
        return 2
    if not args.phone:
        print(f'用 {cfg.get("_phone_from") or "配置"} 里的手机号 '
              f'{phone[:3]}****{phone[-4:]}')
    print('登录中…')
    try:
        d = pile.login(phone)
    except pile.PileError as e:
        print(f'登录失败：{e}')
        print('  手机号可能不对，或接口改了。没有写任何东西。')
        return 1
    tok = d['token']
    try:
        raw = save_token(args.config, tok, time.strftime('%Y-%m-%d %H:%M:%S'))
    except Exception as e:
        print(f'写 {args.config} 失败：{e}')
        return 1
    print(f'已写入 {args.config}')
    print('token:', token_desc(raw))
    return 0


def cmd_status(args, cfg: dict) -> int:
    """现在在不在充 / 这一单什么状态"""
    tok = cfg['token']
    olist = pile.using_orders(tok)
    if not olist:
        print('进行中订单：无')
        rows = pile.orders(tok, page_size=3)
        if rows:
            r = rows[0]
            end = pile.END_TYPES.get(str(r.get('endType') or ''), r.get('endType'))
            print(f"最近一单  {r.get('orderId')}  {_money(r.get('totalPrice'))}  {end}")
        return 0
    for o in olist:
        oid = o.get('orderId')
        print(f'在充订单 {oid}')
        for k, v in o.items():
            if k not in ('params', 'tableNames'):
                print(f'  {k}: {v}')
        code, msg = pile.order_status(tok, oid)
        tail = '（已结束）' if code == pile.ORDER_ENDED else ''
        print(f'  getOrderStatus: {code} {msg}{tail}')
    return 0


def cmd_stop(args, cfg: dict) -> int:
    """远程结束充电"""
    tok = cfg['token']
    oid = args.order_id
    if not oid:
        olist = pile.using_orders(tok)
        if not olist:
            print('当前没有在充订单，无需停止。')
            return 0
        oid = olist[0].get('orderId')
    code, msg = pile.order_status(tok, oid)
    print(f'订单 {oid}   状态 {code} {msg}')
    if code == pile.ORDER_ENDED:
        print('这单已经结束了。')
        return 0
    print('将要执行：POST /app/business/stopOrder  →  远程结束这单')
    if not args.confirm:
        print('\n（试运行，没有发送。确认要停就加 --confirm）')
        return 0
    d = pile.stop_order(tok, oid)
    print('已下发：' + json.dumps(d, ensure_ascii=False))
    time.sleep(3)
    code2, msg2 = pile.order_status(tok, oid)
    print(f'复查：{code2} {msg2}' + ('  → 已停止' if code2 == pile.ORDER_ENDED else '  → 还没结束，等一轮再看'))
    return 0


def report_end(token: str, cfg: dict, args, st: PileState) -> None:
    """从充电中切回空闲时，补一条结束通知（原因/金额/时长）"""
    try:
        rows = pile.orders(token, page_size=3)
    except Exception as e:
        print(f'   收尾查询失败：{e}')
        return
    if not rows:
        return
    r = rows[0]
    oid = r.get('orderId')
    if not oid or oid == st.last_end_id:
        return
    st.last_end_id = oid
    st.save()
    end = pile.END_TYPES.get(str(r.get('endType') or ''), r.get('endType') or '未知')
    bad = str(r.get('endType') or '') in pile.BAD_ENDS
    head = '【电池充电桩】充电异常结束' if bad else '【电池充电桩】充电结束'
    msg = (f"{head}：订单 {oid}，原因 {end}，"
           f"累计 {_money(r.get('totalPrice'))}，时长 {pile.fmt_dur(_f(r.get('usedTime')))}")
    print(f'{time.strftime("%H:%M:%S")}  · {msg}')
    push(cfg, msg, args)
    if (r.get('totalPrice') or 0) > 0 and (r.get('usedElectric') or 0) <= 0:
        push(cfg, f"【电池充电桩】这单用电 0 度、收了 {_money(r.get('totalPrice'))}，"
                  f"退款状态 {r.get('refundStatus')}。金额是按时间收的，不是按电量。", args)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_token(path: str, token: str, obtained_at: str) -> dict:
    """把 token 写进 pile.json —— 只动它自己的键。

    load_config 会把 session.json 的钉钉配置和手机号并进来，原样写回等于
    把全局配置复制一份到这个文件（踩过：webhook 被复制进 pile.json）。
    手机号也**不写**：它只存 session.json 的 mobile 一处。
    """
    try:
        raw = json.load(open(path, encoding='utf-8')) if os.path.exists(path) else {}
    except Exception:
        raw = {}
    raw.setdefault('_说明', '麦希充 token：抓包 checkPhoneNumber 响应里的 data.token')
    raw['token'] = token
    raw['obtained_at'] = obtained_at
    for k in ('phone', '_phone_from'):
        raw.pop(k, None)          # 旧版本写过，清掉；手机号以 session.json 为准
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
        fh.write('\n')
    return raw


def cmd_settoken(args, cfg: dict) -> int:
    """把新抓的 token 写进 pile.json，并立刻验一次。

    每 7 天续一次用这个，比手动编辑 JSON 少踩两个坑：
      * 自动填 obtained_at（不然剩余天数算不准）
      * 写完立刻打一发 getPersonalInfo，token 贴错当场就知道
    """
    tok = (args.token or args.value or '').strip().strip('"').strip("'")
    if not tok:
        print('用法：python evwatch/pile_watch.py settoken "<token>"')
        print('token 在哪：抓包里 checkPhoneNumber 响应的 data.token 字段')
        return 2
    if tok.count('.') != 2:
        print(f'⚠️  这不像 JWT（点号 {tok.count(".")} 个），仍会尝试写入')
    exp = pile.token_exp(tok)
    if exp:
        import datetime
        print('新 token 到期：' + datetime.datetime.fromtimestamp(exp).strftime('%Y-%m-%d %H:%M:%S'))

    print('验证中…')
    try:
        rows = pile.orders(tok)
        print(f'验证通过：订单 {len(rows)} 条')
    except pile.PileError as e:
        print(f'验证失败：{e}')
        print('  没有写入。token 可能贴错、或已经过期。')
        return 1
    except Exception as e:
        print(f'网络异常：{e}')
        print('  没验成，但 token 语法没问题。加 --force 强制写入。')
        if not args.force:
            return 1

    issued = args.issued or time.strftime('%Y-%m-%d %H:%M:%S')
    try:
        raw = save_token(args.config, tok, issued)
    except Exception as e:
        print(f'写 {args.config} 失败：{e}')
        return 1
    print(f'已写入 {args.config}（obtained_at={issued}）')
    print(f'token: {token_desc(raw)}')
    return 0


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except Exception:
            pass

    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['ping', 'orders', 'once', 'watch', 'sim', 'settoken',
                                    'login', 'status', 'stop'])
    ap.add_argument('value', nargs='?', help='settoken 用：新 token（也可以写成 --token）')
    ap.add_argument('--config', default=paths.config('pile.json'))
    ap.add_argument('--fallback-session', default=paths.config('session.json'),
                    help='全局配置：从这里读 dingtalk_webhook / dingtalk_secret / mobile')
    ap.add_argument('--interval', type=int, default=300, help='空闲轮询间隔（秒）')
    ap.add_argument('--interval-active', type=int, default=60, help='充电中轮询间隔（秒）')
    ap.add_argument('--noload-after', type=int, default=180, help='功率持续 0W 多少秒后告警')
    ap.add_argument('--csv', default=paths.data('pile.csv'))
    ap.add_argument('--state', default=paths.data('.pile_state.json'))
    ap.add_argument('--heartbeat', type=int, default=30, help='每 N 分钟打一行心跳（0=关闭）')
    ap.add_argument('--dingtalk')
    ap.add_argument('--dingtalk-secret')
    ap.add_argument('--webhook')
    ap.add_argument('--send', action='store_true')
    ap.add_argument('--dry-run', action='store_true', help='只打印不推送（调试用，避免手滑发群）')
    ap.add_argument('--token', help='settoken 用：新的 token（也可以直接用位置参数）')
    ap.add_argument('--issued', help='settoken 用：token 的签发时间，默认现在（格式 2026-10-23 17:20:50）')
    ap.add_argument('--force', action='store_true', help='settoken 用：网络验不过也强制写入')
    ap.add_argument('--phone', help='login 用：手机号（默认读 session.json 的 mobile）')
    ap.add_argument('--order-id', help='stop 用：指定订单号（默认自动取在充订单）')
    ap.add_argument('--confirm', action='store_true', help='stop 用：真的执行（不加只试运行）')
    ap.add_argument('--max-hours', type=float, default=0, help='watch 用：充满 N 小时自动停（0=不限）')
    ap.add_argument('--max-money', type=float, default=0, help='watch 用：累计金额到 N 元自动停（0=不限）')
    ap.add_argument('--no-auto-stop', action='store_true', help='watch 用：只监听，永不自动停')
    ap.add_argument('--progress-min', type=int, default=30, help='watch 用：每 N 分钟推一条进度（0=不推）')
    ap.add_argument('--load-watts', type=float, default=20, help='有载判定：功率≥N 瓦算在充')
    ap.add_argument('--idle-watts', type=float, default=8, help='空载判定：功率≤N 瓦算没充')
    ap.add_argument('--full-minutes', type=float, default=10,
                    help='空载持续 N 分钟即判满并停（0=关闭这个判据）')
    args = ap.parse_args()

    if args.cmd == 'sim':
        return cmd_sim(args)

    cfg = load_config(args.config, args.fallback_session)

    # login / settoken 就是来「拿 token」的，不能因为还没有 token 被拦在门外
    if args.cmd == 'settoken':
        return cmd_settoken(args, cfg)

    if args.cmd == 'login':
        return cmd_login(args, cfg)

    token = cfg.get('token')
    if not token:
        print(f'找不到 token。要么 login 拿一份，要么建 {args.config}：')
        print('  {"token": "<抓包 checkPhoneNumber 响应里的 data.token>", '
              '"obtained_at": "2026-10-23 17:20:50"}')
        return 2

    if args.cmd == 'status':
        return cmd_status(args, cfg)

    if args.cmd == 'stop':
        return cmd_stop(args, cfg)

    if args.cmd == 'ping':
        print(f'token: {token_desc(cfg)}')
        try:
            rows = pile.orders(token)
            print(f'token: 有效；订单 {len(rows)} 条')
            if rows:
                r = pile.pick_order(rows)
                s = pile.snapshot(token, r.get('orderId'), order_row=r)
                print_snapshot(s)
            else:
                print('  还没有订单')
            w = pile.wallet(token)
            print(f"钱包: 余额 {_money(w.get('money'))}  累计支付 {_money(w.get('totalPay'))}")
        except pile.PileError as e:
            if pile.token_invalid(e):
                print(f'token: 已失效 ({e})')
                print('  处理：手机重开小程序，抓 checkPhoneNumber 的响应，把 data.token 换进 pile.json')
            else:
                print(f'失败: {e}')
            return 1
        return 0

    if args.cmd == 'orders':
        try:
            for r in pile.orders(token):
                print(json.dumps(r, ensure_ascii=False))
        except pile.PileError as e:
            print(f'失败: {e}')
            return 1
        return 0

    if args.cmd == 'once':
        try:
            print(json.dumps(pile.snapshot(token, cfg.get('order_id') or None),
                             ensure_ascii=False, indent=2))
        except pile.PileError as e:
            print(f'失败: {e}')
            return 1
        return 0

    # watch：只监听进度，不发起充电
    rotate_csv_if_needed(args.csv, HEADER)
    st = PileState(args.state, noload_after=args.noload_after)
    sink = CsvSink(args.csv, HEADER)
    auto_stop = not args.no_auto_stop
    limits = []
    if args.max_money:
        limits.append(f'金额≥{_money(args.max_money)}')
    if args.max_hours:
        limits.append(f'时长≥{args.max_hours:g}h')
    print(f'===== {time.strftime("%Y-%m-%d %H:%M:%S")} 充电桩监控启动 =====')
    print(f'解释器 : {sys.executable}')
    print(f'token  : {token_desc(cfg)}')
    print(f'轮询   : 空闲 {args.interval}s / 充电中 {args.interval_active}s')
    print('自动停 : ' + ('已关闭（--no-auto-stop，只监听）' if not auto_stop
                          else (' / '.join(limits) if limits
                                else '只在桩报「充满自停」时停；没设金额/时长上限')))
    print(f'进度   : 每 {args.progress_min} 分钟一条' if args.progress_min else '进度   : 不推送')
    print(f'推送   : {"钉钉" if (args.dingtalk or cfg.get("dingtalk_webhook")) else "未配置"}')
    _p = str(cfg.get('phone') or '').strip()
    if _p:
        print(f'续期   : {cfg.get("_phone_from") or "配置"} → {redact.phone(_p)}'
              f'（token 失效自动重登）')
    else:
        print('续期   : 未配手机号（session.json 的 mobile），token 失效不会自动重登')
    print(f'CSV    : {os.path.abspath(args.csv)}')
    print('        判满：有载(≥%gW) → 掉到空载(≤%gW) 持续 %g 分钟'
          % (args.load_watts, args.idle_watts, args.full_minutes)
          + ('；或桩报充满自停' if not args.no_auto_stop else ''))

    try:
        cfg_mtime = os.path.getmtime(args.config)
    except OSError:
        cfg_mtime = 0
    last_beat = time.time()
    last_progress = time.time()
    login_alerted = False
    expiry_warned = False
    left = pile.token_left(cfg.get('token') or '')
    if left is not None and left < 86400:
        expiry_warned = True
        print(f'   ⚠️ token 剩余不足 1 天（{left / 3600:.1f} 小时）')
        push(cfg, f'【电池充电桩】token 将在 {left / 3600:.1f} 小时后过期，'
                  f'之后监控中断（重新抓包或 login）', args)
    fail = 0
    mode = None
    stop_attempted: set[str] = set()

    while True:
        nap = args.interval
        try:
            # 热加载：settoken / login 改了 pile.json 就切过去，不用重启
            try:
                m = os.path.getmtime(args.config)
                if m != cfg_mtime:
                    cfg_mtime = m
                    fresh = load_config(args.config, args.fallback_session)
                    if fresh.get('token') and fresh['token'] != token:
                        token = fresh['token']
                        cfg['token'] = token
                        cfg['obtained_at'] = fresh.get('obtained_at')
                        expiry_warned = False
                        print(f"{time.strftime('%H:%M:%S')}  · pile.json 已更新，切到新 token"
                              f"（{token_desc(cfg)}）")
                        push(cfg, '【电池充电桩】token 已热更新，监控继续', args)
            except OSError:
                pass

            rows = pile.using_orders(token)
            login_alerted = False
            fail = 0

            if not rows:
                # 充电中 → 空闲：补一条结束通知
                if st.active and st.order_id:
                    report_end(token, cfg, args, st)
                st.active = False
                st.order_id = None
                st.started_at = None
                st.peak_power = None
                st.quiet_since = None
                st.save()
                sink.write([int(time.time()), None, 0, None, None, None, None, None, None])
                if mode != 'idle':
                    mode = 'idle'
                    print(f"{time.strftime('%H:%M:%S')}  · 空闲（没有在充订单）")
                nap = args.interval
            else:
                oid = rows[0].get('orderId')
                p = pile.order_progress(token, oid)
                el = elapsed_seconds(p, st.started_at)
                new_order = oid != st.order_id
                if new_order:
                    st.order_id = oid
                    st.started_at = int(time.time())
                    stop_attempted.discard(oid)
                    st.peak_power = None
                    st.quiet_since = None
                    last_progress = time.time()
                    mode = 'charging'
                    print(f"{time.strftime('%H:%M:%S')}  · 开始充电  订单 {oid}"
                          f"  {p.get('startTime') or ''}")
                    push(cfg, f"【电池充电桩】开始充电：订单 {oid}，"
                              f"起始金额 {_money(p.get('totalPrice'))}，"
                              f"到 {args.max_hours:g} 小时或 ¥{args.max_money:g} 自动停"
                              if (args.max_hours or args.max_money) else
                              f"【电池充电桩】开始充电：订单 {oid}，起始金额 {_money(p.get('totalPrice'))}",
                         args)
                st.active = True
                st.save()

                sink.write([int(time.time()), oid, 1, p.get('status'), el,
                            p.get('totalPrice'), p.get('usedTime'), p.get('usedElectric'),
                            p.get('endType')])

                if args.progress_min and time.time() - last_progress >= args.progress_min * 60:
                    last_progress = time.time()
                    pw = p.get('power')
                    msg = (f"【电池充电桩】充电中 {pile.fmt_dur(el)}，"
                           f"累计 {_money(p.get('totalPrice'))}"
                           + (f"，{pw:.0f}W" if pw else '')
                           + f"（订单 {oid}）")
                    print(f"{time.strftime('%H:%M:%S')}  · {msg}")
                    push(cfg, msg, args)

                if args.heartbeat and time.time() - last_beat >= args.heartbeat * 60:
                    last_beat = time.time()
                    print(f"{time.strftime('%H:%M:%S')}  · 心跳  充电中 {pile.fmt_dur(el)}"
                          f"  {_money(p.get('totalPrice'))}")

                track_power(st, p, args)
                st.save()
                reason = stop_reason(p, el, st, args)
                if reason and oid not in stop_attempted:
                    stop_attempted.add(oid)
                    print(f"   → 自动结束：{reason}")
                    try:
                        pile.stop_order(token, oid)
                        push(cfg, f"【电池充电桩】自动结束充电：{reason}。"
                                  f"订单 {oid}，已充 {pile.fmt_dur(el)}，"
                                  f"累计 {_money(p.get('totalPrice'))}", args)
                        time.sleep(3)
                        code, _msg = pile.order_status(token, oid)
                        print('   复查：' + ('已结束' if code == pile.ORDER_ENDED
                                             else '尚未结束，下一轮再看'))
                    except Exception as e:
                        print(f'   停止失败：{e}')
                        push(cfg, f'【电池充电桩】自动结束失败：{e}（订单 {oid}）', args)

                nap = args.interval_active

            if not expiry_warned:
                left = pile.token_left(cfg.get('token') or '')
                if left is not None and left < 86400:
                    expiry_warned = True
                    print(f'   ⚠️ token 剩余不足 1 天（{left / 3600:.1f} 小时）')
                    push(cfg, f'【电池充电桩】token 将在 {left / 3600:.1f} 小时后过期，'
                              f'之后监控中断', args)

        except pile.PileError as e:
            if pile.token_invalid(e):
                print(f'[{time.strftime("%H:%M:%S")}] token 失效 ({e})')
                log_token_expiry(cfg)
                phone = (cfg.get('phone') or '').strip()
                if phone:
                    try:
                        d = pile.login(phone)
                        token = d['token']
                        save_token(args.config, token, time.strftime('%Y-%m-%d %H:%M:%S'))
                        cfg['token'] = token
                        login_alerted = False
                        print(f"{time.strftime('%H:%M:%S')}  · 已用手机号自动续期"
                              f"（{cfg.get('_phone_from') or '配置'}，{token_desc(cfg)}）")
                        continue
                    except Exception as e2:
                        print(f'   自动续期失败：{e2}')
                if not login_alerted:
                    push(cfg, '【电池充电桩】监控中断：token 已失效，且没配手机号无法自动续期'
                              '（在 session.json 里加 mobile）', args)
                    login_alerted = True
                time.sleep(max(args.interval, 900))
                continue
            print(f'[{time.strftime("%H:%M:%S")}] 接口报错: {e}')
        except KeyboardInterrupt:
            print('\n已停止')
            return 0
        except Exception as e:
            fail += 1
            print(f'[{time.strftime("%H:%M:%S")}] 失败: {e}')
            print(f'   [net] 尝试过：{net_describe()}')
            nap = min(args.interval * (2 ** min(fail, 4)), 3600)
        try:
            time.sleep(nap)
        except KeyboardInterrupt:
            print('\n已停止')
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
