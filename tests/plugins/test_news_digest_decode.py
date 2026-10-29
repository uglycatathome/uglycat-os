"""Tests for the news_digest decode knowledge base (handbook integration)."""

from __future__ import annotations

from family_os.plugins.news_digest import decode


class TestConstants:
    def test_decode_formula_has_two_laws(self):
        assert "对冲规律" in decode.DECODE_FORMULA
        assert "程度阶梯" in decode.DECODE_FORMULA

    def test_verb_ladder_both_directions(self):
        assert "严禁" in decode.VERB_LADDER
        assert "常态化监管" in decode.VERB_LADDER

    def test_intensity_table_has_five_levels(self):
        levels = [row[0] for row in decode.INTENSITY_TABLE]
        assert levels == ["最高级", "高级", "中级", "观察级", "反向"]
        assert all(len(row) == 3 for row in decode.INTENSITY_TABLE)

    def test_topic_tables_cover_seven_chapters(self):
        assert set(decode.TOPIC_TABLES) == {
            "宏观经济形势", "货币政策", "财政政策", "资本市场",
            "房地产", "监管与平台经济", "产业与新动能"}
        for name, rows in decode.TOPIC_TABLES.items():
            assert len(rows) >= 5, f"{name} 表行不足 5 条"
            assert all(isinstance(r, tuple) and len(r) == 2 for r in rows), name

    def test_monetary_ladder_string_present(self):
        """货币政策表必须含程度阶梯串：稳健 → … → 适度宽松。"""
        flat = "\n".join(f"{a} {b}" for a, b in decode.TOPIC_TABLES["货币政策"])
        assert "稳健" in flat and "适度宽松" in flat and "→" in flat

    def test_reverse_reading_and_period_compare(self):
        assert "告知型" in decode.REVERSE_READING and "反着读" in decode.REVERSE_READING
        assert "新增词" in decode.PERIOD_COMPARE and "消失词" in decode.PERIOD_COMPARE

    def test_misread_warnings_three(self):
        assert len(decode.MISREAD_WARNINGS) == 3
        assert any("套话" in w for w in decode.MISREAD_WARNINGS)
        assert any("概率" in w for w in decode.MISREAD_WARNINGS)

    def test_legacy_constants_kept(self):
        """现有数据不删：政策术语 / 力度分级 / 术语演变 / 外交辞令（后者不注入）。"""
        assert decode.POLICY_TERMS and decode.INTENSITY_GRADING and decode.TERM_EVOLUTION
        assert decode.DIPLOMATIC_SUBTEXT


class TestBlockComposition:
    def test_stage0_has_grading_not_topic_tables(self):
        block = decode.stage0_block()
        assert "高频词分级" in block
        assert "严禁" in block
        assert "经济类专题表" not in block
        assert "货币政策" not in block

    def test_stage1_has_everything(self):
        block = decode.stage1_block()
        assert "对冲规律" in block
        assert "经济类专题表" in block
        assert "政策术语对照" in block
        assert "告知型" in block
        assert "套话不是信号" in block
        for name in decode.TOPIC_TABLES:
            assert name in block, name

    def test_stage1_omits_extract_hint(self):
        """stage1 是解码阶段，不应带 stage0 的「提取」指令语气。"""
        assert "任务提示：提取时" not in decode.stage1_block()
        assert "任务提示：提取时" in decode.stage0_block()

    def test_stage2_only_formula_and_period(self):
        block = decode.stage2_block()
        assert "对冲规律" in block
        assert "新增词" in block
        assert "术语演变参照" in block
        assert "经济类专题表" not in block
        assert "高频词分级" not in block

    def test_stage3_is_empty(self):
        assert decode.stage3_block() == ""


class TestExclusions:
    def test_h_chapter_not_injected(self):
        for block in (decode.stage0_block(), decode.stage1_block(), decode.stage2_block()):
            assert "坦率交谈" not in block
            assert "反腐" not in block
            assert "座次" not in block

    def test_f_chapter_not_injected(self):
        for block in (decode.stage0_block(), decode.stage1_block(), decode.stage2_block()):
            assert "新闻联播" not in block


class TestSizeLimits:
    def test_block_sizes_within_budget(self):
        limits = {"stage0": 4096, "stage1": 14_336, "stage2": 3072}
        actual = {
            "stage0": len(decode.stage0_block().encode("utf-8")),
            "stage1": len(decode.stage1_block().encode("utf-8")),
            "stage2": len(decode.stage2_block().encode("utf-8")),
        }
        for name, size in actual.items():
            assert size <= limits[name], f"{name}_block 超限：{size} > {limits[name]}"

    def test_legacy_builder_removed(self):
        assert not hasattr(decode, "build_methodology_block")
        assert not hasattr(decode, "METHOD_GUIDE")
