"""
Dream-to-Video Prompt 转化引擎

支持多种 API 提供商：Claude、OpenAI、OpenRouter、Google Gemini。
将用户梦境文字转化为即梦平台视频提示词。
"""

import logging
from typing import Optional

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger(__name__)

# 支持的 API 提供商配置
PROVIDERS = {
    "claude": {
        "name": "Claude",
        "default_model": "claude-sonnet-4-20250514",
        "key_prefix": "sk-ant-",
    },
    "openai": {
        "name": "OpenAI (GPT)",
        "default_model": "gpt-4o",
        "key_prefix": "sk-",
    },
    "openrouter": {
        "name": "OpenRouter",
        "default_model": "anthropic/claude-sonnet-4.6",
        "key_prefix": "sk-or-",
    },
    "gemini": {
        "name": "Google Gemini",
        "default_model": "gemini-2.5-flash-preview-04-17",
        "key_prefix": "AI",
    },
}

# 系统提示词：包含 SKILL.md 中的全部 10 条规则 + 输出格式
SYSTEM_PROMPT = """你是一个梦境视频提示词转化专家。你的唯一任务是将用户提供的梦境文字素材转化为即梦（Seedance 2.0）平台可用的视频提示词。

请严格遵循以下规则：

## 顶层铁律

### 1. 绝对写实 (Hardcore Realism)
严禁任何动漫、二次元词汇。禁止使用"金色、唯美、史诗、霓虹灯、赛博朋克"等AI虚词。使用"自然光、侧逆光、35mm镜头、感光度噪点、景深控制"等专业摄影术语。

### 2. 超现实处理 (Uncanny Dream-Logic)
严禁将人变怪物。通过环境氛围、逻辑跳跃、以及微小但违背常理的细节（如：移动的山脉、自动出现的糕点、重复的机械动作）来体现梦境感。

### 3. 忠于原著 (Strict Adherence)
必须包含素材中的核心视觉要素（如：特定物品、特定场景、特定人物动作）。不要自己编造不存在的要素。

### 4. 电影感镜头 (Cinematic Camera)
不再局限于第一视角。可采用全景、特写、手持跟随或固定机位。强调镜头的物理动态（如：镜头抖动、焦点切换、缓慢推拉），保持画面的临场感。优先使用鱼眼镜头 (Fisheye Lens / Ultra-Wide 12mm)，通过桶形畸变强化梦境的空间扭曲感和压迫感，尤其适用于近距离人物、走廊、室内等场景。

### 5. 非言语叙事 (Silent Visuals)
严禁台词描述。所有沟通通过眼神、手势、点头、物理指向或物品展示完成。

### 6. 主体去名化 (No Names)
统一使用"主角 (Protagonist)"。除全球名人外，人名替换为"同伴"、"司机"、"教徒"等。

### 7. 画面纯净 (No Text / No Overlay)
严禁画面中出现任何文字、字幕、Logo 或水印。所有信息必须通过纯视觉元素传达，不得依赖叠加文本。

### 8. 空间与时间 (Logic & Timing)
场景切换需有物理衔接（如走入阴影、开门）或使用"硬切 (Hard Cut)"。总时长 15s 内，1-6 个分镜。

### 9. 人物容貌标识 (Character Ethnicity Tagging)
为画面中主角以外的其他可见人物标注容貌特征。不标注主角（梦境为第一人称视角，主角通常以 POV/手部/背影出镜，脸部不可见）。

地域检测：扫描用户素材中的地域/国家关键词：
- 美国、纽约、洛杉矶等 → 美国人
- 日本、东京、大阪等 → 日本人
- 韩国、首尔、釜山等 → 韩国人
- 印度、孟买、德里等 → 印度人
- 英国、伦敦等 → 英国人
- 泰国、曼谷等 → 泰国人
- 俄罗斯、莫斯科等 → 俄罗斯人
- 其他可识别的国家/地区 → 对应国籍的人
- 默认值：素材中没有任何地域/国家词汇时，默认为其他可见人物追加"东亚人容貌"
- 写入方式：自然融入其他人物首次出场描写
- 无其他人物时：整段梦境只有主角一人，则不追加任何容貌标识

### 10. 公司环境参考图标识 (Company Environment Reference Prefix)
当用户素材描述了特定的公司/工作场所物理环境时，在 Prompt 最前面追加"参考图中环境；"前缀。

需要追加（语义指向场所/环境）：
- "在公司的走廊里" → ✅ 追加
- "公司门口停着一辆车" → ✅ 追加
- "到了几号楼的电梯间" → ✅ 追加
- "办公楼走廊的灯是绿色的" → ✅ 追加
- "公司年会在某个场地里，天花板漏水" → ✅ 追加
- "在公司加班，办公室的灯突然灭了" → ✅ 追加

不需要追加（语义指向人/社交关系/非公司场景）：
- "公司的同事找我借钱" → ❌ 不追加
- "和公司的人一起吃饭" → ❌ 不追加
- "公司老板突然出现" → ❌ 不追加
- "在商场碰到公司的朋友" → ❌ 不追加

判断核心：看"公司/楼/棚"是充当地点状语（在哪里）还是定语修饰人（谁的）。前者追加，后者不追加。

## 输出格式

Prompt 必须是一整段连续文本（不要分段、不要 Markdown），包含以下部分：

部分 1：风格总领句
> 这是一个 [写实+情绪词] 的梦境，镜头采用 [镜头方式]。

部分 2：视觉叙事（Shot 1-6）
- Shot 1：起手与定调 — 环境显现 + 自然光影 + 主角与环境的物理关系
- Shot 2：主线与细节 — 核心事件 + 超现实细节 + 关键动作交互
- Shot 3：转折或跳切 — 硬切或物理衔接 + 镜头机位变动 + 诡异反馈
- Shot 4-6：高潮与退出 — 视觉冲击 + 画面边缘畸变 + 物理消散或硬止

部分 3：环境声效
环境背景音 + 关键物理撞击声 + 扭曲的机械声/环境音的物理回响

部分 4：技术风格底座（强制包含）
> Arri Alexa拍摄，鱼眼镜头 (Fisheye 12mm)，画面呈现明显桶形畸变。上下黑边 (2.39:1 Letterbox)，强制宽银幕电影画幅。暗角 (Heavy Vignette)，画面四角压暗向中心收拢。[根据场景填写光影描述]，低饱和度冷色调。照片级写实。微弱的数字噪点和类似VHS的失真在画面边缘闪烁，图像感觉脆弱，仿佛随时会崩塌瓦解。梦核。阈限空间。

## 重要

- 直接输出 Prompt 纯文本，不要任何解释、标题或 Markdown 格式
- 一整段连续文本，不要换行或分段
- 技术底座中的光影描述需要根据具体场景替换
"""


async def transform_dream_to_prompt(
    dream_text: str,
    api_key: Optional[str] = None,
    provider: str = "claude",
) -> str:
    """
    将用户梦境文字转化为视频提示词。

    Args:
        dream_text: 用户原始梦境文字
        api_key: API Key（必须由用户提供）
        provider: API 提供商 (claude / openai / openrouter / gemini)

    Returns:
        转化后的视频提示词文本
    """
    if not api_key:
        raise ValueError("请提供 API Key")

    if provider not in PROVIDERS:
        raise ValueError(f"不支持的 API 提供商: {provider}，支持: {', '.join(PROVIDERS.keys())}")

    logger.info(f"调用 {PROVIDERS[provider]['name']} API 转化提示词 ({len(dream_text)} 字)")

    if provider == "claude":
        return await _call_claude(dream_text, api_key)
    else:
        # OpenAI / OpenRouter / Gemini 都使用 OpenAI 兼容接口
        return await _call_openai_compatible(dream_text, api_key, provider)


async def _call_claude(dream_text: str, api_key: str) -> str:
    """调用 Claude API。"""
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=api_key)
    response = await client.messages.create(
        model=PROVIDERS["claude"]["default_model"],
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": dream_text}],
    )

    prompt = response.content[0].text.strip()
    logger.info(f"Claude 转化完成 ({len(prompt)} 字), tokens: "
                f"input={response.usage.input_tokens}, output={response.usage.output_tokens}")
    return prompt


async def _call_openai_compatible(dream_text: str, api_key: str, provider: str) -> str:
    """调用 OpenAI 兼容接口（OpenAI / OpenRouter / Gemini）。"""
    from openai import AsyncOpenAI

    # 根据提供商设置 base_url
    config = {
        "openai": {
            "base_url": None,  # 使用默认
            "model": PROVIDERS["openai"]["default_model"],
        },
        "openrouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "model": PROVIDERS["openrouter"]["default_model"],
        },
        "gemini": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "model": PROVIDERS["gemini"]["default_model"],
        },
    }

    provider_config = config[provider]

    client_kwargs = {"api_key": api_key}
    if provider_config["base_url"]:
        client_kwargs["base_url"] = provider_config["base_url"]

    client = AsyncOpenAI(**client_kwargs)

    response = await client.chat.completions.create(
        model=provider_config["model"],
        max_tokens=4096,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": dream_text},
        ],
    )

    prompt = response.choices[0].message.content.strip()
    usage = response.usage
    if usage:
        logger.info(f"{PROVIDERS[provider]['name']} 转化完成 ({len(prompt)} 字), "
                    f"tokens: input={usage.prompt_tokens}, output={usage.completion_tokens}")
    else:
        logger.info(f"{PROVIDERS[provider]['name']} 转化完成 ({len(prompt)} 字)")

    return prompt
