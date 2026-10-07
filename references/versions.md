# 多版本：同一份幻灯片，不同时长

> 从 SKILL.md 拆出，按需读。核心：**改的是解说词详略，不动 deck 页数**。

同一份 PPT 对不同对象要讲不同时长（10 分钟给投资人、15 分钟给深度沟通、8 分钟给合作方）。
时长做成版本，**画面只截一次，解说词/审稿/成片各版本独立**。

### 四个控制参数

| 参数 | 写在哪 | 作用 |
|---|---|---|
| **总时长** | `versions.json` 的 `minutes` | 该版本的目标分钟数 |
| **每页时长** | `data-sec`（HTML）/ `slides.json` 的 `sec`（导入）| **绝对秒**（规范 §六 第 84 行）：这是这一页的目标秒数，不是权重。版本模式按这些秒数**等比缩放**到该版本的总时长，fixed 页（封面/目录）保持原值 |
| **固定页** | `data-fixed="12"` / `slides.json` 的 `fixed` | 封面、目录等固定秒数，不参与缩放 |
| **跳页** | `versions.json` 的 `skip: [11, 12]` | 短版整页跳过附录/明细，比每页压到 15 秒好 |

**关键：不要把"讲 10 分钟"直接告诉模型。** 模型对时长没有概念，字数会偏得很远。脚本按下面的公式把
总时长换算成**每页字数目标**，模型只面对一个明确的字数。

```
每页秒数 → 字数：  speech = max(sec − LEAD − TAIL, 3)
                  chars  = speech × cps / (1 + GAP × cps / 25)
```
（`LEAD/TAIL/GAP` 是页首留白/页尾留白/句间停顿；`cps` 是实测语速，见下）
按 cps=4.5 估算：**30 秒 ≈ 125 字，45 秒 ≈ 190 字**。

- 每页有下限 `MIN_SEC=12` 秒。**钳制后总长会超标，脚本会报出来**：
  "因每页下限 12 秒，实际总长 X 分钟 > 目标 Y 分钟" → 这时该用 `skip` 砍页，而不是硬压每页。
- 短版的**上限是软的**：超过目标上限 20%（`OVER_TOL`）才退回重写。内容优先，不硬卡上限。

### versions.json（放在幻灯片旁边）

```json
{
  "investor-15": {"minutes": 15, "brief": "brief-investor.md"},
  "investor-10": {"minutes": 10, "brief": "brief-investor.md", "from": "investor-15"},
  "partner-8":   {"minutes": 8,  "brief": "brief-partner.md", "skip": [11, 12], "from": "investor-15"}
}
```

**先做最长的母版，短版从母版压缩**（`from`）。压缩不会冒出新的编造内容——母版文本也会进数字溯源。
反过来把短版扩写成长版容易编造，所以**长版必须单独生成**，不要从短版扩写。
受众差异很大时（投资人 vs 技术合作方）用不同 `brief` 重新生成，不走压缩。

母版更新后，脚本会按文件时间提醒"本版可能已过期"。

### 配置层级（四个文件，各管一件事）

| 文件 | 位置 | 管什么 |
|---|---|---|
| `versions.json` | 幻灯片旁边 | **版本级**：总时长、跳页、场景卡、母版来源 |
| `review.md` | `versions/<版本>/` | **该版本的真源**：音色、语速倍数、字幕、构图 + 逐页解说词 |
| `cps.json` | 输出根目录 | 实测语速（**与音色/后端/语速绑定**，各版本共用） |
| `slides.json` | 输出根目录 | 导入的画面文字 + 每页 `sec`/`fixed`/`disc`/`notes`（各版本共用） |

`brief` 优先级：命令行 `--brief` > `versions.json` 的 `brief`。

### 输出目录

```
<deck>-video/
├── slides/001.png …            # 画面只截一次，各版本共用
├── slides.json                 # 导入信息（各版本共用）
├── cps.json                    # 实测语速（各版本共用）
├── tts-cache/                  # 句子级缓存（各版本共用：相同句子只合成一次）
├── versions/<版本名>/           # 该版本的一切
│   ├── narrations.json  review.md  brief.md  outline.md
│   ├── narration.wav  list.ffconcat  subs.srt
│   └── final-<版本名>.mp4
└── final-video.mp4             # 只有不带 --version 的单版本模式才有
```

### 使用流程

```bash
# 1. 母版（最长版本）＋校准语速
python ppt2video.py narrate 路演.pptx --version investor-15 --source report.docx
python ppt2video.py review  路演.pptx --version investor-15 --reset
python ppt2video.py build   路演.pptx --version investor-15    # 写入 cps.json

# 2. 从母版压缩出短版（只需在 versions.json 里加一行）
python ppt2video.py narrate 路演.pptx --version investor-10
python ppt2video.py review  路演.pptx --version investor-10 --reset
python ppt2video.py build   路演.pptx --version investor-10
```

`--version` **不传就是老的单版本流程**（产物落在输出根目录，字数下限用 `--length`），
现有 deck 不受影响。

### 校准与容差

- `build` 结束会打印 **实测字/秒**，并写入 `cps.json`。之后的版本自动用它，时长会越来越准。
- `cps.json` 记录后端/音色/语速**指纹**：换了参考录音、TTS 后端或改了 `speed`，指纹不匹配 →
  自动作废并按默认 4.5 估算，同时提示重做母版校准。（`speed` 走 ffmpeg atempo，直接改字/秒。）
- 每页低于 20 秒基本只能念结论；20 页的 PPT 比较舒服的范围是 **8–20 分钟**。
- 还差几个百分点时不必重写：微调 `GAP`/`TAIL`（各 0.1 秒 × 20 页 ≈ 十几秒），
  或对个别页用 `--pages` 只重写那几页。
- **短版审稿重点看取舍**：压缩可能删掉你认为重要的内容。某页总被删错时，
  最根本的办法是把那条要点**移到讲述要点的第一条**，而不是反复重新生成。
