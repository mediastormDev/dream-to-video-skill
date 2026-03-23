# Dream-to-Video Skill

将梦境文字自动转化为电影级视频的 AI agent 技能。只需描述你的梦境，agent 会自动生成专业视频提示词、通过浏览器自动化提交到[即梦](https://jimeng.jianying.com)平台、下载成品视频并叠加后处理特效。

## 工作原理

```
梦境文字 → AI 转化为电影级提示词 → 任务队列 → 浏览器自动化 → 即梦平台 → 下载 → 后处理 → 视频文件
```

1. **提示词转化** — AI agent 按照 10 条严格规则（照片级写实、鱼眼镜头、无文字叠加、非言语叙事等）将梦境描述转化为详细的电影级视频提示词
2. **队列提交** — 提示词被加入本地任务队列（SQLite + JSONL）
3. **浏览器自动化** — 后台 Worker 通过 Playwright 驱动 Chromium 与即梦视频生成平台交互：登录、提交提示词、按需上传参考图、监控生成进度
4. **下载与后处理** — 生成完成的视频自动下载，并叠加「椭圆破碎」边缘特效（中心清晰、边缘碎玻璃质感），同时输出原版和特效版

## 安装

### 通过 skills CLI

```bash
npx skills add mediastormDev/dream-to-video-skill -s dream-to-video
```

### 手动安装

克隆仓库并将 skill 目录软链接到你的 agent 技能目录：

```bash
git clone https://github.com/mediastormDev/dream-to-video-skill.git
mkdir -p ~/.claude/skills
ln -s "$(pwd)/dream-to-video-skill/skills/dream-to-video" ~/.claude/skills/dream-to-video
```

## 环境要求

- Python >= 3.10
- Chromium（通过 Playwright 安装）
- Anthropic API Key（用于梦境转提示词）

## 配置

安装技能后，克隆本仓库以获取 Python 工具链：

```bash
git clone https://github.com/mediastormDev/dream-to-video-skill.git
cd dream-to-video-skill/dream_to_video
pip install -r requirements.txt
playwright install chromium
```

然后登录即梦平台（仅需一次二维码扫码）：

```bash
python main.py login
```

## 使用

配置完成后，直接向 AI agent 描述你的梦境即可：

> "我梦到自己赤脚在海滨步道上奔跑，周围全是密密麻麻的海狮..."

Agent 会自动：
1. 将文字转化为电影级视频提示词
2. 提交到任务队列（`python main.py add "<提示词>"`）
3. 启动后台 Worker（`python main.py worker`）
4. 视频生成完毕后通知你，文件保存在 `dream_to_video/output/`

### CLI 命令

| 命令 | 说明 |
|------|------|
| `python main.py login` | 扫码登录即梦平台 |
| `python main.py verify` | 检查登录状态 |
| `python main.py add "<提示词>"` | 添加提示词到任务队列 |
| `python main.py worker` | 启动后台 Worker |
| `python main.py status` | 查看任务进度 |
| `python main.py generate "<提示词>"` | 单次同步生成 |
| `python main.py serve` | 启动 FastAPI Web 服务 |

### 输出

每个任务在 `dream_to_video/output/` 下生成两个视频文件：

- `task_XXX_YYYYMMDD_HHMMSS.mp4` — 原版视频
- `task_XXX_YYYYMMDD_HHMMSS_elliptic-shatter.mp4` — 椭圆破碎边缘特效版

## 许可证

[MIT](./LICENSE)
