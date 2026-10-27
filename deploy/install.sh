#!/bin/bash
# 用 macOS 自带的 launchd 把监控挂成常驻服务（开机自启、崩溃自动重启）
#
#   bash deploy/install.sh                          安装电量监控（只监控，不自动续期）
#   bash deploy/install.sh --relogin 13800138000    安装 + 开启自动续期（iCloud 取码）
#   bash deploy/install.sh --interval 180           改轮询间隔（秒，默认 90）
#   bash deploy/install.sh --tower                  安装「铁塔充满自动结束」服务（独立进程）
#   bash deploy/install.sh --tower --dry-run        同上，但只判定不停（先观察一单）
#   bash deploy/install.sh --tower --start          只加载已生成的配置（注册/启动服务）
#   bash deploy/install.sh --pile                   安装「麦希充充电桩功率监控」服务（独立进程）
#   bash deploy/install.sh --huiming                安装「荟铭充电桩监听 + 充满自动停」服务（独立进程）
#   bash deploy/install.sh --status                 看状态（是否在跑 / 是否开了续期）
#   bash deploy/install.sh --fg                     前台跑，Ctrl+C 停（带 caffeinate 防睡眠）
#   bash deploy/install.sh --uninstall              停止并移除
#
# 其它参数：
#   --code-dir PATH   验证码落地目录，默认 iCloud 的 evwatch/
#   --code-file PATH  单文件模式（快捷指令固定写同一个文件时用）
#   --code-cmd CMD    用命令取码（邮件/Bark/机器人等）
#   --mobile MOBILE   等同于 --relogin
#
# 四个服务是分开的（com.evwatch.battery / .tower / .pile / .huiming），互不影响：
#   电量监控挂了不影响铁塔结束，铁塔 token 过期也不影响电量推送，充电桩同理。
#
# 注意：需要在「终端」里执行（某些受沙箱限制的环境里 launchctl bootstrap 会报 EIO）

set -uo pipefail

TARGET="battery"        # battery | tower | pile | huiming（--tower / --pile / --huiming 切换）
DRY_RUN=""              # tower 专用：只判定不停
DIR="$(cd "$(dirname "$0")/.." && pwd)"
UID_NUM="$(id -u)"

# 解释器怎么选（这一步以前踩过坑）：
#   `command -v python3` 在装了 Xcode 的机器上会解析到
#   /Applications/Xcode.app/Contents/Developer/usr/bin/python3 —— 它在 launchd / 终端里
#   读 ~/Documents 下的脚本会报 "Operation not permitted"（TCC），服务直接 exit 2。
#   所以：先看环境变量，再看现有电池服务已经在用的那个，最后才兜底 command -v。
pick_python() {
  local cands=() c
  [ -n "${PYTHON:-}" ] && cands+=("$PYTHON")
  # 复用已经在跑的服务用的那个解释器（换解释器时不必重新找一遍）
  local existing
  existing="$HOME/Library/LaunchAgents/com.evwatch.battery.plist"
  if [ -f "$existing" ]; then
    c="$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$existing" 2>/dev/null || true)"
    [ -n "$c" ] && cands+=("$c")
  fi
  for c in "$HOME/.workbuddy/binaries/python/envs"/*/bin/python \
           "$HOME/.workbuddy/binaries/python/versions"/*/bin/python3 \
           /opt/homebrew/bin/python3 /usr/local/bin/python3 \
           "$(command -v python3 2>/dev/null)"; do
    [ -n "$c" ] && [ -x "$c" ] && cands+=("$c")
  done
  for c in "${cands[@]}"; do
    # 真正的判据：这个 python 能不能读到本项目的脚本（TCC 就是在这一步翻车的）
    if "$c" -c 'import sys;open(sys.argv[1]).read(1)' "$DIR/evwatch/battery_watch.py" >/dev/null 2>&1; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

# 终端输出不出现完整手机号（规则与 evwatch/redact.py 的 phone() 一致）
mask_phone() {
  local p="${1:-}"
  if [ ${#p} -ge 7 ]; then
    printf '%s****%s\n' "${p:0:3}" "${p: -4}"
  else
    printf '%s\n' "$p"
  fi
}

PY="$(pick_python)" || {
  echo "找不到能用的 python3（要求能读取 $DIR 下的脚本，且装了 pycryptodome）"
  echo "可以手动指定：PYTHON=/path/to/python3 bash deploy/install.sh ..."
  exit 1
}

# 出口：默认直连（国内域名，代理反而会把证书换掉导致验不过）
# 需要强制走代理时：EVWATCH_PROXY=http://127.0.0.1:7890（脚本/服务都认）

MODE=install
RELOGIN_MOBILE="${RELOGIN_MOBILE:-}"
INTERVAL="${INTERVAL:-90}"
CODE_DIR="${CODE_DIR:-$HOME/Library/Mobile Documents/com~apple~CloudDocs/evwatch}"
CODE_DIR_SET=""   # 用户是否显式指定过（铁塔用它区分「默认值」和「我要用目录模式」）
CODE_FILE="${CODE_FILE:-}"
CODE_CMD="${CODE_CMD:-}"

usage() {
  sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) MODE=uninstall; shift ;;
    --status)    MODE=status;    shift ;;
    --start)     MODE=start;     shift ;;
    --fg)        MODE=fg;        shift ;;
    --tower)     TARGET=tower;   shift ;;
    --pile)      TARGET=pile;    shift ;;
    --huiming)   TARGET=huiming; shift ;;
    --dry-run)   DRY_RUN="--dry-run"; shift ;;
    --print-args) MODE=print-args; shift ;;
    --relogin|--mobile) RELOGIN_MOBILE="$2"; shift 2 ;;
    --relogin=*|--mobile=*) RELOGIN_MOBILE="${1#*=}"; shift ;;
    --interval)  INTERVAL="$2";  shift 2 ;;
    --interval=*)  INTERVAL="${1#*=}"; shift ;;
    --code-dir)  CODE_DIR="$2"; CODE_DIR_SET=1; shift 2 ;;
    --code-dir=*)  CODE_DIR="${1#*=}"; CODE_DIR_SET=1; shift ;;
    --code-file) CODE_FILE="$2"; shift 2 ;;
    --code-file=*) CODE_FILE="${1#*=}"; shift ;;
    --code-cmd)  CODE_CMD="$2";  shift 2 ;;
    --code-cmd=*)  CODE_CMD="${1#*=}"; shift ;;
    -h|--help)   usage; exit 0 ;;
    *) echo "未知参数：$1"; echo; usage; exit 2 ;;
  esac
done

case "$TARGET" in
  tower) LABEL="com.evwatch.tower"
         LOG_FILE="$DIR/data/logs/tower.log"
         ERR_FILE="$DIR/data/logs/tower.err.log" ;;
  pile)  LABEL="com.evwatch.pile"
         LOG_FILE="$DIR/data/logs/pile.log"
         ERR_FILE="$DIR/data/logs/pile.err.log" ;;
  huiming) LABEL="com.evwatch.huiming"
         LOG_FILE="$DIR/data/logs/huiming.log"
         ERR_FILE="$DIR/data/logs/huiming.err.log" ;;
  *)     LABEL="com.evwatch.battery"
         LOG_FILE="$DIR/data/logs/watch.log"
         ERR_FILE="$DIR/data/logs/watch.err.log" ;;
esac
# --start / --status 的提示语里要带上切换参数
TARGET_FLAG=""
[ "$TARGET" != battery ] && TARGET_FLAG=" --$TARGET"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# 配置 / 数据 / 日志目录（paths.py 里也是同一套约定，这里只是提前建好）
mkdir -p "$DIR/config" "$DIR/data" "$DIR/data/logs"

# 现有 plist 里的参数读出来（防止「只想改个代理/间隔」却把自动续期参数弄丢）
plist_arg() {
  [ -f "$PLIST" ] || return 1
  grep -q -- "$1" "$PLIST" || return 1
  grep -A1 -- "$1" "$PLIST" | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/'
}

if [ -z "$RELOGIN_MOBILE" ]; then
  RELOGIN_MOBILE="$(plist_arg 'relogin-mobile' || true)"
fi
[ -z "$CODE_CMD" ] && CODE_CMD="$(plist_arg 'code-cmd' || true)"
[ -z "$CODE_FILE" ] && CODE_FILE="$(plist_arg 'code-file' || true)"
if [ -z "$CODE_DIR_SET" ]; then
  _cd="$(plist_arg 'code-dir' || true)"
  [ -n "$_cd" ] && { CODE_DIR="$_cd"; CODE_DIR_SET=1; }
fi

if [ "$TARGET" = tower ]; then
  # 铁塔：以电池端为准结束桩侧计费。判停/推送/续期参数都有安全默认值，这里只定几个关要的
  ARGS=("$DIR/evwatch/tower.py" watch
        --session "$DIR/config/tower_session.json"
        --haha-session "$DIR/config/session.json"
        --csv "$DIR/data/tower_charge.csv"
        --state "$DIR/data/.tower_state.json"
        --interval 60 --battery-interval 60 --idle-interval 120)
  [ -n "$DRY_RUN" ] && ARGS+=("$DRY_RUN")
  if [ -n "$RELOGIN_MOBILE" ]; then
    ARGS+=(--relogin-mobile "$RELOGIN_MOBILE")
    if [ -n "$CODE_CMD" ]; then
      ARGS+=(--code-cmd "$CODE_CMD")
    elif [ -n "$CODE_FILE" ]; then
      ARGS+=(--code-file "$CODE_FILE")
    elif [ -n "$CODE_DIR_SET" ]; then
      ARGS+=(--code-dir "$CODE_DIR")
    fi   # 都不给就用脚本默认：iCloud/verify_code.txt
  fi
elif [ "$TARGET" = pile ]; then
  # 麦希充：只读轮询；token 是 7 天 JWT，靠 settoken 手动续，这里不传续期参数
  ARGS=("$DIR/evwatch/pile_watch.py" watch
        --config "$DIR/config/pile.json"
        --csv "$DIR/data/pile.csv"
        --state "$DIR/data/.pile_state.json"
        --interval 300 --interval-active 60)
elif [ "$TARGET" = huiming ]; then
  # 荟铭：监听 + 功率判满 + closeCharge 自动结束；token 是 UUID，settoken 后需重启
  ARGS=("$DIR/evwatch/huiming_watch.py" watch
        --config "$DIR/config/huiming.json"
        --csv "$DIR/data/huiming.csv"
        --state "$DIR/data/.huiming_state.json"
        --interval 300 --interval-active 60)
else
  ARGS=("$DIR/evwatch/battery_watch.py" watch
        --session "$DIR/config/session.json"
        --csv "$DIR/data/battery.csv"
        --state "$DIR/data/.charge_state.json"
        --interval "$INTERVAL" --low 20)

  if [ -n "$RELOGIN_MOBILE" ]; then
    ARGS+=(--relogin-mobile "$RELOGIN_MOBILE")
    if [ -n "$CODE_CMD" ]; then
      ARGS+=(--code-cmd "$CODE_CMD")
    elif [ -n "$CODE_FILE" ]; then
      ARGS+=(--code-file "$CODE_FILE")
    else
      mkdir -p "$CODE_DIR"
      ARGS+=(--code-dir "$CODE_DIR")
    fi
  fi
fi

case "$MODE" in
  --uninstall|uninstall)
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "已停止并移除：$LABEL"
    exit 0 ;;
  start|--start)
    if [ ! -f "$PLIST" ]; then
      echo "还没有配置文件：$PLIST"
      echo "先跑：bash deploy/install.sh$TARGET_FLAG"
      exit 2
    fi
    if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
      echo "已经在跑："
      launchctl print "gui/$UID_NUM/$LABEL" | grep -E "state =|pid ="
      exit 0
    fi
    if launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
      sleep 2
      launchctl print "gui/$UID_NUM/$LABEL" | grep -E "state =|pid =" || true
      echo "已启动。日志：tail -f $LOG_FILE"
      exit 0
    fi
    echo "⚠️  launchctl 拒绝加载（当前环境受沙箱限制）。在「终端」里执行："
    echo "    launchctl bootstrap gui/$UID_NUM \"$PLIST\""
    exit 1 ;;
  status|--status)
    echo "目标：$LABEL"
    if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
      launchctl print "gui/$UID_NUM/$LABEL" | grep -E "state =|pid =|last exit"
    elif [ -f "$PLIST" ]; then
      echo "服务：已配置，但**还没加载**（在「终端」里执行下面这条才会跑起来）"
      echo "    launchctl bootstrap gui/$UID_NUM \"$PLIST\""
    else
      echo "服务：未安装（先跑 bash deploy/install.sh$TARGET_FLAG）"
    fi
    if [ -f "$PLIST" ]; then
      if [ "$TARGET" = tower ]; then
        # 铁塔不需要 --relogin-mobile：session 里有手机号 + open_id 就默认兜底
        if grep -q "no-relogin" "$PLIST"; then
          echo "自动续期：已关闭（plist 里带 --no-relogin）"
        else
          echo "自动续期：默认开启（303 时自动发码）"
        fi
        if grep -q 'relogin-mobile' "$PLIST"; then
          local_mobile="$(grep -A1 'relogin-mobile' "$PLIST" | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/')"
          src_desc="plist 的 --relogin-mobile"
        elif [ -f "$DIR/config/tower_session.json" ]; then
          local_mobile="$("$PY" -c 'import json,sys;print(json.load(open(sys.argv[1])).get("mobile","") or "")' "$DIR/config/tower_session.json" 2>/dev/null || true)"
          src_desc="tower_session.json 的 mobile"
        else
          local_mobile=""
          src_desc="无"
        fi
        echo "手机号：$(mask_phone "$local_mobile")（来源：${src_desc}）"
      elif [ "$TARGET" = huiming ]; then
        echo "自动续期：无（token 是 UUID，没有 exp；失效后重新抓包 settoken，需重启服务）"
        if [ -f "$DIR/config/huiming.json" ]; then
          echo "token   ：$("$PY" -c 'import json,sys
d=json.load(open(sys.argv[1])); print("obtained_at=" + str(d.get("obtained_at")))' "$DIR/config/huiming.json" 2>/dev/null || echo '?')"
        fi
      elif [ "$TARGET" = pile ]; then
        echo "自动续期：无（token 是 7 天 JWT；抓包后跑 pile_watch.py settoken 手动续，进程会热加载）"
        if [ -f "$DIR/config/pile.json" ]; then
          echo "token   ：$("$PY" -c 'import json,sys,datetime
d=json.load(open(sys.argv[1])); print("obtained_at=" + str(d.get("obtained_at")))' "$DIR/config/pile.json" 2>/dev/null || echo '?')"
        fi
      elif grep -q "relogin-mobile" "$PLIST"; then
        echo "自动续期：已开启（$(mask_phone "$(grep -A1 'relogin-mobile' "$PLIST" | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/')")）"
      else
        echo "自动续期：未开启"
      fi
      if [ "$TARGET" = tower ]; then
        grep -q "dry-run" "$PLIST" && echo "动作：dry-run（只判定，不停桩）" || echo "动作：会真的结束桩侧订单"
      fi
      echo "解释器：$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$PLIST" 2>/dev/null || echo '?')"
      echo "出口：$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:EVWATCH_PROXY' "$PLIST" 2>/dev/null || echo '直连')"
      echo "配置文件：$PLIST"
    fi
    if [ -f "$LOG_FILE" ]; then
      echo "---- 最近日志 ----"
      tail -5 "$LOG_FILE"
    fi
    exit 0 ;;
  fg|--fg)
    echo "前台运行 ${LABEL}（Ctrl+C 停止）。caffeinate 会在运行期间阻止系统睡眠。"
    [ -n "$RELOGIN_MOBILE" ] && echo "自动续期：已开启（$(mask_phone "$RELOGIN_MOBILE")）"
    exec caffeinate -is "$PY" "${ARGS[@]}" ;;
  print-args|--print-args)
    echo "$LABEL"
    printf '%q ' "$PY" "${ARGS[@]}"; echo
    echo "出口：直连（EVWATCH_PROXY 可强制代理）"
    echo "日志：$LOG_FILE"
    exit 0 ;;
esac

# 把 PY + ARGS 渲染成 plist 的 <string> 序列（路径里有中文/空格都没问题）
ARGS_XML=""
for a in "$PY" "${ARGS[@]}"; do
  ARGS_XML="${ARGS_XML}    <string>${a}</string>
"
done


cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$ARGS_XML  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_FILE</string>
  <key>StandardErrorPath</key><string>$ERR_FILE</string>
</dict>
</plist>
PLIST_EOF

echo "配置已写入：$PLIST"
echo "解释器：$PY"
echo "出口：直连（要强制代理就加环境变量 EVWATCH_PROXY）"
if [ -n "$RELOGIN_MOBILE" ]; then
  echo "自动续期：已开启（$(mask_phone "$RELOGIN_MOBILE")）"
  if [ -n "$CODE_CMD" ]; then echo "  取码方式：命令 $CODE_CMD"
  elif [ -n "$CODE_FILE" ]; then echo "  取码方式：文件 $CODE_FILE"
  elif [ -n "$CODE_DIR" ]; then echo "  取码方式：目录 $CODE_DIR"
  else echo "  取码方式：脚本默认（iCloud/verify_code.txt）"; fi
  echo "  ⚠️ 前提：手机快捷指令要能把验证码写进上面这个位置，否则会白发短信"
else
  if [ "$TARGET" = tower ]; then
    echo "自动续期：用 session 里的 phone/open_id（303 时自动发码；取不到码只告警）"
  elif [ "$TARGET" = huiming ]; then
    echo "自动续期：无。token 是 UUID：抓包后跑"
    echo "  $PY $DIR/evwatch/huiming_watch.py settoken \"<token>\""
    echo "  写 huiming.json；这个进程不会热加载，改完重启服务。"
  elif [ "$TARGET" = pile ]; then
    echo "自动续期：无。token 是 7 天 JWT：抓包后跑"
    echo "  $PY $DIR/evwatch/pile_watch.py settoken \"<token>\""
    echo "  写 pile.json 即可，正在跑的进程会热加载，不用重启。"
  else
    echo "自动续期：未开启（要开：bash deploy/install.sh --relogin 你的手机号）"
  fi
fi
if [ "$TARGET" = tower ]; then
  if [ -n "$DRY_RUN" ]; then
    echo "动作：dry-run —— 只判定、只推送，不会真的结束桩"
  else
    echo "动作：电池端一停就调 endCharge（会真的结束桩侧订单）"
  fi
fi

# 已经加载过就不再 bootout（会真的掐掉正在跑的服务，而且在这个环境里会被拦），
# 改成提示用户手动重载
if launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
  echo
  echo "⚠️  服务已在运行，配置改动不会自动生效。手动重载："
  echo "    launchctl bootout gui/$UID_NUM/$LABEL; launchctl bootstrap gui/$UID_NUM \"$PLIST\""
  exit 0
fi

if launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>/dev/null; then
  sleep 2
  launchctl print "gui/$UID_NUM/$LABEL" | grep -E "state =|pid =" || true
  echo "启动成功。日志：tail -f $LOG_FILE"
else
  echo
  echo "⚠️  launchctl 拒绝加载（多半是当前环境受沙箱限制）。请在「终端」里执行："
  echo "    launchctl bootstrap gui/$UID_NUM \"$PLIST\""
  echo "  或者先这样跑起来看效果："
  echo "    bash $DIR/deploy/install.sh --fg$TARGET_FLAG${RELOGIN_MOBILE:+ --relogin $(mask_phone "$RELOGIN_MOBILE")}"
  exit 1
fi
