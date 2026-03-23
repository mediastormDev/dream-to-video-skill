"""
即梦页面 DOM 选择器 - 集中管理

基于 2026-02-27 实际页面检查结果更新。
当即梦更新 UI 时，只需要修改这个文件。
"""


class JimengSelectors:
    """即梦视频生成页面的所有选择器"""

    # ========== 输入区域（已确认 2026-02-27）==========
    # 主输入框：textarea + input 双重备选
    # prompt-container-SvZ73x 包含 textarea 和 input
    PROMPT_INPUT = (
        'textarea.prompt-textarea-l5tJNE, '
        'textarea.lv-textarea, '
        'input.prompt-input-w0wBdF, '
        'textarea[placeholder*="输入文字"], '
        'input[placeholder*="输入文字"]'
    )

    # ========== 生成按钮（已确认 2026-02-27）==========
    # 即梦有两个 submit-button：一个 collapsed（不可见），一个是真正的生成按钮
    # 必须排除 collapsed 按钮，否则 .first 会选到不可见的那个
    GENERATE_BUTTON = (
        'button[class*="submit-button"]:not([class*="collapsed"]), '
        'button.submit-button-CpjScj'
    )

    # 生成按钮的 Loading/Disabled 状态
    GENERATE_BUTTON_LOADING = (
        'button[class*="submit-button"][disabled], '
        'button[class*="submit-button"][class*="disabled"], '
        'button[class*="submit-button"][class*="loading"]'
    )

    # ========== 进度指示器 ==========
    PROGRESS_BAR = (
        '[role="progressbar"], '
        '[class*="progress-bar"], '
        '[class*="progressBar"], '
        '[class*="progress"][class*="inner"]'
    )

    PROGRESS_TEXT = (
        '[class*="progress-text"], '
        '[class*="progress-percent"], '
        '[class*="percent"]'
    )

    LOADING_INDICATOR = (
        '[class*="loading"], '
        '[class*="spinner"], '
        '[class*="generating"], '
        '.lottie, '
        'svg[class*="loading"]'
    )

    # ========== 生成结果（已确认 2026-02-27 DOM 检查）==========
    # 即梦使用 virtual list 渲染结果，关键结构:
    #   virtual-list-gUs6jj (可滚动虚拟列表容器)
    #     └── record-list-container-YQhwuM (共享单个容器，不是每个结果一个！)
    #         └── content-DPogfx ai-generated-record-content-hg5EL8 (每个生成结果一个！)
    #             └── video-record-nlt6eI
    #                 ├── record-header-E91Dfj → record-header-content-Lkk9CM (prompt 文本)
    #                 └── record-box-wrapper-rmKP8n
    #                     └── video-record-content-JwGmeX → video[src]

    VIDEO_RESULT = (
        'video[src], '
        'video source[src], '
        '[class*="video-card"] video, '
        '[class*="record"] video'
    )

    # 共享容器 — 注意：即梦虚拟列表中只有一个，不是每个结果一个！
    RECORD_LIST_CONTAINER = '[class*="record-list-container"]'

    # 单个生成结果卡片 — 每个生成任务对应一个，包含 prompt 文本 + 视频
    GENERATION_RESULT_CARD = '[class*="ai-generated-record-content"]'

    # 单个视频卡片
    VIDEO_CARD = '[class*="video-card-wrapper"]'
    VIDEO_CARD_GRID = '[class*="responsive-video-grid"]'

    DOWNLOAD_BUTTON = (
        'button:has-text("下载"), '
        'a[download], '
        '[class*="download"] button, '
        'button[class*="download"], '
        'a:has-text("下载")'
    )

    PREVIEW_IMAGE = (
        '[class*="preview"] img, '
        '[class*="thumbnail"] img, '
        'video[poster]'
    )

    # ========== 错误状态 ==========
    ERROR_TOAST = (
        '[class*="toast"][class*="error"], '
        '[class*="message"][class*="error"], '
        '[class*="notification"][class*="error"], '
        '.lv-notification--error'
    )

    SENSITIVE_CONTENT_ALERT = (
        ':text("敏感"), '
        ':text("违规"), '
        ':text("内容审核"), '
        ':text("不合规"), '
        ':text("无法生成")'
    )

    # 审核未通过标识（生成后在结果卡片上显示）
    MODERATION_FAILED = (
        ':text("审核未通过"), '
        ':text("审核失败"), '
        ':text("未通过审核"), '
        ':text("内容违规")'
    )

    ERROR_DIALOG = (
        '[class*="modal"][class*="error"], '
        '[class*="dialog"][class*="error"]'
    )

    # ========== 工具栏设置（已确认 2026-02-27）==========
    # 即梦工具栏使用 Lark Design 的 lv-select 下拉组件
    # 工具栏结构: toolbar-tBNbB3 > toolbar-settings-YNMCja > toolbar-settings-content-uImXGN
    TOOLBAR_CONTENT = '.toolbar-settings-content-uImXGN'
    TOOLBAR_ALL_SELECTS = '.toolbar-settings-content-uImXGN .lv-select'
    TOOLBAR_TYPE_SELECT_CLASS = 'type-select'       # 类型选择器（"视频生成"）的特征 class
    TOOLBAR_FEATURE_SELECT_CLASS = 'feature-select'  # 功能选择器（"首尾帧"）的特征 class
    TOOLBAR_RATIO_BUTTON = 'button.toolbar-button-FhFnQ_'

    # lv-select 下拉弹出选项
    LV_SELECT_OPTION = '[role="option"], .lv-select-option'
    LV_SELECT_POPUP = '[role="listbox"]'

    # ========== 参考图上传（Reference Image Upload，2026-03-01 DOM 检查确认）==========
    #
    # 流程：feature-select 切到「全能参考」→ 参考区域变为单个上传槽 → 上传图片
    # 清理：切回「首尾帧」（默认模式）
    #
    # 关键结构：
    #   feature-select-VcsuXi (工具栏功能选择器，lv-select)
    #     └── 下拉选项：全能参考 / 首尾帧(默认) / 智能多帧 / 主体参考
    #   references-vWIzeo (参考图区域)
    #     └── reference-group-_DAGw1
    #         └── reference-item-aI97LK
    #             └── reference-upload-h7tmnr (点击弹出文件选择器)
    #                 ├── svg (+ 图标)
    #                 ├── label-O_5YLx (标签文字)
    #                 └── input.file-input-OfqonL (hidden file input)

    # 功能选择器（feature-select），用 _click_lv_select_option 切换模式
    REFERENCE_FEATURE_SELECT_CLASS = 'feature-select'

    # 参考图上传区域（点击弹出文件选择器，或用 file input）
    REFERENCE_UPLOAD_AREA = '[class*="reference-upload"]'

    # 隐藏的 <input type="file">（直接 set_input_files）
    REFERENCE_FILE_INPUT = 'input.file-input-OfqonL'

    # 已上传的参考图缩略图（上传后出现在 reference-item 中）
    REFERENCE_IMAGE_THUMBNAIL = '[class*="reference-item"] img, [class*="reference-upload"] img'

    # 参考图上的删除/关闭按钮（hover 缩略图后出现）
    REFERENCE_IMAGE_REMOVE = '[class*="reference-item"] [class*="close"], [class*="reference-item"] [class*="delete"], [class*="reference-item"] [class*="remove"]'

    # ========== 认证状态 ==========
    LOGIN_PROMPT = (
        '[class*="login-modal"], '
        '[class*="login-dialog"], '
        '[class*="qr-code"], '
        '[class*="sign-in"], '
        'img[src*="qrcode"]'
    )

    USER_AVATAR = (
        '[class*="avatar"], '
        '[class*="user-info"], '
        'img[src*="avatar"]'
    )
