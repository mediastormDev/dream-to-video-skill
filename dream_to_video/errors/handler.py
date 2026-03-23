"""错误检测、分类与重试策略"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import ErrorType
from config import MAX_RETRIES, RETRY_BASE_DELAY


class ErrorHandler:
    """错误处理器：分类错误并决定重试策略"""

    # 致命错误：不重试
    FATAL_ERRORS = {
        ErrorType.SENSITIVE_CONTENT,
        ErrorType.AUTH_EXPIRED,
    }

    # 可重试错误：指数退避
    RETRYABLE_ERRORS = {
        ErrorType.NETWORK_ERROR,
        ErrorType.RENDER_TIMEOUT,
        ErrorType.ELEMENT_NOT_FOUND,
        ErrorType.UNKNOWN,
    }

    # 错误关键词映射
    ERROR_KEYWORDS = {
        ErrorType.SENSITIVE_CONTENT: [
            "敏感", "违规", "审核", "不合规", "内容安全",
            "无法生成", "涉及", "禁止", "违法",
        ],
        ErrorType.NETWORK_ERROR: [
            "网络", "超时", "连接", "network", "timeout",
            "断开", "失败", "请稍后",
        ],
        ErrorType.AUTH_EXPIRED: [
            "登录", "过期", "未登录", "请先登录",
            "身份验证", "认证",
        ],
    }

    @staticmethod
    def classify_error(error_text: str) -> ErrorType:
        """根据错误文本分类错误类型"""
        text_lower = error_text.lower()
        for error_type, keywords in ErrorHandler.ERROR_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return error_type
        return ErrorType.UNKNOWN

    @staticmethod
    def should_retry(error_type: ErrorType, attempt: int) -> bool:
        """判断是否应该重试"""
        if error_type in ErrorHandler.FATAL_ERRORS:
            return False
        return attempt < MAX_RETRIES

    @staticmethod
    def get_retry_delay(attempt: int) -> float:
        """计算重试等待时间（指数退避：5s → 10s → 20s）"""
        return RETRY_BASE_DELAY * (2 ** attempt)

    @staticmethod
    def get_error_message(error_type: ErrorType) -> str:
        """获取用户友好的错误说明"""
        messages = {
            ErrorType.SENSITIVE_CONTENT: "提示词包含敏感内容，请修改后重试",
            ErrorType.NETWORK_ERROR: "网络连接不稳定，正在重试...",
            ErrorType.RENDER_TIMEOUT: "视频渲染超时，可能是服务器繁忙",
            ErrorType.AUTH_EXPIRED: "登录已过期，请运行: python main.py login",
            ErrorType.ELEMENT_NOT_FOUND: "页面元素未找到，可能需要更新选择器",
            ErrorType.UNKNOWN: "未知错误",
        }
        return messages.get(error_type, "未知错误")
