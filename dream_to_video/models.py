"""Dream-to-Video 数据模型"""

from pydantic import BaseModel, Field
from enum import Enum
from typing import Optional
from datetime import datetime


class GenerationStatus(str, Enum):
    """视频生成状态"""
    PENDING = "pending"               # 等待开始
    SUBMITTING = "submitting"         # 正在提交 Prompt
    SUBMITTED = "submitted"           # 已提交到平台，等待渲染
    GENERATING = "generating"         # 视频渲染中
    DOWNLOADING = "downloading"       # 正在下载视频
    COMPLETED = "completed"           # 生成完成
    FAILED = "failed"                 # 生成失败


class ErrorType(str, Enum):
    """错误类型"""
    SENSITIVE_CONTENT = "sensitive_content"   # 敏感词拦截
    NETWORK_ERROR = "network_error"          # 网络错误
    RENDER_TIMEOUT = "render_timeout"        # 渲染超时
    AUTH_EXPIRED = "auth_expired"            # 登录过期
    ELEMENT_NOT_FOUND = "element_not_found"  # 页面元素未找到
    UNKNOWN = "unknown"                      # 未知错误


class ProgressInfo(BaseModel):
    """进度信息"""
    status: GenerationStatus
    progress_percent: Optional[int] = None   # 0-100
    message: str = ""                        # 中文状态消息
    error_type: Optional[ErrorType] = None
    timestamp: datetime = Field(default_factory=datetime.now)


class GenerationRequest(BaseModel):
    """生成请求"""
    prompt: str                              # 视频提示词
    task_id: Optional[str] = None            # 任务 ID（不填则自动生成）


class GenerationResult(BaseModel):
    """生成结果"""
    task_id: str
    status: GenerationStatus
    video_path: Optional[str] = None         # 本地视频文件路径
    preview_image: Optional[str] = None      # 预览图路径
    prompt_used: str = ""                    # 使用的提示词
    error_message: Optional[str] = None
    error_type: Optional[ErrorType] = None
    created_at: datetime = Field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None


# ====== 批量生成模型 ======

class BatchTask(BaseModel):
    """批次中的单个任务"""
    task_id: str                              # "task_001"
    prompt: str                               # 视频提示词
    status: GenerationStatus = GenerationStatus.PENDING
    submit_order: int = 0                     # 提交顺序（0-based）
    submitted_at: Optional[datetime] = None   # 提交时间
    completed_at: Optional[datetime] = None   # 完成时间
    video_path: Optional[str] = None          # 下载的原版视频路径
    effect_video_path: Optional[str] = None   # 后处理特效视频路径
    error_message: Optional[str] = None       # 错误信息
    retry_count: int = 0                      # 审核未通过等原因的自动重试次数
    reference_image_path: Optional[str] = None  # 使用的参考图路径（Rule 10 触发时记录）


class BatchState(BaseModel):
    """
    整个批次的运行状态。
    序列化为 JSON 用于崩溃恢复和跨进程通信。
    """
    tasks: list[BatchTask] = []
    initial_video_count: int = 0              # 首次提交前页面上的视频数
    settings_configured: bool = False         # 设置是否已配置
    downloaded_video_urls: list[str] = []     # 已下载的视频 URL（防重复）
    worker_started_at: Optional[datetime] = None
