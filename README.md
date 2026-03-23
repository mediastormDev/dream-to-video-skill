# Dream-to-Video Skill

[中文文档](./README.zh-CN.md)

AI agent skill that transforms dream text into cinematic videos. Describe your dream, and the agent automatically generates professional video prompts, submits them to the [Jimeng](https://jimeng.jianying.com) platform via browser automation, downloads the finished videos, and applies post-processing effects.

## How It Works

```
Dream text → AI transforms to cinematic prompt → Queue → Browser automation → Jimeng platform → Download → Post-processing → Video files
```

1. **Prompt transformation** — The AI agent converts your dream description into a detailed cinematic video prompt following 10 strict rules (photorealistic style, fisheye lens, no text overlays, silent narrative, etc.)
2. **Queue submission** — The prompt is added to a local task queue (SQLite + JSONL)
3. **Browser automation** — A background worker drives Chromium via Playwright to interact with the Jimeng video generation platform: login, submit prompts, upload reference images when needed, monitor progress
4. **Download & post-processing** — Completed videos are downloaded and automatically processed with an "Elliptic Shatter" edge effect (center-clear, shattered-glass edges), outputting both the original and the effect version

## Install

### Via skills CLI

```bash
npx skills add mediastormDev/dream-to-video-skill -s dream-to-video
```

### Manual install

Clone the repo and symlink the skill directory into your agent's skill folder:

```bash
git clone https://github.com/mediastormDev/dream-to-video-skill.git
mkdir -p ~/.claude/skills
ln -s "$(pwd)/dream-to-video-skill/skills/dream-to-video" ~/.claude/skills/dream-to-video
```

## Requirements

- Python >= 3.10
- Chromium (installed via Playwright)
- Anthropic API key (for dream-to-prompt transformation)

## Setup

After installing the skill, clone this repo to get the Python toolchain:

```bash
git clone https://github.com/mediastormDev/dream-to-video-skill.git
cd dream-to-video-skill/dream_to_video
pip install -r requirements.txt
playwright install chromium
```

Then log in to the Jimeng platform (one-time QR code scan):

```bash
python main.py login
```

## Usage

Once set up, just describe a dream to your AI agent:

> "I dreamed I was running barefoot on a coastal boardwalk surrounded by hundreds of sea lions..."

The agent will:
1. Transform your text into a cinematic video prompt
2. Submit it to the queue (`python main.py add "<prompt>"`)
3. Start the background worker (`python main.py worker`)
4. Notify you when the video is ready in `dream_to_video/output/`

### CLI Commands

| Command | Description |
|---------|-------------|
| `python main.py login` | Log in to Jimeng via QR code |
| `python main.py verify` | Check login status |
| `python main.py add "<prompt>"` | Add a prompt to the task queue |
| `python main.py worker` | Start background worker |
| `python main.py status` | View task progress |
| `python main.py generate "<prompt>"` | Single synchronous generation |
| `python main.py serve` | Start FastAPI web server |

### Output

Each task produces two video files in `dream_to_video/output/`:

- `task_XXX_YYYYMMDD_HHMMSS.mp4` — Original video
- `task_XXX_YYYYMMDD_HHMMSS_elliptic-shatter.mp4` — With shattered-glass edge effect

## License

[MIT](./LICENSE)
