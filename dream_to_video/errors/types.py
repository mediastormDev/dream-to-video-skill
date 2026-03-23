"""自定义异常类"""

from models import ErrorType


class DreamToVideoError(Exception):
    """基础异常"""
    def __init__(self, message: str, error_type: ErrorType = ErrorType.UNKNOWN):
        super().__init__(message)
        self.error_type = error_type


class SensitiveContentError(DreamToVideoError):
    """敏感词拦截"""
    def __init__(self, message: str = "内容包含敏感词，无法生成"):
        super().__init__(message, ErrorType.SENSITIVE_CONTENT)


class AuthExpiredError(DreamToVideoError):
    """登录过期"""
    def __init__(self, message: str = "登录已过期，请重新运行: python main.py login"):
        super().__init__(message, ErrorType.AUTH_EXPIRED)


class RenderTimeoutError(DreamToVideoError):
    """渲染超时"""
    def __init__(self, message: str = "视频渲染超时，请稍后重试"):
        super().__init__(message, ErrorType.RENDER_TIMEOUT)


class ElementNotFoundError(DreamToVideoError):
    """页面元素未找到"""
    def __init__(self, message: str = "页面元素未找到，可能需要更新选择器"):
        super().__init__(message, ErrorType.ELEMENT_NOT_FOUND)
