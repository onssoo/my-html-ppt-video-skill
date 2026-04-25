# html-ppt-video-skill

English | **[中文](README.md)**

Convert documents into narrated HTML presentation videos with synchronized Chinese TTS audio and subtitles. Works with [html-ppt-skill](https://github.com/lewislulu/html-ppt-skill).

## Features

- Document → HTML PPT → narrated video with subtitles, fully automated
- Supports 10-16 slide PPTs, producing 3-5 minute videos
- Chinese TTS voice synthesis (edge-tts, 4 voice options)
- User confirmation flow: PPT visual check → narration review → skip page selection
- ffmpeg `-t` precise duration control, zero subtitle drift

## Installation

```bash
# Prerequisites
pip install edge-tts
brew install ffmpeg
brew install font-noto-sans-sc

# Install html-ppt skill
npx skills add https://github.com/lewislulu/html-ppt-skill -y -g

# Install this skill (Claude Code)
# Clone this repo to ~/.claude/skills/html-ppt-video/
```

## Usage

In Claude Code:

```
/html-ppt-video path/to/your-document.md 把文档转为视频
```

Or let Claude use this skill directly. Trigger phrases: `PPT转视频`, `文档转视频`, `生成讲解视频`, etc.

## Workflow

```
Phase 1    Document → HTML PPT (calls html-ppt skill)
Phase 1.5  Browser visual check ← user confirms PPT appearance
Phase 2    Generate review.md (slides + narrations + checkboxes)
Phase 3    User confirmation ← edit narrations, check pages to skip
Phase 4    build → audio + subtitles + video segments → final video
```

## Using build_video.py Standalone

```bash
# Copy build_video.py to your project, modify the constants at the top, then:

python build_video.py check             # Check dependencies
python build_video.py review            # Render slides + generate confirmation doc
python build_video.py build             # Parse confirmation doc + generate video
python build_video.py build --step 3    # Re-run a specific step only
```

## Configuration

Edit in `review.md`:

```yaml
voice: zh-CN-YunxiNeural   # Voice (see options below)
rate: "+5%"                 # Speech rate adjustment
style: 口语化               # Language style
skip: []                    # Globally skipped page numbers
```

Each page's narration is wrapped in a code block for easy editing. Check `- [x]` to skip unwanted pages.

## Voice Options

| Voice ID | Gender | Style |
|----------|--------|-------|
| zh-CN-YunxiNeural | Male | Steady (recommended for tech talks) |
| zh-CN-XiaoxiaoNeural | Female | Warm and natural |
| zh-CN-YunjianNeural | Male | Deep and resonant |
| zh-CN-XiaoyiNeural | Female | Bright and lively |

## Output Structure

```
project/
├── build_video.py
└── video-output/
    ├── slides/           # PNG slide images
    ├── review.md         # User confirmation document
    ├── narrations/       # Narration txt files
    ├── audio/            # mp3 + srt files
    ├── segments/         # mp4 segments
    ├── subtitles/        # combined.srt + combined.ass
    └── final-video.mp4   # Final video
```

## Dependencies

| Dependency | Purpose |
|------------|---------|
| edge-tts | TTS voice synthesis |
| ffmpeg + ffprobe | Video processing |
| Google Chrome | Headless slide PNG rendering |
| Noto Sans SC | Chinese subtitle font |
| html-ppt skill | HTML PPT authoring |

## License

MIT
