# 路线 B：已有 PPTX / PDF → import

> 从 SKILL.md 拆出，按需读。命令入口：`ppt2video.py import <文件>`。

```bash
PY=~/.venv-mlx-audio/bin/python

# 1. 导入：渲染截图 + 生成 slides.json
$PY ~/ppt2video.py import old/融资路演.pptx

# 2. 检查 slides/ 下的截图；在 slides.json 里设置每页 sec / fixed / notes
#    （disc 默认关闭，owner 明确要求免责语时才设 true；没有备注的页补 notes）

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
