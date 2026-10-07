"""pytest 断言（吸收 reviewer 第 三.6 条：手工测过的用例要变成测试）。

跑法：python -m pytest tests/ -q        （只测确定性逻辑，不碰 TTS / LLM / 浏览器）
"""
import importlib.util, re, sys
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
    assert "1200 年" in m.speak("1200 年历史")            # 四位非 19/20/21xx 不当年份
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
