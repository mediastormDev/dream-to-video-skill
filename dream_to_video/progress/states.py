"""状态枚举与中文消息映射"""

from models import GenerationStatus

# 各状态对应的中文消息
STATUS_MESSAGES = {
    GenerationStatus.PENDING: "等待开始...",
    GenerationStatus.SUBMITTING: "正在提交 Prompt...",
    GenerationStatus.GENERATING: "视频渲染中...",
    GenerationStatus.DOWNLOADING: "正在下载视频...",
    GenerationStatus.COMPLETED: "视频生成完成!",
    GenerationStatus.FAILED: "生成失败",
}
