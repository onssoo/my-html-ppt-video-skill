# 路线 B：已有 PPTX / PDF → import

> 从 SKILL.md 拆出，按需读。命令入口：`ppt2video.py import <文件>`。

```bash
PY=~/.venv-mlx-audio/bin/python

# 1. 导入：渲染截图 + 生成 slides.json
$PY ~/ppt2video.py import old/融资路演.pptx

# 2. 检查 slides/ 下的截图；在 slides.json 里设置每页 sec / disc / notes
#    （财务页和结尾页设 "disc": true；没有备注的页补 notes）

# 3. 写解说词（图表多、文字少的话加 --vision，需多模态模型）
$PY ~/ppt2video.py narrate old/融资路演.pptx --source source/report.docx --brief brief.md

# 4. 出片（旧幻灯片建议在 review.md 开头设 frame: letterbox）
$PY ~/ppt2video.py build old/融资路演.pptx
```

- **PDF**：PyMuPDF 按 1920×1080 的画布逐页渲染成 PNG，同时提取每页文字。PDF 里没有演讲者备注。
- **PPTX**：python-pptx 提取标题、文字、表格、图表里的数据和**演讲者备注**（备注当"讲述要点"用；
  旧 PPT 的备注常常就是当年的讲稿，是很好的素材）。截图优先用**同名且更新的 PDF**；
  没有才调 LibreOffice 转换。
- **`slides.json` 可以手工编辑**：每页的 `sec`（目标秒数）、`disc`（是否要免责说明，**默认关闭**）、
  `notes`（讲述要点）都能改。这三项对应 HTML 流程里的 `data-sec`、`data-disclaimer` 和 `notes`，
  解说词规范照样生效。**重新导入时会保留你改过的这三项。**
- 输出目录：HTML 用 `video-output/`，PPTX/PDF 用 `<文件名>-video/`，同一目录放多份幻灯片不会互相覆盖。
- 页数**不用手工确认**，`import` 会打印；PPTX 的隐藏页自动跳过。

#### 还原度：PPTX 最好自己先导出 PDF

用 PowerPoint 或 Keynote 导出 PDF，放在同一目录、**使用同名文件**，脚本会直接用。LibreOffice 转换有三个常见问题：

- **字体被替换**：Mac 上没有微软雅黑、等线这类字体时，LibreOffice 会换成别的字体，导致换行和排版变形。
- **部分元素显示异常**：SmartArt、某些图表样式和特效可能显示不正确。
- **分步动画全部叠在一起**：PDF 只有静态画面，原本分步出现、或者出现后又消失的元素，会同时显示在同一页上。

所以导入后**一定要打开 `slides/` 目录，逐页检查一遍截图**。

#### 几点说明

- **旧稿里的数字要先进口径表**：旧 PPT 里的数字可能和现在的源文档不一致（做 PPT 时还是老版本的预测）。
  画面已经无法修改，解说词又必须和画面一致，所以先核对，把两边的差异在 `brief.md` 的口径表里讲清楚。
- **备注已经是成稿的情况**：如果某份 PPT 的备注本身就是写好的讲稿，`narrate` 会把它当作讲述要点**重新改写**。
  想直接用原文需要另加 `--from-notes`（**尚未实现**，需要时提）。
- **PDF 必须是幻灯片版式**：A4 竖版的报告类 PDF 也能导入，但画面会是左右留大片空白的竖版页面，不适合做视频。

### 路线 A · Phase 1: 文档 → HTML PPT

调用 **html-ppt skill** 制作。要点：

1. **规划结构**：封面 → 路线图 → 内容页（每页一个核心观点）→ 要点回顾 → 结尾
   - 先按 **business-deck-spec.md §一** 定场景（受众/用途/时长/主线）→ 写 `大纲.md` → **Gate 1 审过才做 slides**
   - 版式从 `html-ppt-skill/references/layouts.md` 的骨架里选（`ppt2video.py deck-skeleton 大纲.md` 可生成骨架）
   - 做完跑 `check_deck.py`（规范 §七：逐页几何/字号/填充率检查，无 ERROR 才进 Gate 2）
2. **选模板**：`~/html-ppt-skill/templates/full-decks/` 下有 15 套完整模板
   - 技术分享 → `tech-sharing`；商务汇报 → `corporate-clean`（主题）
   - 知识架构 → `knowledge-arch-blueprint`；课程 → `course-module`
3. **新建 deck**：`~/html-ppt-skill/scripts/new-deck.sh my-talk`
4. **页数不用手工确认** —— 脚本自动识别 `<section class="slide">` 的数量

**⚠️ 结构要求**（已用真实模板验证）：
```html
<section class="slide" data-title="Objectives">      ← 类名必须是 slide
<section class="slide full" data-title="Cover">      ← full 等附加类无妨
```
运行时按 **`#/N`（1-based）** 深链翻页 —— 与脚本默认 `--hash-start 1` 一致。

**解说词相关属性**：
- `data-sec="50"`：本页解说词的目标秒数（用于字数下限与"偏短"告警）。
- **免责语默认关闭**：deck 不要主动加免责页，解说词也不要写任何免责/声明/风险提示内容（含各种变体）。
  只有 owner 明确要求时才用 `--disclaimer` 打开；打开后 `data-disclaimer` 才被认作免责页，
  措辞固定为 `references/narration-spec.md` 第八节那一句，且只出现在这些页。
- `<div class="notes">`：本页的讲述要点（解说词的依据，也参与数字溯源）。

### Phase 1.5: PPT 视觉确认（不可跳过）

```bash
open <deck-path>/index.html
```
让用户确认风格、主题（按 `T` 实时切换）、排版、页数。**确认后再进 Phase 2** ——
避免在不满意的 PPT 上白跑配音。

### Phase 2: 截图 + 生成确认文档

```bash
PY=~/.venv-mlx-audio/bin/python
$PY ~/ppt2video.py review deck/index.html
```
产出 `deck/video-output/`：
- `slides/001.png …`（Edge 无头截图，1920×1080；编号 **3 位**，>99 页不乱序）
- `outline.md`（提取出的每页文字）
- `review.md`（**核心确认文档**）

### Phase 2.5: 让 LLM 写解说词（逐页）

```bash
$PY ~/ppt2video.py narrate deck/index.html --source 原文.md --brief deck/brief.md
```
- **逐页生成**（不再整篇一次）—— 整篇模式会让模型套"承接→结论→引出"的固定结构，写出电报体碎句。
- LLM 走 **OpenAI 兼容端点**，temperature 0.5。端点**不写进仓库**，按优先级取：
  `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` 环境变量 → 本地配置 `~/.config/mhpvs/local.json`
  （`{"llm_base": "http://<你的网关>:9000/v1", "llm_model": "main"}`）。都没配时命令会直接报错并提示怎么配。
- `--source`：原始文档，切成块后按相关性喂给每页，做**数字溯源**（解说词里的数字必须能在画面/要点/源文档里找到）。
- `--brief`：场景卡（brief.md），提供语气、主线、**口径表**（关键数字唯一写法）、术语表。
- 语气规范见 **`references/narration-spec.md`**（正式陈述、无自问自答、无套话、无俚语、无画面指代；**免责默认不加**）。
- 每页过 **lint**（字数下限、问句、套话、碎句、禁用符号、**免责语（默认出现即不合格）**、数字溯源），不合格自动重写（最多 2 次）。
- 产出 `narrations.json`，并**自动同步 `review.md`**（保留原配置头），可直接 `build`，无需再跑 `review --reset`。

### Phase 3: 用户确认（改 review.md）

`review.md` 顶部是 **YAML 配置块，这才是配置的真源**（改脚本里的 `DEFAULTS` 会被它覆盖）：

```yaml
ref_audio: <参考录音的绝对路径>     # 参考录音（克隆音色）
ref_text: <参考录音的逐字文本>                  # 必须与录音一字不差
instruct:                                # ⚠️ 克隆模式下无效，见「坑」
speed: 1.15                              # ✅ 基线（ffmpeg atempo 后处理）
subtitles: burn                          # burn 烧录 / soft 软字幕 / off
```

每页：`# N · 标题` + `- [ ]` checkbox + 图片 + ``` 代码块里的解说词 ```。
勾成 `- [x]` 跳过该页。

### Phase 4: 出片

```bash
$PY ~/ppt2video.py build deck/index.html
```
1. 一页的所有句子一次 `batch_generate`（共享参考条件 → 句间音色稳）
2. 每句独立音频 → 字幕起止时间 = **真实音频时长**（不是估算）
3. **TTS 读口语、字幕用书面**：解说词按书面写（`40%`、`120ms`），`speak()` 用 cn2an + 读音规则 + `pronounce.json` 转成读法再送 TTS；字幕显示的是书面原文
4. 按 `speed` 做 atempo 变速（**只重跑 ffmpeg，缓存复用，约 2 秒**）
5. **只合成一次音轨、一次视频**（不逐页切片再拼）→ 结构上无累积漂移
6. `-t <总时长>` 硬截断 → 视频与音轨严格等长
