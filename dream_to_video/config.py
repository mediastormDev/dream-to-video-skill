"""Dream-to-Video 全局配置"""

import os
from pathlib import Path

# === 路径 ===
BASE_DIR = Path(__file__).parent
AUTH_FILE = BASE_DIR / "auth" / "auth.json"
USER_DATA_DIR = BASE_DIR / "auth" / "browser_profile"  # 持久化浏览器配置目录
OUTPUT_DIR = BASE_DIR / "output"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATABASE_PATH = DATA_DIR / "dream_to_video.db"

# === Anthropic API（Prompt 转化）===
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# === URL ===
JIMENG_BASE_URL = "https://jimeng.jianying.com"
JIMENG_VIDEO_URL = "https://jimeng.jianying.com/ai-tool/generate?type=video"

# === 超时（秒）===
LOGIN_TIMEOUT = 600         # 扫码登录等待时间（10 分钟）
GENERATION_TIMEOUT = 1800   # 视频渲染最大等待时间（30 分钟，Seedance 2.0 + 15s 可能较慢）
POLL_INTERVAL = 2.0         # 进度轮询间隔
HEARTBEAT_INTERVAL = 600    # 心跳检测间隔（每 10 分钟打印一次状态 + 检查页面健康）
PAGE_LOAD_TIMEOUT = 30      # 页面加载超时

# === 重试 ===
MAX_RETRIES = 3
RETRY_BASE_DELAY = 5        # 指数退避基准（5s → 10s → 20s）
MAX_MODERATION_RETRIES = 2  # 审核未通过时最大自动重试次数

# === Worker（批量模式）===
WORKER_POLL_INTERVAL = 10    # Worker 主循环间隔（秒）
GLOBAL_TIMEOUT = 14400       # Worker 全局超时：4 小时
SUBMIT_INTERVAL = 5.0        # 提交间隔（秒），每个 prompt 之间等待
PROMPT_QUEUE_FILE = OUTPUT_DIR / "prompt_queue.jsonl"
BATCH_STATE_FILE = OUTPUT_DIR / "batch_state.json"
PROCESSED_IDS_FILE = OUTPUT_DIR / "processed_ids.txt"  # 已提交任务的轻量级备份（防状态损坏重复提交）

# === 参考图（Reference Images）===
REFERENCE_IMAGE_BASE_DIR = Path(os.environ.get(
    "REFERENCE_IMAGE_DIR",
    str(BASE_DIR / "reference_images")
))
REFERENCE_IMAGE_INDOOR_DIR = REFERENCE_IMAGE_BASE_DIR / "室内"
REFERENCE_IMAGE_OUTDOOR_DIR = REFERENCE_IMAGE_BASE_DIR / "室外"
REFERENCE_IMAGE_PREFIX = "参考图中环境；"

# 室内/室外场景分类关键词
INDOOR_KEYWORDS = [
    "走廊", "办公室", "电梯", "楼梯", "会议室", "大厅", "大堂",
    "室内", "工位", "茶水间", "卫生间", "前台", "车间", "仓库",
    "棚", "楼道", "过道", "食堂", "天花板", "灯管", "房间",
    "屋内", "门口", "格子间", "写字楼", "办公楼",
]
OUTDOOR_KEYWORDS = [
    "室外", "停车场", "广场", "花园", "天台", "楼顶",
    "外墙", "大门外", "院子", "绿化", "草坪", "外景",
]

# === 浏览器 ===
HEADLESS = os.environ.get("HEADLESS", "false").lower() in ("true", "1", "yes")
SLOW_MO = 100                # 操作间隔毫秒数（防检测）
VIEWPORT = {"width": 1280, "height": 800}

# === Web 服务 ===
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
VIDEO_RETENTION_DAYS = int(os.environ.get("VIDEO_RETENTION_DAYS", "7"))
