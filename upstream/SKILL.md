---
name: html-ppt-video
description: Use when converting a document or article into a narrated HTML presentation video with synchronized Chinese TTS audio and subtitles. Triggers when user mentions "PPT转视频", "文档转视频", "生成讲解视频", "配音幻灯片", "把PPT做成带配音的视频", "文档做成视频", or wants to turn any written content into a narrated slide video with voiceover. Also use when user has an existing HTML PPT and wants to add narration and export as video.
---

# HTML PPT Video

将文档转为带中文配音和字幕的讲解视频。

## Dependencies

| 依赖 | 用途 | 安装 |
|------|------|------|
| edge-tts | TTS 语音合成 | `pip install edge-tts` |
| ffmpeg + ffprobe | 视频处理 | `brew install ffmpeg` |
| Google Chrome | headless 渲染幻灯片 PNG | 已安装即可 |
| Noto Sans SC 字体 | 中文字幕渲染 | `brew install font-noto-sans-sc`（或系统安装） |
| html-ppt skill | Phase 1 制作 HTML PPT | `npx skills add https://github.com/lewislulu/html-ppt-skill -y -g` |
| render.sh | html-ppt 附带的渲染脚本 | 随 html-ppt skill 自动安装 |

运行 `python build_video.py` 时会自动检查以上依赖，缺失会报错并给出安装命令。

## Workflow

### Phase 1: 文档 → HTML PPT

**调用 html-ppt skill**，按以下步骤将文档转为 HTML 演示文稿：

**1. 分析文档，规划幻灯片结构**

阅读文档，提取核心内容，规划每页幻灯片。典型结构：

| 页码 | 类型 | 内容 |
|------|------|------|
| 1 | 封面 | 标题 + 副标题 + 作者/日期 |
| 2 | 路线图 | 议程/大纲概览 |
| 3-N-2 | 内容页 | 按文档逻辑分段，每页一个核心观点 |
| N-1 | 要点回顾 | 关键 takeaways |
| N | 结尾 | Q&A / 感谢页 |

目标页数 10-16 页（对应 3-5 分钟视频）。

**2. 选择主题和模板**

根据内容类型选择（调用 html-ppt skill 后按 T 可实时预览切换）：
- 技术分享 / 工程内容 → `tech-sharing` 全 deck 模板 + `tokyo-night` / `dracula` 主题
- 商务汇报 → `corporate-clean` 主题
- 产品发布 → `pitch-deck-vc` 主题
- 学术报告 → `academic-paper` 主题
- 知识架构 → `knowledge-arch-blueprint` 模板

**3. 制作 PPT**

使用 html-ppt skill 的全 deck 模板作为起点：
```bash
~/.claude/skills/html-ppt/scripts/new-deck.sh my-talk
```

**4. 确认页数**

最终确认幻灯片总页数 N（HTML 中 `<section class="slide">` 的数量）。

### Phase 1.5: PPT 视觉确认（重要）

HTML PPT 制作完成后，**必须暂停**，让用户确认视觉效果：

1. **用浏览器打开 HTML 文件**：`open <deck-path>/index.html`
2. **告知用户**：PPT 已生成，请在浏览器中检查效果，确认以下内容：
   - 整体风格和主题是否满意
   - 是否需要切换主题（按 T 键可实时切换）
   - 幻灯片内容和排版是否需要调整
   - 页数是否合适
3. **等待用户确认后**，再进入 Phase 2

这一步非常重要，避免在用户不满意的 PPT 上生成语音和视频，浪费计算时间。

### Phase 2: 生成确认文档

运行 `python build_video.py review`，脚本会自动完成：
1. 渲染幻灯片为 PNG
2. 生成 `video-output/review.md` 确认文档（包含每页的图片 + 解说词）

确认文档格式：

````markdown
---
voice: zh-CN-YunxiNeural
rate: "+5%"
style: 口语化
skip: []
---

<!--
说明：
  - 每个 `# N · Title` 下有一个 `- [ ]` checkbox
  - 取消勾选改为 `- [x]` 表示跳过该页（不生成语音和视频）
  - 解说词在 code block 中，可直接编辑
  - YAML 块中可修改 voice、rate、style 等参数
  - 确认无误后告知继续
-->

# 1 · Cover

- [ ] **跳过此页**

![slide](slides/index_01.png)

```
大家好，今天来聊聊如何用 MCP 构建能够触达生产系统的智能体。
```

# 2 · Agenda

- [ ] **跳过此页**

![slide](slides/index_02.png)

```
分享分为六个部分。先看三条连接路径的对比...
```

# 3 · 可跳过的页面

- [x] **跳过此页**

![slide](slides/index_03.png)

```
这页内容可以跳过...
```
````

确认文档包含：
- **YAML 块**：`voice`（语音）、`rate`（语速）、`style`（语言风格）、`skip`（全局跳过的页码列表）
- **每页**：页码 + 标题 + checkbox + 幻灯片图片 + code block 包裹的解说词
- **Checkbox**：`- [ ]` 表示保留，`- [x]` 表示跳过该页
- **解说词**：用 code block（` ``` `）包裹，便于识别和编辑

### Phase 3: 用户确认

**把 `review.md` 展示给用户**，引导用户：

1. 修改 YAML 块中的参数（换语音、调语速等）
2. 直接编辑 code block 中的解说词
3. 勾选 `- [x]` 跳过不需要的页面
4. 确认无误后告知继续

### Phase 4: 解析确认文档 + 生成视频

用户确认后，运行 `python build_video.py build`，脚本会：

1. **解析 `review.md`** — 读取 YAML 参数和解说词，识别 `- [x]` 跳过的页面
2. **生成语音** — 对非跳过页面运行 edge-tts，生成 mp3 + srt
3. **合并字幕** — 基于音频时长偏移合并 SRT
4. **生成视频片段** — ffmpeg 用 `-t` 精确控制每段时长（不用 `-shortest`）
5. **拼接 + 烧字幕** — 最终输出 `final-video.mp4`

也可以单独重跑某个步骤：
```bash
python build_video.py build          # 全部 5 步
python build_video.py build --step 3  # 仅步骤 3（生成视频片段）
```

## Critical Pitfalls

### ffmpeg 时间漂移（最重要）

`-loop 1` 静态图 + `-shortest` 会因帧对齐导致每段多 ~2s，累积后字幕完全错位。

必须用 `-t <exact_audio_duration>` 精确控制每段时长，不依赖 `-shortest`。

### 字幕偏移计算

合并 SRT 时，时间偏移基于 ffprobe 获取的音频文件时长累加，不基于 SRT 结束时间。

### 中文 ASS 字幕样式

SRT 转 ASS 后需替换默认样式（Noto Sans SC，字号 12，白字黑边）。ffmpeg subtitles filter 的路径需绝对路径且冒号转义：`path.replace(":", "\\:")`。

## Output Structure

```
project/
├── build_video.py
└── video-output/
    ├── slides/           # N 个 PNG (1920x1080)
    ├── review.md         # 用户确认文档（核心）
    ├── narrations/       # txt 文件
    ├── audio/            # mp3 + srt
    ├── segments/         # mp4 片段
    ├── subtitles/        # combined.srt + combined.ass
    └── final-video.mp4   # 最终视频
```

## Voice Options

| 语音 ID | 性别 | 风格 |
|---------|------|------|
| zh-CN-YunxiNeural | 男 | 沉稳（推荐技术分享） |
| zh-CN-XiaoxiaoNeural | 女 | 亲切自然 |
| zh-CN-YunjianNeural | 男 | 磁性浑厚 |
| zh-CN-XiaoyiNeural | 女 | 活泼明快 |

## Troubleshooting

| 问题 | 原因 | 解决 |
|------|------|------|
| 后半段字幕不同步 | -shortest 漂移 | 改用 -t 精确时长 |
| 字幕字体太大 | ASS 默认字号 | 改 Style 行字号 |
| 中文方块 | 缺中文字体 | 用 Noto Sans SC |
| ffmpeg 路径报错 | 冒号未转义 | replace(":", "\\:") |
| 换模板后视频没变 | build 未重新渲染 | 先 render 或跑 review 再 build |
