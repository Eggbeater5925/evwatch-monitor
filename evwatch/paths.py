"""项目路径约定 —— 所有默认路径都以项目根为基准，不依赖当前工作目录。

整理后的目录结构：

    <root>/
      evwatch/          代码
      deploy/           部署脚本
      docs/             协议分析 / 策略文档
      examples/         *.example.json 模板
      config/           登录态与桩配置（含凭据，不入库）
      data/             CSV / 状态文件 / 日志（运行产物，不入库）
      captures/         抓包、解包产物（本地样本，不入库）

这样无论从哪个目录启动脚本、还是 launchd 直接拉起，读写位置都一致。
"""
from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CONFIG_DIR = os.path.join(ROOT, 'config')
DATA_DIR = os.path.join(ROOT, 'data')
LOG_DIR = os.path.join(DATA_DIR, 'logs')
CAPTURE_DIR = os.path.join(ROOT, 'captures')
DOCS_DIR = os.path.join(ROOT, 'docs')
EXAMPLES_DIR = os.path.join(ROOT, 'examples')

# 抓包按来源分目录，别放 captures 根下 —— 两边编号会重叠（哈啰 551~752、铁塔 278~635）
# 哈啰「小哈智电」：加密报文，dump_all / selftest 的真实数据校验用它
HELLOBIKE_CAPTURE_DIR = os.path.join(CAPTURE_DIR, '小哈智电')
# 铁塔：明文报文，tower_login / selftest 共用
TOWER_CAPTURE_DIR = os.path.join(CAPTURE_DIR, '铁塔充电_raw')


def config(name: str) -> str:
    """配置文件：config/<name>（session.json / pile.json / huiming.json …）"""
    return os.path.join(CONFIG_DIR, name)


def data(name: str) -> str:
    """运行产物：data/<name>（*.csv / .*_state.json）"""
    return os.path.join(DATA_DIR, name)


def log(name: str) -> str:
    """日志：data/logs/<name>"""
    return os.path.join(LOG_DIR, name)


def capture(*parts: str) -> str:
    """抓包/解包产物：captures/<parts>"""
    return os.path.join(CAPTURE_DIR, *parts)


def ensure_dirs() -> None:
    """确保运行期目录存在（写日志/CSV 前调用）"""
    for d in (CONFIG_DIR, DATA_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)
