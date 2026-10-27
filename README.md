# evwatch-monitor · 电巡

**多品牌电动车 / 电池 / 充电桩监控 · 非官方**

盯电池（电量、充电状态、电气参数）、盯充电桩（有没有真的在充、充满自动停），
异常和里程碑推送到钉钉 / 企业微信；macOS 上一条命令装成常驻服务。

| 数据源 | 入口 | 能做什么 |
|---|---|---|
| 哈啰「小哈智电」换电电池 | `evwatch/battery_watch.py` | 电量/充电状态轮询、里程碑推送、登录态自动续期 |
| 中国铁塔充电桩 | `evwatch/tower.py` | 电池端一停就 `endCharge`，不再白付服务费 |
| 麦希充充电桩 | `evwatch/pile_watch.py` | 「计费了但功率 0W」告警、判满自动结束 |
| 荟铭充电桩 | `evwatch/huiming_watch.py` | 实时功率判满、`closeCharge` 自动结束 |

设计上不绑定品牌：每个来源一个模块 + 一份配置，独立进程、独立 CSV，互不影响。

---

## ⚠️ 免责声明

1. **仅限本人账号、本人电池 / 订单。** 不含绕过登录、批量获取他人数据、破解付费功能的能力，也不接受这类 PR。
2. 协议信息来自对公开客户端（微信小程序）的静态分析；小程序著作权归各平台所有。
3. 仓库**不含**小程序安装包、反编译源码、抓包原文、真实凭据 —— 示例配置里全是占位符。
4. 接口可能变更，高频请求可能触发风控。**使用风险自负**，作者不对账号被封、数据错误、设备损坏负责。
5. 请勿商用，请勿传播他人的登录凭据。

## 快速开始

```bash
git clone <this-repo> && cd evwatch-monitor
python3 -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple pycryptodome
```

**1. 登录态** —— `config/session.json` 需要 `token` + `ticket`

```bash
cp examples/session.example.json config/session.json
# 编辑 config/session.json 填 token / ticket；或者直接短信登录（自动写入）：
python3 evwatch/login.py relogin --mobile 13800138000
```

抓包取法：手机连代理（Proxyman / Charles），打开小程序，在 `api.hellobike.com` 的
`user.account.weixinEasyLogin`（一键登录）或 `weixinMobileLogin` 响应里取 `token` / `ticket`。

**2. 验证**

```bash
python3 evwatch/battery_watch.py ping    # 登录态 + 名下电池
python3 evwatch/battery_watch.py once    # 一份完整快照
```

**3. 开跑**

```bash
python3 evwatch/battery_watch.py watch --interval 90 --low 20
```

## 常驻运行（macOS）

```bash
bash deploy/install.sh                        # 电池监控（开机自启、崩溃重启）
bash deploy/install.sh --relogin 13800138000  # 顺带开启登录态自动续期
bash deploy/install.sh --tower                # 铁塔自动结束（独立进程）
bash deploy/install.sh --pile                 # 麦希充（独立进程）
bash deploy/install.sh --huiming              # 荟铭（独立进程）
bash deploy/install.sh --status               # 状态 + 最近 5 行日志
bash deploy/install.sh --fg                   # 前台跑（caffeinate 防睡眠）
bash deploy/install.sh --uninstall
```

四个服务互不牵连（`com.evwatch.battery` / `.tower` / `.pile` / `.huiming`）：
电量推送挂了不影响结束桩，铁塔 token 过期也不影响电量监控。

合盖会暂停 —— launchd 进程在系统睡眠时冻结。要 7×24 得用常开机器，或 `--fg`。

## 升级

```bash
bash deploy/upgrade.sh --status   # 看版本 / 装了哪些服务 / 在不在跑
bash deploy/upgrade.sh --pull     # 拉代码 + 逐个重装
bash deploy/upgrade.sh            # 代码已拉好，只重装
```

plist 里存的是绝对路径，所以**改了参数或路径必须重装**（`bootout` + `bootstrap`）；
`launchctl kickstart -k` 只会拿旧 plist 重启进程。脚本先停服务再装，逐个报告结果。

Linux / NAS：在非 macOS 上 `upgrade.sh` 会打印 systemd / docker 的等价写法。

## 通知配置

钉钉机器人建议用「自定义关键词」填 **电池**（本项目所有文案都含这两个字，`notify.py`
也会在缺失时自动补前缀）；用「加签」就把密钥填 `dingtalk_secret`。

```json
{
  "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_TOKEN",
  "dingtalk_secret": "SEC..."
}
```

## 数据源

### 哈啰「小哈智电」换电电池

`batteryLife` + 充电状态 + 实时电流/电压/温度/单体电压；80 / 90 / 100% 与「充电结束」
各推一条，不刷屏；按剩余容量 ÷ 电流估充满时间；只在非充电时提醒低电量。

`token` 长期不变，**会过期的是 `ticket`**（实测 ≥4 小时，上限未知），失效返回 `code=103`。

```bash
python3 evwatch/login.py relogin --mobile 13800138000        # 手动续，一次一条短信
python3 evwatch/login.py icloud-setup                        # 生成 iPhone 快捷指令配置
python3 evwatch/login.py relogin --mobile 13800138000 \
  --code-dir "$HOME/Library/Mobile Documents/com~apple~CloudDocs/evwatch"
```

发码接口有频控，**别把续期当心跳** —— 确认 `ping` 报 103 再续。

报文加密、签名算法、字段表与踩坑记录见 **[小哈智电-接口分析.md](docs/小哈智电-接口分析.md)**。

### 中国铁塔充电桩

桩按时长收服务费，自己的「充满自停」靠不住：**电池 BMS 已经报「不在充电」，
桩还在继续计时吃服务费**（实测多吃近一小时）。所以盯电池端，一停就调 `endCharge`。

```bash
cp examples/tower_session.example.json config/tower_session.json   # 填 Authorization
python3 evwatch/tower.py status            # 在充吗？充到哪了
python3 evwatch/tower.py sim               # 离线看判定（不联网）
python3 evwatch/tower.py watch --dry-run   # 只判不停，先观察一单
python3 evwatch/tower.py watch             # 真跑：电池停 → 结束桩 → 推送
python3 evwatch/tower.py stop              # 人工立刻结束当前这单
```

判据以电池端为准：`chargeStatus` 由 1 变 0/-1 连续 2 次；或 100% 且电流 ≈0（浮充）。
防误杀：开充 3 分钟内不判、必须先见过 `chargeStatus=1`、同一份电池缓存不重复计数。
`--smart-stop`（默认开）还会按「元/%」止损 —— 末端一小时只涨几个点却要付一块多时提前收。

登录态是 JWT（约半年），**抓包优先**，被挤下线（`code=303`）才走短信兜底：

```bash
python3 evwatch/tower_login.py use-capture   # 从抓包抠 Authorization
python3 evwatch/tower_login.py token-inbox   # iPhone 存到 iCloud/evwatch/tower_token.txt
python3 evwatch/tower_login.py whoami        # token 还剩多久
```

`deviceId` 必须和 token 配对，iPhone 上用 Proxyman「Copy as cURL」整段存最稳。
登录后别在手机上再开小程序 —— 单会话。

收费模型、功率档位、实盘时间线见 **[铁塔-充电策略.md](docs/铁塔-充电策略.md)**。

### 麦希充充电桩

桩已经计费、但充电器没插好或被拔掉 —— 到点发现钱花了、一度电没充上。
接口不给 SOC 但给**实时功率**：功率长期 0W 而订单还在跑，立刻推一条。

```bash
cp examples/pile.example.json config/pile.json   # 填 checkPhoneNumber 响应里的 data.token
python3 evwatch/pile_watch.py ping               # 验 token + 最近一单
python3 evwatch/pile_watch.py status             # 现在在不在充
python3 evwatch/pile_watch.py sim                # 离线看判满逻辑（不联网）
python3 evwatch/pile_watch.py watch --interval 300 --interval-active 60
```

只监听、不发起充电。判满三档：桩报充满自停 → 功率有载转空载持续 10 分钟 →
`--max-hours` / `--max-money` 兜底，命中就 `stopOrder`。token 是 7 天 JWT，
`config/session.json` 里有 `mobile` 就会自动重登。

接口、计费模型、安全问题见 **[麦希充-接口分析.md](docs/麦希充-接口分析.md)**。

### 荟铭充电桩

比麦希充多给**实时功率**和**已充电量**，判满主要靠功率：SOC ≥ 90%（读 `data/battery.csv`）、
功率从峰值降 10% 持续 2 分钟、购买时长剩 60 秒 —— 命中就 `closeCharge`。

```bash
cp examples/huiming.example.json config/huiming.json
python3 evwatch/huiming_watch.py ping            # 验 token + 账号 + 在不在充
python3 evwatch/huiming_watch.py settoken "<profile 响应里的 data.userinfo.token>"
```

token 是 UUID，没有 exp 也没有登录接口，失效只能重新抓包（这个进程不热加载，改完要重启）。

计费与策略见 **[荟铭-充电策略.md](docs/荟铭-充电策略.md)**，
接口见 **[荟铭-接口分析.md](docs/荟铭-接口分析.md)**。

## 目录结构

```
evwatch-monitor/
├── evwatch/      代码（paths.py 定义所有路径，redact.py 负责输出脱敏）
├── deploy/       install.sh 安装 / upgrade.sh 升级
├── docs/         协议分析与充电策略
├── examples/     *.example.json 模板（复制到 config/ 再填）
├── config/       登录态与桩配置        ← 不入库
├── data/         CSV / 状态文件 / logs  ← 不入库
└── captures/     抓包原文与解包产物      ← 不入库
```

后三个目录只跟踪一个 `.gitkeep` 占位，保证 clone 下来目录结构完整；里面的内容一律忽略。

默认路径全部由 `evwatch/paths.py` 以**项目根绝对路径**拼出，不依赖当前工作目录，
从任何目录执行或由 launchd 直接拉起都一致。新增品牌按同一约定加文件即可：

```
evwatch/<brand>.py          接口封装
evwatch/<brand>_watch.py    轮询 / 判据 / 推送（复用 net / notify / csvsink / paths）
examples/<brand>.example.json
config/<brand>.json         本地填凭据
data/<brand>.csv            data/.<brand>_state.json
```

## 常见问题

**日志一片空白？**
Python 写文件是块缓冲，本项目已开行缓冲并加了启动横幅 + 心跳。仍为空就看 `data/logs/watch.err.log`。

**服务报 `SSL: CERTIFICATE_VERIFY_FAILED`？**
launchd 不继承 shell 的代理设置，`net.py` 是**直连优先**（国内域名，直连最干净）。
代理会把证书链换成它自己的 CA，Python 反而验不过。要强制走代理设
`EVWATCH_PROXY=http://127.0.0.1:7890`；想禁止降级不校验 TLS 就设 `EVWATCH_TLS_STRICT=1`。

**改完代码怎么生效？**
`bash deploy/upgrade.sh`。只改 `.py` 也可以用 `launchctl kickstart -k gui/501/com.evwatch.battery`；
但改了 plist 里的参数或路径，必须重装。

**小程序里刷新了，为什么数据还是旧的？**
小程序的「刷新」只是请服务端下发查询指令；**电池休眠时不应答，服务端返回缓存**。
静置时电池约 20 分钟才上报一次，这是电池侧决定的。

## 许可

MIT（见 [LICENSE](LICENSE)）。免责声明是本许可的一部分。
