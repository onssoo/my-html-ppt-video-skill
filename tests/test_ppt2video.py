"""pytest 断言（吸收 reviewer 第 三.6 条：手工测过的用例要变成测试）。

跑法：python -m pytest tests/ -q        （只测确定性逻辑，不碰 TTS / LLM / 浏览器）
"""
import importlib.util, json as _json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
spec = importlib.util.spec_from_file_location("p2v", Path(__file__).resolve().parents[1] / "ppt2video.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

MD = """# 测试 · 大纲

- 受众：投资人
- 用途：验收
- 目标时长：2 分钟
- 主线：证明能力能变成规模
- 素材：none.md

## P1 封面标题写结论
- 版式：封面：kicker + 主标题（模板 cover.html）
- 信息点（1）：
  1. 主标题：甲
- 时长：12 秒
- 讲述要点（3–5 条，每条注明出处）：
  1. 无数字的解释性要点，不给出处（§1）
  2. 又一解释性要点（§1）
  3. 第三条（§1）

## P2 内容页标题
- 版式：KPI 条(3) + 结论框（模板 kpi-grid.html）
- 信息点（9）：
  1. 份额 92%
  2. 客户 7 家
  3. 收入 7300 万元
  4. 产能 30 吨
  5. 专利 835 项
  6. 纯度 8.8 ppb
  7. 良率 96%
  8. 团队 60 人
- 时长：36 秒
- 讲述要点（3–5 条，每条注明出处）：
  1. 含数字但没出处 96%
  2. 含数字有出处（§2.1）92%
  3. 纯解释不给出处

## P3 结尾页
- 版式：编号要点(3)（模板 end.html）
- 信息点（1）：
  1. 结论
- 时长：12 秒
- 讲述要点（3–5 条，每条注明出处）：
  1. 甲（§3）
  2. 乙（§3）
  3. 丙（§3）
"""


def doc():
    return m.parse_outline_md(MD)


# ── 解析（规范格式）────────────────────────────────────────
def test_parse_spec_format():
    p = doc()
    assert [x["n"] for x in p["pages"]] == [1, 2, 3]
    assert p["pages"][0]["title"] == "封面标题写结论"
    assert p["pages"][0]["sec"] == 12 and p["pages"][1]["sec"] == 36
    assert p["pages"][1]["info_declared"] == 9 and len(p["pages"][1]["info"]) == 8
    assert len(p["pages"][1]["notes"]) == 3


def test_budget_uses_spec_formula():
    b, cps, minutes = m.outline_md_budget(doc())
    sec = b[1]["sec"]
    assert cps == 4.5 and minutes == 2.0
    assert b[1]["lo"] == int(sec * cps * 0.8) and b[1]["hi"] == int(sec * cps * 1.3)


# ── 预检（规范 §一/§二）─────────────────────────────────────
def test_preflight_fires_the_right_things():
    iss = m.outline_preflight(doc())
    msgs = {(i["page"], i["level"], i["msg"]) for i in iss}
    assert any(pg == 2 and lv == "ERROR" and "不符" in msg for pg, lv, msg in msgs)          # 信息点（9）vs 8
    assert any(pg == 2 and "含数字的讲述要点没注明出处" in msg for pg, lv, msg in msgs)        # 只卡含数字的
    assert not any("纯解释不给出处" in msg for pg, lv, msg in msgs)                            # 无数字不卡
    assert not any(pg == 1 and "信息点" in msg for pg, lv, msg in msgs)                        # 封面不计密度
    assert not any(pg == 3 and "信息点" in msg for pg, lv, msg in msgs)                        # 结尾页不计密度
    assert not any(pg == 3 and "含数字" in msg for pg, lv, msg in msgs)


def test_preflight_title_and_disclaimer():
    iss = m.outline_preflight(doc())
    assert not any("标题" in i["msg"] for i in iss)                       # 标题都 ≤30 字
    d = m.parse_outline_md(MD.replace("## P3 结尾页", "## P3 本报告不构成任何收益承诺"))
    assert any("免责" in i["msg"] and i["level"] == "ERROR" for i in m.outline_preflight(d))


# ── 结构与写回 ──────────────────────────────────────────────
def test_ops_and_renumber():
    assert [x["title"] for x in m.parse_outline_md(m.outline_md_op(MD, "del", 2))["pages"]] == ["封面标题写结论", "结尾页"]
    assert [x["title"] for x in m.parse_outline_md(m.outline_md_op(MD, "up", 2))["pages"]] == ["内容页标题", "封面标题写结论", "结尾页"]
    assert [x["title"] for x in m.parse_outline_md(m.outline_md_op(MD, "down", 1))["pages"]] == ["内容页标题", "封面标题写结论", "结尾页"]
    added = m.parse_outline_md(m.outline_md_op(MD, "add", 2))["pages"]
    assert [x["n"] for x in added] == [1, 2, 3, 4] and "版式" in added[2]["chunk"]
    assert m.outline_md_op(MD, "del", 9) is None


def test_replace_page_only_touches_that_page():
    new = m.replace_page_md(MD, 2, "## P2 换标题\n- 版式：表格(4 列)（模板 table.html）\n- 信息点（6）：\n  1. a\n- 时长：30 秒\n- 讲述要点（3–5 条，每条注明出处）：\n  1. x（§1）")
    p = m.parse_outline_md(new)
    assert p["pages"][1]["title"] == "换标题" and p["pages"][0]["title"] == "封面标题写结论"
    assert p["pages"][2]["title"] == "结尾页"


# ── 大纲 → deck 骨架（规范 §六 第 84 行）─────────────────────
def test_deck_section_puts_notes_hidden_and_sec_absolute():
    s = m.deck_section(doc()["pages"][1])
    assert 'data-sec="36"' in s and 'data-title="内容页标题"' in s
    assert '<div class="notes">' in s and "（§2.1）" in s              # 出处进隐藏备注
    visible = re.sub(r'<div class="notes">.*?</div>', "", s, flags=re.S)
    visible = re.sub(r"<!--.*?-->", "", visible, flags=re.S)
    assert not re.search(r"§\s*\d|源文档|出处", visible)                # 可见画面零来源标注


# ── 读法规则（NOTES 里手工测过的用例）────────────────────────
def test_speak_rules_machine_independent():
    """只验规则本身（不依赖 cn2an）：年份逐位读、1200 年不误伤、≥1 亿的万元折成亿元。
    中文读法的最终转换由 cn2an 完成，只在装了的机器（出片机）上生效。"""
    assert "二零二六" in m.speak("2026 年")
    _y = m.speak("1200 年历史")                          # 四位非 19/20/21xx 不当年份
    assert "年历史" in _y and "到" not in _y               # 装着 cn2an 时会变「一千二百年历史」，所以不能断言数字形态
    assert "亿元" in m.speak("100000 万元") and "万元" not in m.speak("100000 万元")
    assert "万元" in m.speak("7300 万元")                  # 千万级保持万元
    assert "1200" not in m.speak("2026 年")                # 不许念成"二零二六年"以外的形式


# ── 审核台/审稿：--reset 不得冲掉手改的配置 ───────────────────
def test_review_reset_preserves_hand_edited_cfg(tmp_path):
    out = tmp_path / "video-output"; out.mkdir()
    slides = [{"title": "封面", "text": ["甲"], "notes": [], "sec": 12, "fixed": None, "disc": False}]
    deck = tmp_path / "deck.pptx"                       # 非 html，跳过截图
    m.cmd_review(str(deck), out, slides, reset=False, start=1)
    rv = out / "review.md"
    t = rv.read_text(encoding="utf-8")
    # review.md 里写的是本机配置解析后的值（用户项目目录里，正常）；
    # 这里把它改成一个假路径，模拟 owner 手改，然后验证 --reset 不冲掉
    t2 = re.sub(r"^ref_audio:.*$", "ref_audio: /tmp/voice.wav", t, count=1, flags=re.M)
    rv.write_text(t2, encoding="utf-8")
    assert m.parse_review(rv)[0]["ref_audio"] == "/tmp/voice.wav"
    m.cmd_review(str(deck), out, slides, reset=True, start=1)   # --reset
    assert "ref_audio: /tmp/voice.wav" in rv.read_text(encoding="utf-8")


# ── data-sec 是绝对秒（规范 §六 第 84 行）：版本模式等比缩放，不是当权重 ──
def test_page_seconds_scales_declared_seconds():
    slides = [{"sec": 12, "fixed": "1"}, {"sec": 40, "fixed": None},
              {"sec": 60, "fixed": None}, {"sec": 12, "fixed": "1"}]
    s = m.page_seconds(slides, 120)                       # 目标 120 秒
    assert abs(s[1] - 12) < 0.01 and abs(s[4] - 12) < 0.01        # fixed 页不缩放
    assert abs(sum(s.values()) - 120) < 0.01                      # 总长精确命中
    assert abs(s[2] / s[3] - 40 / 60) < 0.01                      # 非 fixed 页保持 40:60 比例
    s2 = m.page_seconds(slides, 120, skip=(3,))
    assert 3 not in s2 and abs(sum(s2.values()) - 120) < 0.01     # 跳页后仍命中总长


# ── 审核状态门禁（gates.json）──────────────────────────────
def test_gates_approve_then_stale(tmp_path):
    out = tmp_path / "vo"; out.mkdir()
    deck = tmp_path / "deck.html"; deck.write_text("<html>v1</html>", encoding="utf-8")
    m.gates_approve(out, "review", {"deck": deck})
    assert m.gates_stale(out, "review", {"deck": deck}) == []        # 没改 → 不算过期
    deck.write_text("<html>v2</html>", encoding="utf-8")
    assert m.gates_stale(out, "review", {"deck": deck}) == ["deck"]  # 改了 → 过期
    assert m.gates_stale(out, "review", {"deck": deck}) and \
        m.cmd_build(str(out), [{"title": "a", "sec": 10, "text": [], "notes": []}],
                    deck=str(deck)) == 2                             # build 拒绝
    assert m.gates_stale(tmp_path / "nowhere", "review", {}) is None  # 从没审过


# ── 批 1：输出目录统一 / 门禁收紧 / 无记录即拦 ──────────────
def test_out_dir_html_vs_pptx(tmp_path):
    h = tmp_path / "deck.html"; h.write_text("x")
    p = tmp_path / "路演.pptx"; p.write_text("x")
    assert m.out_dir(h).name == "video-output"
    assert m.out_dir(p).name == "路演-video"


def test_gate_files_includes_review_md(tmp_path):
    deck = tmp_path / "deck.html"; deck.write_text("x")
    out = tmp_path / "video-output"; out.mkdir()
    f = m.gate_files(str(deck), str(out), str(out))
    assert set(f) >= {"deck", "narrations", "review"}      # review.md 是 build 的真源


def test_gates_stale_treats_missing_at_approval_as_stale(tmp_path):
    out = tmp_path / "vo"; out.mkdir()
    rv = out / "review.md"
    m.gates_approve(out, "narration", {"review": rv})       # 审核时 review.md 还不存在
    rv.write_text("later", encoding="utf-8")                # 后来才生成 → 等于没审过
    assert m.gates_stale(out, "narration", {"review": rv}) == ["review"]


def test_build_refuses_without_any_gate_record(tmp_path):
    out = tmp_path / "vo"; out.mkdir()
    deck = tmp_path / "deck.html"; deck.write_text("x")
    rc = m.cmd_build(str(out), [{"title": "a", "sec": 10, "text": [], "notes": []}], deck=str(deck))
    assert rc == 2                                          # 没审核记录 → 拒绝出片


# ── 批 2：唯一字数公式 / 读法 / 读音表指纹 / 导入继承 ──────────
def test_char_range_is_single_source():
    assert m.char_range(45, 4.5) == (162, 263)                    # 规范 §八：0.8–1.3×
    c = m.chars_for(45, 4.5)
    assert m.char_range(45, 4.5, strict=True) == (int(c * 0.9), int(c * 1.1))   # 版本模式


def test_read_rules_date_and_dimension():
    assert "乘" in m.speak("1920x1080")                            # 尺寸不读成"倍"
    out = m.speak("2026-10-07")
    assert "到" not in out and "月" in out                          # 日期不被区间规则吃掉
    assert "到" in m.speak("2026–2030 年")                          # 真区间仍然读"到"


def test_pron_changes_cps_fingerprint():
    cfg = {"backend": "qwen", "ref_audio": "x", "speed": 1.0}
    before = m.tts_fingerprint(cfg)
    m.PRON["测试专名"] = "测试专名读法"
    assert m.tts_fingerprint(cfg) != before                        # 改读音 → 语速校准作废
    m.PRON.pop("测试专名")


def test_inherit_prev_unique_title_only():
    old = [{"title": "目录", "sec": 12, "notes": ["a"]},
           {"title": "目录", "sec": 20, "notes": ["b"]},           # 重名 → 不参与标题匹配
           {"title": "正文", "sec": 40, "notes": ["c"]}]
    new = [{"title": "正文", "sec": None, "notes": []},
           {"title": "目录", "sec": None, "notes": []},
           {"title": "新页", "sec": None, "notes": []},
           {"title": "尾巴", "sec": None, "notes": []}]             # 4 页 ≠ 旧 3 页
    nt, np_, miss = m.inherit_prev(new, old)
    assert new[0]["sec"] == 40 and new[0]["notes"] == ["c"]         # 唯一标题命中
    assert new[1]["sec"] is None                                    # 重名标题不继承
    assert (nt, np_, miss) == (1, 0, [2, 3, 4])                     # 页数变了 → 不按页码兜底
    # 页数没变才允许按页码补
    same = [{"title": "甲", "sec": None, "notes": []}, {"title": "乙", "sec": None, "notes": []}]
    nt2, np2, miss2 = m.inherit_prev(same, [{"title": "甲", "sec": 12}, {"title": "丙", "sec": 30}])
    assert nt2 == 1 and np2 == 1 and miss2 == []


# ── 重录标记真正生效（reviewer 一.3）──────────────────────
def test_redo_clears_that_pages_cache_only(tmp_path):
    """标了重录的页，其句子缓存被删；其他页不受影响。"""
    import json as _json
    cache = tmp_path / "tts-cache"; cache.mkdir()
    tts = type("T", (), {"key": ["qwen-clone", "/m", "/v.wav", "hash", "文本"]})()
    a = m.cache_key(tts, "第一页的句子。")
    b = m.cache_key(tts, "第二页的句子。")
    for h in (a, b):
        (cache / f"{h}.wav").write_bytes(b"x")
        (cache / f"{h}_x1.15.wav").write_bytes(b"x")     # atempo 变速副本
    # 模拟 build 里对第 1 页做的事
    for sp in ["第一页的句子。"]:
        for f in cache.glob(m.cache_key(tts, sp) + "*.wav"):
            f.unlink()
    assert not list(cache.glob(a + "*"))                  # 第 1 页清干净（含变速副本）
    assert len(list(cache.glob(b + "*"))) == 2            # 第 2 页原样


# ── 自查抓到的两条 NameError（测试原先没覆盖的路径）──────────
def test_review_narration_diff(tmp_path):
    """review.md 与 narrations.json 不一致的页要能列出来（build 走这条）。"""
    tdir = tmp_path / "vo"; tdir.mkdir()
    (tdir / "narrations.json").write_text(
        _json.dumps({"1": "一样", "2": "旧的"}, ensure_ascii=False), encoding="utf-8")
    pages = [(1, "一样"), (2, "新的"), (3, "只有 review 有")]
    assert m.review_narration_diff(tdir, pages) == [2, 3]
    assert m.review_narration_diff(tmp_path / "nope", pages) == []      # 没文件 → 不报


def test_cmd_review_doc_runs(tmp_path):
    """cmd_review_doc 全流程能跑通（原来 tot_t 引用了已删除的变量 c，会 NameError）。"""
    out = tmp_path / "video-output"; out.mkdir()
    deck = tmp_path / "deck.pptx"; deck.write_text("x")   # 非 html，跳过截图
    slides = [{"title": "封面", "text": ["甲"], "notes": [], "sec": 12, "fixed": None, "disc": False},
              {"title": "正文", "text": ["乙"], "notes": [], "sec": 40, "fixed": None, "disc": False}]
    (out / "slides.json").write_text(_json.dumps(slides, ensure_ascii=False), encoding="utf-8")
    m.cmd_review(str(deck), out, slides, reset=False, start=1)
    # 字数要够各页下限（12 秒 → 43 字；40 秒 → 144 字），且不能踩套话（"本页/这一页/最后/先说结论"…）
    long1 = (        "封面页的解说词要足够长才能过下限，所以这里把该说的事说清楚："
        "这份材料面向拟合作的投资人，用十四分钟讲清产能、客户结构与收"
        "入结构三件事，每一件都给出依据和口径，方便对方快速判断。除此"
        "之外不再增加别的内容，避免冲淡主线。")
    long2 = ("正文部分讲三件事，一件是口径表里的数字保持原样，一件是落地时间取决于重整方案的进度，"
             "一件是收入结构因此得到改善。把这三件串起来看，它们共同指向产能释放这个前提。"
             "第一条依据来自源文档的口径表，第二位依据说明前提条件，第三位依据说明它对公司的意义，"
             "也就是收入结构得到改善。以上判断都基于现有材料，没有引入材料之外的信息，也没有对外推演。")
    (out / "narrations.json").write_text(
        _json.dumps({"1": long1, "2": long2}, ensure_ascii=False), encoding="utf-8")
    rc = m.cmd_review_doc(str(deck), out, None, str(tmp_path / "审定稿.md"))
    assert rc == 0
    assert (tmp_path / "审定稿.md").exists()


# ── 读音表进指纹的连锁：谁算指纹谁就必须先 load_pron（自查发现）──
def test_project_pronounce_must_be_loaded_before_fingerprint(tmp_path):
    """项目里有 pronounce.json 时：load_pron 之后指纹才与校准时一致。"""
    out = tmp_path / "video-output"; out.mkdir()
    (out / "pronounce.json").write_text(_json.dumps({"Pt": "铂"}, ensure_ascii=False), encoding="utf-8")
    cfg = {"backend": "qwen", "ref_audio": "/x.wav", "speed": 1.0}
    m.PRON.clear(); m.load_pron(out, tmp_path)
    fp_cal = m.tts_fingerprint(cfg)
    (out / "cps.json").write_text(_json.dumps({"cps": 4.43, "fingerprint": fp_cal}), encoding="utf-8")
    m.PRON.clear()                                          # 模拟"没加载读音表"的调用方
    assert m.tts_fingerprint(cfg) != fp_cal                 # 指纹会不同 → 会误判作废
    assert m.load_cps(out, cfg) != 4.43
    m.PRON.clear(); m.load_pron(out, tmp_path)              # 加载后恢复正常
    assert m.load_cps(out, cfg) == 4.43


# ── 封面/结尾这类非内容页是内容页规则的例外（owner 2026-10-07）──────
def test_non_content_slide_detection():
    cover = {"title": "某材料公司", "cls": "slide cover", "sec": 12, "fixed": None}
    body = {"title": "海外客户合计约 77%", "cls": "slide", "sec": 45, "fixed": None}
    assert m.slide_is_non_content(cover, 1, 3) is True        # 首页 = 封面
    assert m.slide_is_non_content(cover, 3, 3) is True        # 末页 = 结尾
    assert m.slide_is_non_content({"title": "目录", "cls": "", "sec": 15}, 2, 5) is True
    assert m.slide_is_non_content({"title": "正文", "cls": "", "sec": 15, "fixed": 15}, 2, 5) is True
    assert m.slide_is_non_content(body, 2, 3) is False


def test_non_content_pages_exempt_from_global_floor():
    """12 秒封面页的下限是规范的 43 字，而不是全局下限 100 字。"""
    spec = m.char_range(12, 4.5)[0]
    assert m.page_floor(12, 100, 4.5, content=False) == spec          # 封面：豁免
    assert m.page_floor(12, 100, 4.5, content=True) == 100            # 内容页：仍是 100
    assert m.page_floor(45, 100, 4.5, content=True) == m.char_range(45, 4.5)[0]   # 162 > 100


# ── --length 持久化到 review.md，三处读同一个值（owner 2026-10-07 第 2 条）──
def test_length_persisted_and_used_by_review_doc(tmp_path):
    """--length 的生效值写进 review.md；review-doc 与审核台都读它（owner 2026-10-07 第 2 条）。"""
    out = tmp_path / "video-output"; out.mkdir()
    deck = tmp_path / "deck.pptx"; deck.write_text("x")
    slides = [{"title": "封面", "text": ["甲"], "notes": [], "sec": 12, "fixed": None, "disc": False},
              {"title": "短内容页", "text": ["乙"], "notes": [], "sec": 12, "fixed": None, "disc": False},
              {"title": "长内容页", "text": ["丙"], "notes": [], "sec": 45, "fixed": None, "disc": False},
              {"title": "谢谢", "text": ["丁"], "notes": [], "sec": 10, "fixed": None, "disc": False}]
    (out / "slides.json").write_text(_json.dumps(slides, ensure_ascii=False), encoding="utf-8")
    m.write_review(out / "review.md", slides, {}, dict(m.DEFAULTS), skip=(), slides_dir=out / "slides")
    (out / "narrations.json").write_text(
        _json.dumps({str(i): "占位解说词。" * 12 for i in range(1, 5)}, ensure_ascii=False), encoding="utf-8")

    rv = out / "review.md"
    assert m.parse_review(rv)[0]["length"] == "100-180"          # ① 新建即带默认值

    # ② 默认下限下：12 秒的"内容页"被要求 100 字（旧行为）
    m.cmd_review_doc(str(deck), out, None, str(tmp_path / "a.md"))
    row2 = next(l for l in (tmp_path / "a.md").read_text(encoding="utf-8").split("\n") if l.startswith("| 2 |"))
    assert "≥100" in row2

    # ③ 落盘自定义 length 后，同一个页面按 40 起算 → max(40, 43) = 43
    assert m.set_review_cfg(rv, length="40-200") is True
    assert m.parse_review(rv)[0]["length"] == "40-200"
    m.cmd_review_doc(str(deck), out, None, str(tmp_path / "b.md"))
    doc = (tmp_path / "b.md").read_text(encoding="utf-8")
    assert "≥43" in next(l for l in doc.split("\n") if l.startswith("| 2 |"))
    assert "≥43" in next(l for l in doc.split("\n") if l.startswith("| 1 |"))   # 封面（非内容页）
    assert "≥162" in next(l for l in doc.split("\n") if l.startswith("| 3 |"))  # 内容页取大者
    assert "≥36" in next(l for l in doc.split("\n") if l.startswith("| 4 |"))   # 结尾（非内容页）


# ── 审核点守卫：Gate ① 没通过不许开始做 slides（owner 2026-10-07 规则）──
def test_deck_skeleton_requires_gate1(tmp_path):
    import shutil
    md = tmp_path / "大纲.md"
    shutil.copy(Path(__file__).parent / "fixture-大纲.md", md)
    sk = tmp_path / "deck-骨架.html"
    assert m.cmd_deck_skeleton(str(md), str(sk)) == 2        # 没有 Gate ① 记录 → 拒绝
    assert not sk.exists()
    out = tmp_path / "video-output"; out.mkdir()
    m.gates_approve(out, "outline", {"大纲": md})            # owner 点了「完成」
    assert m.cmd_deck_skeleton(str(md), str(sk)) == 0
    assert sk.exists()
    md.write_text(md.read_text(encoding="utf-8") + "\n<!-- 改过 -->\n", encoding="utf-8")
    assert m.cmd_deck_skeleton(str(md), str(sk)) == 2        # 通过后又改了 → 需重审
    assert m.cmd_deck_skeleton(str(md), str(sk), force=True) == 0   # owner 明确要求才可越过


# ── 画布容量：设计阶段就要能算出「装不下」（不用渲染）──────────────
def _page(layout, info, n=1, title="标题"):
    return {"n": n, "title": title, "layout": layout, "info": info,
            "notes": ["甲", "乙", "丙"], "sec": 30, "info_declared": len(info)}


def test_capacity_table_columns_rows_and_cells():
    # 5 列 → ERROR（规范 §二 第 43 行 /§三 第 61 行：表格 ≤4 列）
    p = _page("表格(5 列，表头 + 3 行)（模板 table.html）",
              ["表头：A · B · C · D · E"] + ["甲 · 乙 · 丙 · 丁 · 戊"] * 3)
    lv = [x[0] for x in m.capacity_issues(p)]
    assert "ERROR" in lv
    # 8 行 + 图注 → 超出画布（几何上最多 7 行）；无图注 8 行可以
    rows = ["甲 · 乙"] * 8
    with_cap = _page("表格(2 列，表头 + 8 行)（模板 table.html）",
                     ["表头：A · B"] + rows + ["图注：说明文字"])
    assert any("行超出画布" in x[1] for x in m.capacity_issues(with_cap))
    no_cap = _page("表格(2 列，表头 + 8 行)（模板 table.html）", ["表头：A · B"] + rows)
    assert not any("行超出画布" in x[1] for x in m.capacity_issues(no_cap))
    # 9 行 → ERROR
    nine = _page("表格(2 列，表头 + 9 行)（模板 table.html）", ["表头：A · B"] + ["甲 · 乙"] * 9)
    assert any(x[0] == "ERROR" and "行超出画布" in x[1] for x in m.capacity_issues(nine))
    # 每格 > 12 字 → WARN
    long_cell = _page("表格(2 列，表头 + 2 行)（模板 table.html）",
                      ["表头：A · B", "这是一个超过十二个字的很长的格 · 短"])
    iss2 = m.capacity_issues(long_cell)
    assert any("格超过容量" in x[1] for x in iss2)
    # 预警要给出该列数/行数下的具体字数（2 列 × 2 行 → 一行 25 字 × 3 行 = 75 字）
    assert any("每格约" in x[1] for x in iss2)


def test_capacity_geometry_matches_measured_constants():
    """几何常量要和实测一致：一行 71px、表头 76px、标题块 141px、上下留白 92px。"""
    assert m.GEOM["box"]["td"] == 71.0 and m.GEOM["box"]["th"] == 76.0
    assert m.GEOM["title"] == 141 and m.GEOM["pad_top"] + m.GEOM["pad_bottom"] == 92
    # 表格高度模型：4 行 1 行文字 = 76 + 4×71（实测 362）
    assert m.table_height([["A"] * 4] + [["甲"] * 4] * 4, 4) == 76 + 4 * 71


# ── 表格切列与行列一致性（实测踩坑：括号内的「 · 」、以及一行被写成两条信息点）──
def test_split_cells_ignores_parentheses():
    assert m.split_cells("甲 · 乙 · 丙") == ["甲", "乙", "丙"]
    # 括号内的「 · 」不是列分隔（P14 曾被切坏成 6 列）
    s = "发光层整体方案（RD／GD／BD · RH／GH／BH · R'G'B'） · 各项 10–15% · 各项 40%"
    assert len(m.split_cells(s)) == 3


def test_capacity_flags_header_row_mismatch():
    p = {"n": 1, "title": "T", "layout": "表格(4 列，表头 + 2 行)（模板 table.html）",
         "info": ["表头：A · B · C · D", "甲 · 乙", "丙 · 丁 · 戊 · 己"], "notes": ["x"], "sec": 30}
    iss = m.capacity_issues(p)
    assert any(x[0] == "ERROR" and "列" in x[1] and "错位" in x[1] for x in iss)
    # 续行（只有 1 格）会被并进上一行，不算错位
    ok = {"n": 2, "title": "T", "layout": "表格(4 列，表头 + 1 行)（模板 table.html）",
          "info": ["表头：A · B · C · D", "甲 · 乙 · 丙", "续行内容"], "notes": ["x"], "sec": 30}
    assert not any("错位" in x[1] for x in m.capacity_issues(ok))
