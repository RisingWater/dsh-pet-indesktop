# -*- coding: utf-8 -*-
"""SwarmBubble 渲染回归（2026-10-02）：字面量 HTML 标签不得破坏 md 表格。

根因：agent 回答里常有字面量标签（如 ``<L>``）。Qt 的 markdown 解析器把未知
标签当内联 HTML 透传，污染后续块——实测普通文本里的 ``<L>`` 会让紧跟其后的
GFM 表格列宽塌缩到每列约 1 个字符（表格竖排、完全不可读）。修复：渲染前把
``&``/``<``/``>`` 转义为实体，标签按字面量显示，表格与其余 md 语法正常。
"""
from __future__ import annotations

from PySide6.QtCore import QRect
from PySide6.QtGui import QTextTable
from PySide6.QtWidgets import QApplication

from pet.swarm_bubble import SwarmBubble, _sanitize_markdown


def _qapp():
    return QApplication.instance() or QApplication([])


def _first_table_width(doc) -> float | None:
    """文档里第一个 QTextTable 的渲染宽度；无表格返回 None。"""
    found = []

    def walk(frame):
        for child in frame.childFrames():
            if isinstance(child, QTextTable):
                found.append(child)
            walk(child)

    walk(doc.rootFrame())
    if not found:
        return None
    layout = doc.documentLayout()
    _ = layout.documentSize()  # 强制完成布局
    return layout.frameBoundingRect(found[0]).width()


# 真实形状：指令标题下的字面量 <L> + 紧跟的 GFM 表格（摘自 CFrCp4KWKake 简报）
TABLE_MD = (
    "- 成功标准：全站双语 + 文案全走 t(zh,en)/<L> + 不翻代码 + build/lint 通过\n"
    "\n"
    "| 任务 | 状态 | 依赖 | 执行 | 验收 |\n"
    "|---|---|---|---|---|\n"
    "| 共享 chrome 与账号区文案外置 | done | 无 | agent_swarm | auto |\n"
    "| 中枢 Nexus 页与时间线渲染双语 | done | 共享 chrome | agent_swarm | auto |\n"
)


class TestSanitizeMarkdown:
    def test_escapes_raw_html(self):
        out = _sanitize_markdown("看 <L> 和 <http://x> 与 a & b")
        assert "&lt;L>" in out
        assert "&lt;http://x>" in out
        assert "&amp;" in out
        assert "<L>" not in out

    def test_keeps_markdown_syntax(self):
        # > 是块引用语法，不能被转义；&/< 之外的 md 语法原样保留
        src = "**bold** `code` | a | b |\n\n> quote\n\n- item"
        assert _sanitize_markdown(src) == src


class TestBubbleTableRendering:
    def test_table_survives_literal_html_in_body(self):
        """字面量 <L> 不再让后续表格塌缩，且按文本显示。"""
        _qapp()
        bubble = SwarmBubble()
        try:
            bubble.show_brief(
                title="任务完成", task_first="任务",
                answer=TABLE_MD, workspace_name="planner",
                anchor_rect=QRect(0, 0, 10, 10),
            )
            doc = bubble._body.document()
            width = _first_table_width(doc)
            assert width is not None, "GFM 表格未渲染成 QTextTable"
            assert width > 300, f"表格列宽塌缩（width={width}）"
            # 字面量标签按文本显示，不作为 HTML 元素
            assert "<L>" in doc.toPlainText().replace(" ", "")
        finally:
            bubble.dismiss()


class TestBubbleNoHorizontalOverflow:
    def test_code_block_wraps_instead_of_overflow(self):
        """长代码块解除 nonBreakableLines，横向不溢出（滚动条保持关闭）。"""
        _qapp()
        bubble = SwarmBubble()
        try:
            code = ('SumatraPDF.exe -print-to "HP LaserJet M405dn" '
                    '-print-settings "2x,duplex,pages=1-8" ' + "x" * 200)
            md = "说明\n\n```\n" + code + "\n```\n"
            bubble.show_brief(
                title="任务完成", task_first="任务", answer=md,
                workspace_name="ws", anchor_rect=QRect(0, 0, 10, 10),
            )
            body = bubble._body
            assert body.horizontalScrollBar().maximum() == 0, "代码块横向溢出"
            block = body.document().begin()
            while block.isValid():
                assert not block.blockFormat().nonBreakableLines()
                block = block.next()
        finally:
            bubble.dismiss()

    def test_wide_table_fits_narrow_bubble(self):
        """表头多、单元格长时，缩到最小宽度仍不横向溢出。"""
        _qapp()
        from pet.swarm_bubble import MIN_BUBBLE_H, MIN_BUBBLE_W

        bubble = SwarmBubble()
        try:
            row = "| 很长很长很长的中文任务名称 | done | 无 | agent_swarm | auto |\n"
            md = ("| 任务 | 状态 | 依赖 | 执行 | 验收 |\n|---|---|---|---|---|\n"
                  + row * 3)
            bubble.show_brief(
                title="任务完成", task_first="任务", answer=md,
                workspace_name="ws", anchor_rect=QRect(0, 0, 10, 10),
            )
            bubble._resize_to(MIN_BUBBLE_W, MIN_BUBBLE_H)
            body = bubble._body
            width = _first_table_width(body.document())
            assert width is not None
            assert width <= body.width() + 1, f"窄卡表格溢出（{width}>{body.width()}）"
            assert body.horizontalScrollBar().maximum() == 0
        finally:
            bubble.dismiss()
