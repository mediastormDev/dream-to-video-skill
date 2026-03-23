"""
参考图（Reference Image）逻辑模块。

当 Rule 10 触发（prompt 以 "参考图中环境；" 开头）时：
1. 分析 prompt 判断室内/室外
2. 从对应文件夹随机选取参考图
"""

import random
from pathlib import Path
from typing import Optional

from config import (
    REFERENCE_IMAGE_PREFIX,
    REFERENCE_IMAGE_INDOOR_DIR,
    REFERENCE_IMAGE_OUTDOOR_DIR,
    INDOOR_KEYWORDS,
    OUTDOOR_KEYWORDS,
)


def needs_reference_image(prompt: str) -> bool:
    """检查 prompt 是否触发 Rule 10（以参考图前缀开头）"""
    return prompt.startswith(REFERENCE_IMAGE_PREFIX)


def classify_scene(prompt: str) -> str:
    """
    分析 prompt 判断场景是室内还是室外。

    扫描关键词计数，室外词多于室内词时返回 '室外'，
    否则默认返回 '室内'（因为 Rule 10 触发场景多为公司/办公环境）。

    Returns: '室内' or '室外'
    """
    indoor_count = sum(1 for kw in INDOOR_KEYWORDS if kw in prompt)
    outdoor_count = sum(1 for kw in OUTDOOR_KEYWORDS if kw in prompt)

    if outdoor_count > indoor_count:
        return "室外"
    return "室内"  # 默认


def select_reference_image(scene_type: str) -> Optional[Path]:
    """
    从对应文件夹随机选取一张参考图。

    scene_type: '室内' or '室外'
    Returns: 图片文件路径，文件夹为空或不存在时返回 None。
    """
    folder = (
        REFERENCE_IMAGE_INDOOR_DIR if scene_type == "室内"
        else REFERENCE_IMAGE_OUTDOOR_DIR
    )

    if not folder.exists():
        return None

    images = list(folder.glob("*.jpg")) + list(folder.glob("*.png"))
    if not images:
        return None

    return random.choice(images)
