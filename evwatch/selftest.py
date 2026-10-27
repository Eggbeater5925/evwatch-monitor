"""
selftest.py — 自检

A. 离线向量测试（任何环境都能跑，不需要抓包、不需要登录）
   - 加解密回环
   - 已知答案测试（KAT）：固定明文必须得到固定密文，防止哪天把 key/iv/模式改错
   - 签名复算：固定 timestamp/nonce 必须得到固定 signature
   - 固定 IV 的确定性：同明文两次加密必须完全一致

B. 真实抓包校验（可选，仅当 `captures/小哈智电/` 下有 `[NNN] request_switchpowerapi...` 时）
   - 所有密文都能解出 JSON
   - 所有请求的 signature 都能复算一致

抓包文件含个人数据，不随仓库分发；没有样本时自动跳过 B。
"""
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from evwatch.hb_crypto import decrypt, encrypt, parse_capture, sign, verify_sign  # noqa: E402
from evwatch import paths  # noqa: E402

ROOT = paths.ROOT

# ---- 已知答案向量（由本实现生成后固化，用于回归）----
KAT_PLAIN = '{"a":1}'
KAT_CIPHER = 'gmfbQGxM28F8S4asUlyctQ=='
KAT_TS = '1700000000000'
KAT_NONCE = '123456'
KAT_SIGN = 'd71984e2e89302738b794ef821b8bf32e69f7c8c'


def part_a() -> bool:
    ok = True

    if decrypt(encrypt(KAT_PLAIN)) != KAT_PLAIN:
        print('  ✗ 加解密回环失败')
        ok = False

    if encrypt(KAT_PLAIN) != KAT_CIPHER:
        print(f'  ✗ KAT 密文不匹配，期望 {KAT_CIPHER}')
        ok = False

    if encrypt(KAT_PLAIN) != encrypt(KAT_PLAIN):
        print('  ✗ 加密不确定（IV 应该是固定的）')
        ok = False

    sig, ts, nonce = sign(KAT_PLAIN, ts=KAT_TS, nonce=KAT_NONCE)
    if sig != KAT_SIGN:
        print(f'  ✗ 签名不匹配，期望 {KAT_SIGN}')
        ok = False
    if not verify_sign(KAT_PLAIN, ts, nonce, sig):
        print('  ✗ verify_sign 复核失败')
        ok = False
    if len(sig) != 40:
        print('  ✗ 签名长度不是 40')
        ok = False

    print(f'  离线向量：{"OK" if ok else "FAIL"}，签名 {sig[:12]}…')
    return ok


def part_b() -> bool:
    reqs = sorted(glob.glob(os.path.join(paths.HELLOBIKE_CAPTURE_DIR, '[[]*request_switchpower*')))
    resps = sorted(glob.glob(os.path.join(paths.HELLOBIKE_CAPTURE_DIR, '[[]*response_switchpower*')))
    if not reqs and not resps:
        print('  未找到抓包样本，跳过真实数据校验（正常）')
        return True

    ok_dec = ok_sign = 0
    for f in reqs:
        cap = parse_capture(f)
        try:
            plain = decrypt(cap.body)
            json.loads(plain)
            ok_dec += 1
        except Exception as e:
            print(f'  ✗ 解密失败 {os.path.basename(f)}: {e}')
            continue
        if verify_sign(plain, cap.headers.get('timestamp', ''), cap.headers.get('nonce', ''),
                       cap.headers.get('signature', '')):
            ok_sign += 1
        else:
            print(f'  ✗ 签名不符 {os.path.basename(f)}')

    for f in resps:
        cap = parse_capture(f)
        try:
            json.loads(decrypt(cap.body))
            ok_dec += 1
        except Exception as e:
            print(f'  ✗ 解密失败 {os.path.basename(f)}: {e}')

    total = len(reqs) + len(resps)
    print(f'  真实抓包：解密 {ok_dec}/{total}，签名 {ok_sign}/{len(reqs)}')
    return ok_dec == total and ok_sign == len(reqs)


def part_c() -> bool:
    """C. 铁塔「电池停了就结束桩」的离线校验（不需要登录态）

    - endCharge 请求体必须和抓包逐字节一致（多一个字段服务端就可能拒绝）
    - start 报文的 chargeType：充满自停=0 / 按时长=5
    - charging/detail 的 resData 能被压成快照
    - 判据以电池端为准：chargeStatus 1→0 连续 N 次才停；刚插上（status=0）不能停
    - 电池同一份缓存（report_ts 不变）不重复计数
    - 电池数据缺失时回落到桩侧兜底
    - 若有 `captures/铁塔充电_raw/` 抓包，再拿真实响应体校验一遍
    """
    from evwatch.tower import (CHARGE_TYPE_FULL, CHARGE_TYPE_TIME, StopDecider,
                              end_charge_payload, snap_from_detail, start_payload)

    ok = True

    body = json.dumps(end_charge_payload(2536900, 'CDC000000000000000', True),
                      separators=(',', ':'), ensure_ascii=False)
    want = '{"plugId":2536900,"orderNum":"CDC000000000000000","openDoor":1}'
    if body != want:
        print(f'  ✗ endCharge 请求体不对：{body}')
        ok = False

    full = start_payload('2536900', full=True, pay_money=6)
    timed = start_payload('2536900', full=False, hours=2, pay_money=1)
    if full['chargeType'] != CHARGE_TYPE_FULL or full['time'] != 0:
        print(f'  ✗ 充满自停报文不对：{full}')
        ok = False
    if timed['chargeType'] != CHARGE_TYPE_TIME or timed['time'] != 2:
        print(f'  ✗ 按时长报文不对：{timed}')
        ok = False

    raw = paths.TOWER_CAPTURE_DIR
    resp = os.path.join(raw, '[353] response_charge-api.chinatowercom.cn_message.txt')
    if os.path.exists(resp):
        cap = parse_capture(resp)
        snap = snap_from_detail(json.loads(cap.body).get('data'))
        if not snap or not (snap['plugNum'] or '').startswith('HBLF') or snap['orderNum'] is None:
            print(f'  ✗ 真实响应解析失败：{snap}')
            ok = False
        else:
            print(f"  真实抓包：{snap['plugNum']} {snap['current']}A/{snap['voltage']}V "
                  f"chargeType={snap['chargeType']} 订单 {snap['orderNum']}")
    else:
        snap = snap_from_detail({'resData': {'orderNum': 'X', 'plugNum': 'P', 'plugId': 1,
                                             'startTime': 1_700_000_000_000,
                                             'currentTime': 1_700_000_600_000,
                                             'current': 5.0, 'voltage': 220.0}})
        if not snap or snap['elapsed'] != 600 or snap['current'] != 5.0:
            print(f'  ✗ 快照归一化失败：{snap}')
            ok = False

    def pile(mins, cur=5.0):
        return {'orderNum': 'T', 'current': cur, 'voltage': 220.0, 'elapsed': mins * 60.0}

    # 电池从「充电中」变「未充电」→ 必须停
    det = StopDecider(confirm=2, warmup_min=3)
    seq = [(5, '1', 90, 5.0, 1000), (10, '1', 95, 5.0, 1060), (15, '1', 99, 5.0, 1120),
           (20, '0', 100, 0.0, 1180), (21, '0', 100, 0.0, 1240)]
    verdict_at = None
    for i, (mins, st, soc, bcur, ts) in enumerate(seq):
        v = det.feed(pile(mins), {'soc': soc, 'status': st, 'current': bcur, 'batt_ts': ts})
        if v and verdict_at is None:
            verdict_at = i + 1
    if verdict_at != len(seq):
        print(f'  ✗ 电池停充没在最后一拍触发（第 {verdict_at} 次）')
        ok = False

    # 刚插上：电池 status=0、桩还没起流 → 不能停
    det2 = StopDecider(confirm=2, warmup_min=3)
    early = None
    for mins, ts in ((1, 1000), (2, 1060), (3, 1120), (4, 1180)):
        early = early or det2.feed(pile(mins, 0.0), {'soc': 92, 'status': '0',
                                                    'current': 0.0, 'batt_ts': ts})
    if early:
        print(f'  ✗ 刚插上就判停了：{early}')
        ok = False

    # 同一份缓存（batt_ts 不变）不累计连续次数
    det3 = StopDecider(confirm=3, warmup_min=1)
    det3.feed(pile(5), {'soc': 99, 'status': '1', 'current': 5.0, 'batt_ts': 1000})
    stuck = None
    for mins in range(6, 12):
        stuck = det3.feed(pile(mins), {'soc': 100, 'status': '0', 'current': 0.0,
                                       'batt_ts': 1000})
    if stuck:
        print('  ✗ 旧缓存被重复计数了')
        ok = False

    # 电池数据缺失 → 桩侧兜底仍要能停
    det4 = StopDecider(confirm=2, warmup_min=3)
    fb = None
    for mins, cur in ((4, 5.0), (10, 5.0), (20, 3.0), (21, 0.4), (22, 0.4)):
        fb = fb or det4.feed(pile(mins, cur), None)
    if not fb or fb.get('source') != '桩侧兜底':
        print(f'  ✗ 桩侧兜底没生效：{fb}')
        ok = False

    # 2026-10-23 实盘：桩不上报读数/度数时，不能把它们当实测值
    from evwatch.tower import full_text, snap_text
    noshow = snap_from_detail({'resData': {'orderNum': 'X', 'plugNum': 'DTMF00000000', 'plugId': 1,
                                           'startTime': 1_700_000_000_000,
                                           'currentTime': 1_700_001_000_000,
                                           'current': 5.37, 'voltage': 220.0, 'power': 1180.3,
                                           'chargingDegree': 0, 'costMoney': 0.4, 'paymentMoney': 60.5},
                               'isReadCurrent': False, 'isDisplayDegree': False})
    if not noshow or noshow['read_current'] or noshow['has_degree']:
        print(f'  ✗ isReadCurrent/isDisplayDegree 没解析出来：{noshow}')
        ok = False
    txt = snap_text(noshow)
    if '5.37A' in txt or '0.00度' in txt:
        print(f'  ✗ 桩没上报时仍在展示假读数：{txt}')
        ok = False
    if '1.18kW' not in txt:
        print(f'  ✗ 桩上报的功率没被展示：{txt}')
        ok = False
    ftxt = full_text(noshow, {'source': '电池端', 'reason': '测试', 'soc': 100})
    if '充入' in ftxt and '度' in ftxt.split('充入')[1][:8]:
        print(f'  ✗ 推送里仍在编造度数：{ftxt}')
        ok = False

    # SOC 100% 但仍报 status=1、电流 0.5A（浮充）→ 也要判停
    det5 = StopDecider(confirm=2, warmup_min=3)
    tri = None
    for i, (mins, soc, cur) in enumerate(((5, 99, 16.9), (6, 100, 0.5), (7, 100, 0.45))):
        tri = tri or det5.feed(pile(mins), {'soc': soc, 'status': '1', 'current': cur,
                                            'batt_ts': 5000 + i})
    if not tri or tri.get('source') != '电池端':
        print(f'  ✗ 满电浮充没判停：{tri}')
        ok = False

    # 价值判停（2026-10-23 需求）：本段涨不了几个点就别再占一段
    from evwatch.tower import ValueStop

    def vfeed(vs, mins, soc, cost=None):
        return vs.feed({'orderNum': 'V', 'elapsed': mins * 60.0,
                        'cost': cost if cost is not None else round(mins * 0.03, 2)},
                       {'soc': soc, 'status': '1', 'current': 17.0})

    vs = ValueStop()
    vres = None
    for mins, soc in ((95, 95), (105, 95), (110, 96), (115, 96)):
        vres = vfeed(vs, mins, soc) or vres
    if not vres or vres.get('source') != '价值判停':
        print(f'  ✗ 1h55m 只剩几个点却没止损：{vres}')
        ok = False

    vs2 = ValueStop()          # 中段 1%/分：不该停
    v2 = None
    for mins, soc in ((10, 60), (20, 70), (30, 80), (40, 90)):
        v2 = vfeed(vs2, mins, soc) or v2
    if v2:
        print(f'  ✗ 中段正常充电却被价值判停：{v2}')
        ok = False

    vs3 = ValueStop()          # 95%+ 但本段能充满：不该拦
    v3 = None
    for mins, soc in ((10, 60), (20, 75), (30, 88), (40, 94), (50, 97)):
        v3 = vfeed(vs3, mins, soc) or v3
    if v3:
        print(f'  ✗ 本段能充满却被拦：{v3}')
        ok = False

    vs4 = ValueStop()          # 电量低 + 涓流：不许停
    v4 = None
    for mins, soc in ((40, 58), (50, 59), (60, 59), (70, 60)):
        v4 = vfeed(vs4, mins, soc) or v4
    if v4:
        print(f'  ✗ 低电量时被价值判停：{v4}')
        ok = False

    # 整段计费（按 30 分钟）：识别 + 只在临近边界才判跨段
    vs5 = ValueStop()
    v5 = None
    for mins in range(0, 90):
        soc = min(96, int(50 + mins * 0.77))
        cost = 0.0 if mins < 30 else (1.0 if mins < 60 else 2.0)   # 30/60 分钟各跳一整段
        v5 = vfeed(vs5, mins, soc, cost) or v5
    if vs5.unit != 30 or not vs5.block_cost:
        print(f'  ✗ 没识别出 30 分钟整段计费（unit={vs5.unit}）')
        ok = False
    elif not v5 or v5.get('source') != '价值判停':
        print(f'  ✗ 整段计费临近边界却没止损：{v5}')
        ok = False
    elif vs5.last_info and vs5.last_info['mode'] != 'block':
        print('  ✗ 整段计费模式下 info.mode 不对')
        ok = False

    vs6 = ValueStop()          # 实时计费：跳变间隔 1 分钟，不能被误判成整段
    for mins in range(30, 60):
        vfeed(vs6, mins, min(90, 50 + mins), round(mins * 0.03, 2))
    if vs6.unit:
        print(f'  ✗ 实时计费被误判成整段（unit={vs6.unit}）')
        ok = False

    # 冷启动：服务重启 / 断网很久后恢复，seen_charging 已丢，但电池已经报停且电量很高
    det7 = StopDecider(confirm=2, warmup_min=3)
    cold = None
    for mins in (5, 6):
        cold = cold or det7.feed(pile(mins, 5.0),
                                 {'soc': 99, 'status': '0', 'current': 0.0,
                                  'batt_ts': 7777, 'age': 20})
    if not cold:
        print('  ✗ 冷启动后电池已停充却不判停（会一直充到桩自停）')
        ok = False

    # 目标电量：--target-soc 90，到了就停（还在充电中也停）
    det8 = StopDecider(confirm=2, warmup_min=3, target_soc=90)
    tgt = None
    for i, soc in enumerate((88, 90, 91)):
        tgt = tgt or det8.feed(pile(5 + i), {'soc': soc, 'status': '1',
                                             'current': 16.0, 'batt_ts': 8800 + i, 'age': 20})
    if not tgt:
        print('  ✗ 到了 --target-soc 却没停')
        ok = False

    print(f'  铁塔判定：{"OK" if ok else "FAIL"}')
    return ok


def part_d() -> bool:
    """D. 铁塔登录链路的离线校验

    - 发码签名公式 md5(md5(phone)+timestamp) 的回归向量（假号码）
    - 有登录抓包时，用抓包里的手机号/时间戳复算，必须与抓包 sign 一致
    - 登录报文结构、RSA 密文长度（JSEncrypt 1024bit/PKCS1v15 → base64 172 字符）
    """
    from evwatch.tower_login import _rsa, code_sign, login_payload, send_code_payload

    ok = True
    kat = code_sign('13800138000', 1_700_000_000_000)
    if kat != '1dce0b2d367733d146fe28d91effbbf1':
        print(f'  ✗ 发码签名与回归向量不一致：{kat}')
        ok = False

    body = send_code_payload('13800138000', 1_700_000_000_000)
    if list(body) != ['cellphone', 'vailType', 'timestamp', 'sign'] or body['vailType'] != 1:
        print(f'  ✗ 发码报文结构不对：{body}')
        ok = False

    payload = login_payload('13800138000', '1234', 'openid_placeholder')
    need = ['cellphone', 'code', 'loginType', 'type', 'miniProgramOpenId',
            'invitationCode', 'appId', 'coordinateY', 'coordinateX']
    if list(payload) != need:
        print(f'  ✗ 登录报文结构与抓包不一致：{list(payload)}')
        ok = False
    if len(payload['cellphone']) != 172 or len(payload['appId']) != 172:
        print(f'  ✗ RSA 密文长度不对：{len(payload["cellphone"])}')
        ok = False

    # token 收件箱解析：手机端存什么格式都要能吃，且必须能带出 deviceId
    from evwatch.tower_login import parse_token, parse_token_file
    tok = ('eyJhbGciOiJSUzI1NiJ9.eyJ1c2VyX25hbWUiOiJ0ZXN0IiwiZXhwIjo5OTk5OTk5OTk5fQ'
           '.c2lnbmF0dXJlX3BsYWNlaG9sZGVy')
    cases = [
        f'{tok}',
        f'Bearer {tok}',
        f'Authorization: Bearer {tok}',
        f"curl 'https://x' -H 'deviceId: abc123' -H 'Authorization: Bearer {tok}'",
        f'POST /charge/core/toc/charging/list HTTP/1.1\ndeviceId: abc123\nAuthorization: Bearer {tok}\n\n{{}}',
    ]
    for text in cases:
        got, dev = parse_token_file(text)
        if got != tok:
            print(f'  ✗ 收件箱解析失败：{text[:40]}… → {got}')
            ok = False
        if 'abc123' in text and dev != 'abc123':
            print(f'  ✗ deviceId 没解析出来：{text[:40]}…')
            ok = False
    if parse_token('没有 token 的文本') is not None:
        print('  ✗ 反例被误判成 token')
        ok = False

    # 有登录抓包就用真实数据复算（抓包不入库，没有就跳过）
    raw = paths.TOWER_CAPTURE_DIR
    hit = None
    if os.path.isdir(raw):
        for name in sorted(os.listdir(raw)):
            if 'getValidateCode' not in name and not name.startswith('[611]'):
                continue
            cap = parse_capture(os.path.join(raw, name))
            try:
                obj = json.loads(cap.body)
            except Exception:
                continue
            phone, ts, sign = obj.get('cellphone'), obj.get('timestamp'), obj.get('sign')
            if phone and ts and sign:
                got = code_sign(phone, ts)
                if got != sign:
                    print(f'  ✗ 与抓包签名不一致：算出 {got} / 抓包 {sign}')
                    ok = False
                else:
                    print(f'  真实抓包：发码签名复算一致（{sign[:12]}…）')
                hit = True
                break
    if not hit:
        print('  未找到登录抓包，跳过真实签名校验（正常）')

    print(f'  铁塔登录：{"OK" if ok else "FAIL"}')
    return ok


def main() -> int:
    print('A. 离线向量测试')
    ok_a = part_a()
    print('B. 真实抓包校验')
    ok_b = part_b()
    print('C. 铁塔「电池停了就结束桩」')
    ok_c = part_c()
    print('D. 铁塔短信登录')
    ok_d = part_d()
    good = ok_a and ok_b and ok_c and ok_d
    print('结果:', 'PASS' if good else 'FAIL')
    return 0 if good else 1


if __name__ == '__main__':
    raise SystemExit(main())
