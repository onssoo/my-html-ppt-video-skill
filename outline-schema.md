# `大纲.md` —— 制作 PPT 的剧本（Gate 1 的真源）

> **流程（依据 `my-html-ppt-skill` 的规范，不是我发明的）**
> 拿到原文件 → 理解 → 构思怎么做 PPT → **把构思写成 `大纲.md`** → 依据 `大纲.md` 做 slides → 后续（解说词 → 出片）
>
> 依据的规范文件：
> - `my-html-ppt-skill/references/authoring-guide.md` **§3 Outline the deck**（骨架）· **§5 Author each slide**（"for each outline item → 打开对应 layout → 拷 `<section>` → 换真实数据"）
> - `my-html-ppt-skill/references/layouts.md` —— 36 个**真实骨架模板名**
> - `my-html-ppt-skill/references/dailei-typography-and-density.md` **§3 每页预算** · **§4 拆页规则** · **§5 内容→版式对照表** · **§6 给 agent 的硬规则**

## 整体骨架（authoring-guide §3）

```
cover → toc → section-divider → [2–4 正文页] → section-divider → [2–4 正文页] → … → 结语
```

`dailei` §5 给的**商务稳重首选**（owner 的主场景）：
`cover → toc → section-divider → bullets → two-column → kpi-grid → table → 内联 SVG 图 → comparison → roadmap → 结语页`

## 硬规则（写大纲时就要满足，不是写 slides 时才管）

1. **每页点名一个真实骨架**（`templates/single-page/<name>.html`），**不许连续两页用同一个**
2. **页标题写结论，不写话题**：`海外巨头合计垄断约 77%` ✔ ／ `竞争格局` ✘
3. **每页预算**：正文 ≤350 字 · 信息点 ≤该骨架上限（见 §5 表，多为 6）· 一页一表（≤10 行 × 6 列）或一页一图
4. **超预算就拆页，绝不缩字号**；拆页时第二页标题 = 原标题 + `（续）`
5. **三层结构**：L1 主张（`stat-highlight`/`big-quote`/`kpi-grid`/`bullets`）→ L2 证据（一页一图或一页一表）→ L3 附录（`table` + `（续）`）
6. **图表用内联 SVG**（份额/排名/对比条形图）；`chart-*.html` 依赖 Chart.js CDN，**离线不可用**
7. 对比用 `comparison` / `pros-cons` 卡片；**只有必须精确查值时才用 `table`**

## 文件格式

```markdown
# <项目名> · 大纲

- 受众：…
- 目标时长：14 分钟
- 主题：dailei-business-navy
- 素材：<源文件名>
- 一句话故事：…

## 整体组织

| 部分 | 在论证里干什么 | 页 | 骨架序列 | 时长 |
|---|---|---|---|---|
| 开场 | 给结论 + 声明材料性质 | 1–2 | cover → toc | 0.4 分钟 |

---

## 第 1 部分 · 开场

> 这部分干什么：用一句话给结论，并声明材料性质

### 第 01 页 · <标题写结论>
- **骨架**：`cover.html`
- **页长**：固定 12 秒                      ← 工具解析：`固定 N 秒` 或 `权重 N`
- **这页干什么**：…
- **页面文字**：kicker `…` ｜ 标题 `…` ｜ 副标题 `…` ｜ 页脚 `…`
- **信息点**（≤该骨架上限，一条一行）：
  1. …
- **数据/数字**：…（逐字来自源文档；没有写「无」）
- **讲述要点**：
  1. …（降序，第 1 条最重要）
- **舍弃**：…

### 第 02 页 · …
```

### 工具只解析这四样（其余是给人/模型读的，随便写）

| 标记 | 用途 |
|---|---|
| `## 第 N 部分 · <名>` + 紧随的 `> 这部分干什么：…` | 分部分 + 该部分作用 |
| `### 第 NN 页 · <标题>` | 页边界与页标题 |
| `- **页长**：权重 60` / `- **页长**：固定 12 秒` | 时长分配（权重是相对值）——只在做视频时才需要 |

所以**改大纲不需要改代码**；审核台按页显示可编辑的原始 md 片段，保存即写回。
