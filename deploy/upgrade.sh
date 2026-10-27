#!/bin/bash
# 把本机已安装的监控服务升级到当前工作区的代码。
#
#   bash deploy/upgrade.sh              # 用当前代码重装所有已安装的服务
#   bash deploy/upgrade.sh --pull       # 先 git fetch + rebase origin/main，再重装
#   bash deploy/upgrade.sh --status     # 只看版本 / 已装服务 / 最近日志，不做任何改动
#
# 为什么不直接 `launchctl kickstart -k`：
#   1) 改了 plist 里的参数或路径（间隔、--relogin-mobile、新增的环境变量），
#      必须 bootout + bootstrap 重新读配置；kickstart 只是拿旧参数重启进程；
#   2) 多个服务互不影响，但升级时仍应逐个停、逐个装，避免同时断掉所有监控。
#
# 非 macOS（Linux / NAS）不跑 launchd，脚本只打印需要手动改的地方。
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
UID_NUM="$(id -u)"
LA="$HOME/Library/LaunchAgents"
MODE="upgrade"
PULL=""

for arg in "$@"; do
  case "$arg" in
    --pull)   PULL=1 ;;
    --status) MODE="status" ;;
    -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg"; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
ver() {
  say "代码目录：$DIR"
  if git -C "$DIR" rev-parse --git-dir >/dev/null 2>&1; then
    say "当前版本：$(git -C "$DIR" log -1 --format='%h %s' 2>/dev/null)"
    say "分支    ：$(git -C "$DIR" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    say "远端    ：$(git -C "$DIR" remote get-url origin 2>/dev/null || echo '（未设置）')"
    if [ -n "$(git -C "$DIR" status --porcelain 2>/dev/null)" ]; then
      say "⚠️  工作区有未提交改动（--pull 会失败，先 commit 或 stash）"
    fi
  else
    say "（不是 git 工作区，跳过版本信息）"
  fi
}

# ---- 非 macOS：给手工清单 ----
if [ "$(uname -s)" != "Darwin" ]; then
  ver
  say ""
  say "当前不是 macOS，install.sh 的 launchd 部分用不上。按这个形式自己写 systemd / cron："
  say ""
  say "  ExecStart=/usr/bin/python3 $DIR/evwatch/battery_watch.py watch --interval 90 --low 20"
  say "  WorkingDirectory=$DIR"
  say ""
  say "配置、数据、日志都在项目根的 config/ 和 data/ 下，默认值已经指向那里，"
  say "不用再传 --session / --csv / --state。"
  say ""
  say "改完：systemctl daemon-reload && systemctl restart <unit>"
  say "docker：挂载 $DIR/config 和 $DIR/data 两个目录即可"
  exit 0
fi

# ---- 收集已安装的服务 ----
# 用空格分隔的字符串而不是数组：macOS 自带 bash 3.2 在 set -u 下访问空数组会报 unbound variable
TARGETS=""
for t in battery tower pile huiming; do
  if [ -f "$LA/com.evwatch.$t.plist" ]; then
    TARGETS="$TARGETS${TARGETS:+ }$t"
  fi
done

ver
say ""

if [ "$MODE" = status ]; then
  if [ -z "$TARGETS" ]; then
    say "未安装任何服务（先跑 bash deploy/install.sh）"
    exit 0
  fi
  for t in $TARGETS; do
    flag="--$t"; [ "$t" = battery ] && flag=""
    say "---------- $t ----------"
    bash "$DIR/deploy/install.sh" --status $flag 2>&1 | sed 's/^/  /'
  done
  exit 0
fi

if [ -n "$PULL" ]; then
  if [ -n "$(git -C "$DIR" status --porcelain 2>/dev/null)" ]; then
    say "✗ 工作区不干净，先 commit 或 stash（升级脚本不会替你丢改动）"
    exit 1
  fi
  say "==> git fetch + rebase origin/main"
  if ! git -C "$DIR" fetch origin; then
    say "✗ fetch 失败，代码没动，服务也没动"
    exit 1
  fi
  if ! git -C "$DIR" rebase origin/main; then
    say "✗ rebase 冲突：处理完再跑本脚本（此时服务仍在跑旧代码，未受影响）"
    exit 1
  fi
  ver
  say ""
fi

if [ -z "$TARGETS" ]; then
  say "未检测到已安装的服务。首次安装用：bash deploy/install.sh [--tower|--pile|--huiming]"
  exit 0
fi

# ---- 先全停，再让 install.sh 重写 plist / 启动 ----
say "==> 停止已安装的服务：$TARGETS"
for t in $TARGETS; do
  launchctl bootout "gui/$UID_NUM/com.evwatch.$t" 2>/dev/null || true
done

FAILED=""
for t in $TARGETS; do
  flag="--$t"; [ "$t" = battery ] && flag=""
  say ""
  say "==> 重装 $t"
  if ! bash "$DIR/deploy/install.sh" $flag 2>&1 | sed 's/^/    /'; then
    FAILED="$FAILED $t"
  fi
done

say ""
say "================ 结果 ================"
for t in $TARGETS; do
  flag="--$t"; [ "$t" = battery ] && flag=""
  if launchctl print "gui/$UID_NUM/com.evwatch.$t" >/dev/null 2>&1; then
    say "  ✓ $t 在跑"
  else
    say "  ✗ $t 没起来（看 data/logs/$([ "$t" = battery ] && echo watch || echo "$t").err.log）"
  fi
done
if [ -n "$FAILED" ]; then
  say ""
  say "安装过程报错的：$FAILED"
  exit 1
fi
say ""
say "日志：tail -f $DIR/data/logs/watch.log"
