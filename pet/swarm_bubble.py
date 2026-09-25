# -*- coding: utf-8 -*-
"""Swarm 专用气泡：简报大卡 + 权限/提问小卡（白底、md 渲染、滚动）。

与 SpeechBubble 的分工：SpeechBubble 是通用提醒队列的呈现层（纯文本、
分页、跟随 alert 队列），swarm 简报的信息密度（md 全文 + 滚动 + 定制
布局）超出它的表达范围——2026-09-25 用户决策为 swarm 建独立气泡，
不走 show_alert 队列，由 AgentLinkManager 直接管理生命周期。

尺寸契约（用户要求）：
- 最大宽/高 = PetSpeechBubble 当前实际尺寸的 2 倍；最小宽 = 当前宽。
- 宽度优先把指令装进一行（QFontMetrics 实测），装不下再换行。
- 高度随回答渲染高度自适应（QTextBrowser document().size()），超出转滚动。
- 权限/提问卡：小尺寸，只提示去决策，不渲染选项。

视觉契约（visual-system.md）：白卡片 #ffffff + 1px #e2e4e8 边 + 12px 圆角，
文字色跟随菜单语言（modern 浅 #595959 / 暗 #d6d6d6），按钮 7px 圆角。
"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

log = logging.getLogger("dsh-pet-standalone")

# 屏幕内边距：气泡任何边到屏幕可用区至少这个距离（用户要求：不显示在屏幕外）
SCREEN_PADDING = 16

# 卡片内边距 / 元素间距：四周留足呼吸感（用户要求 padding 充足）
CARD_MARGIN = 20
CARD_SPACING = 10

# 文本截断上限（与 agent_link 的简报语义一致）
TASK_FIRST_MAX = 200


def _menu_text_color(widget: QWidget) -> str:
    """菜单文字色：跟随 modern 主题（icons.py::_icon_theme 同语义）。

    modern 浅色 #595959 / modern 暗色 #d6d6d6；非 modern（native）回
    调色板前景色。逐父链探测 modernDark 属性，与菜单图标同源。
    """
    dark = False
    modern = False
    current = widget
    while current is not None:
        if current.property("modernDark"):
            dark = True
            modern = True
            break
        if str(current.property("menuStyle") or "") == "modern":
            modern = True
            break
        current = current.parentWidget()
    if modern:
        return "#d6d6d6" if dark else "#595959"
    fg = widget.palette().color(widget.foregroundRole())
    return fg.name() if fg.isValid() else "#595959"


class SwarmBubble(QWidget):
    """swarm 专用气泡：白底卡片，md 渲染 + 滚动，sticky 常驻。

    单实例复用（Manager 持有）：新卡顶替旧卡（同类覆盖），「知道了」关闭。
    """

    dismissed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("swarm-bubble")
        flags = (
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # 半透明背景：QSS 12px 圆角外露出透明而不是黑底（SpeechBubble 同款）
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        # —— 尺寸基准：SpeechBubble 的实际宽高（基类卡；权限小卡也按它缩）——
        # SpeechBubble 常规宽约 274px（13px margin × 2 + 248px 列宽）、高随内容。
        # 取固定基准而非运行时测量（气泡可能尚未 show 过）：与 248 列宽契约一致。
        self._base_w = 274
        self._base_h = 180
        self._max_w = self._base_w * 2
        self._max_h = self._base_h * 2

        # —— 白底卡片视觉（visual-system 卡片契约）——
        self._card = QFrame(self)
        self._card.setObjectName("swarm-bubble-card")

        layout = QVBoxLayout(self._card)
        layout.setContentsMargins(CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN)
        layout.setSpacing(CARD_SPACING)

        # ① 完成时间 + 工作区名（一行小字）
        self._meta_label = QLabel(self._card)
        self._meta_label.setObjectName("swarm-bubble-meta")
        self._meta_label.setWordWrap(False)
        layout.addWidget(self._meta_label)

        # ② 指令（QLabel，优先单行，装不下 wordwrap）
        self._task_label = QLabel(self._card)
        self._task_label.setObjectName("swarm-bubble-task")
        self._task_label.setWordWrap(True)
        self._task_label.hide()
        layout.addWidget(self._task_label)

        # ③ 回答（md 渲染 + 滚动）
        self._body = QTextBrowser(self._card)
        self._body.setObjectName("swarm-bubble-body")
        self._body.setOpenExternalLinks(False)
        self._body.setFrameShape(QFrame.Shape.NoFrame)
        self._body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self._body, 1)

        # ④ 按钮行：「知道了」
        btn_row = QHBoxLayout(self._card)
        btn_row.addStretch(1)
        self._ok_btn = QPushButton("知道了", self._card)
        self._ok_btn.setObjectName("swarm-bubble-ok")
        self._ok_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ok_btn.clicked.connect(self._on_ok)
        btn_row.addWidget(self._ok_btn)
        layout.addLayout(btn_row)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._card)
        self._apply_theme()

    # ------------------------------------------------------------ 主题

    def _apply_theme(self) -> None:
        color = _menu_text_color(self)
        muted = "#9a9aa3"
        border = "#e2e4e8"
        self._card.setStyleSheet(f"""
            QFrame#swarm-bubble-card {{
                background: #ffffff;
                border: 1px solid {border};
                border-radius: 12px;
            }}
            QLabel#swarm-bubble-meta {{
                color: {muted}; font-size: 11px; background: transparent; border: none;
            }}
            QLabel#swarm-bubble-task {{
                color: {color}; font-size: 13px; font-weight: 600;
                background: transparent; border: none;
            }}
            QTextBrowser#swarm-bubble-body {{
                color: {color}; font-size: 13px; background: #ffffff;
                border: none; padding: 4px 6px;
                selection-background-color: #0a84ff;
            }}
            QPushButton#swarm-bubble-ok {{
                color: {color}; background: #f4f5f6; border: 1px solid {border};
                border-radius: 7px; padding: 4px 16px; min-height: 22px;
            }}
            QPushButton#swarm-bubble-ok:hover {{ background: #e8eaee; }}
        """)
        # QTextBrowser 内滚动条细样式
        self._body.setStyleSheet(self._body.styleSheet() + """
            QScrollBar:vertical { width: 7px; background: transparent; }
            QScrollBar::handle:vertical { background: #d0d3d8; border-radius: 3px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
        """)

    # ------------------------------------------------------------ 展示

    def show_brief(self, *, title: str, task_first: str, answer: str,
                   workspace_name: str, anchor_rect) -> None:
        """简报卡（大）：时间+工作区 → 指令 → 回答(md)。"""
        now = datetime.now().strftime("%H:%M")
        self._meta_label.setText(f"{now} 完成 · {workspace_name}")
        task_first = " ".join(task_first.split())
        if len(task_first) > TASK_FIRST_MAX:
            task_first = task_first[:TASK_FIRST_MAX] + "…"
        self._task_label.setText(f"任务：{task_first}" if task_first else "")
        self._task_label.setVisible(bool(task_first))
        body = answer.strip() or "（无最终回答文本）"
        self._body.setMarkdown(body[:20000])
        self._show_sized(anchor_rect, wide=True)

    def show_notice(self, *, title: str, message: str, workspace_name: str,
                    anchor_rect) -> None:
        """权限/提问卡（小）：只提示哪个工作区有事要决定，不放选项。"""
        now = datetime.now().strftime("%H:%M")
        self._meta_label.setText(f"{now} {title} · {workspace_name}")
        self._task_label.hide()
        self._body.setMarkdown(message)
        self._show_sized(anchor_rect, wide=False)

    # ------------------------------------------------------------ 尺寸与定位

    def _show_sized(self, anchor_rect, *, wide: bool) -> None:
        """按内容计算宽高 → 夹紧屏幕边界 → 显示。

        wide=True 简报大卡：宽度优先装下指令一行；wide=False 权限小卡。
        """
        if not wide:
            self._resize_to(self._base_w, self._base_h)
            self._popup(anchor_rect)
            return
        # 宽：内容多时直接给满 max（用户实测：内容多时小窗滚不动也没必要——
        # max 就是按 2 倍基准设计的）；指令一行确实很短时才收窄。
        fm = QFontMetrics(self._task_label.font())
        task_w = fm.horizontalAdvance(self._task_label.text()) if self._task_label.isVisible() else 0
        want_w = task_w + CARD_MARGIN * 2 + 24  # + 余量，避免临界换行
        # 高：回答渲染高度 + 头部区高度。内容超过基准高（必然，简报几乎总超）
        # 就直接给满 max 高——用户实测反馈：内容很多仍 274x180，要求直接 2 倍。
        self._body.setFixedWidth(self._max_w - CARD_MARGIN * 2)
        doc_h = self._body.document().size().toSize().height() + 6
        head_h = self._meta_label.sizeHint().height() + CARD_SPACING
        if self._task_label.isVisible():
            head_h += self._task_label.sizeHint().height() + CARD_SPACING
        need_h = doc_h + head_h + CARD_MARGIN * 2 + 44
        width = self._max_w if want_w > self._base_w else self._base_w
        height = self._max_h if need_h > self._base_h else self._base_h
        self._resize_to(width, height)
        self._popup(anchor_rect)

    def _resize_to(self, width: int, height: int) -> None:
        self._body.setFixedWidth(width - CARD_MARGIN * 2)
        self.setFixedSize(width, height)

    def _popup(self, anchor_rect) -> None:
        """锚点上方居中弹出 + 屏幕边界收口（四周 SCREEN_PADDING）。"""
        x = anchor_rect.center().x() - self.width() // 2
        y = anchor_rect.top() - self.height() - 8  # 上方留 8px 呼吸
        screen = self.screen()
        if screen is None or screen.geometry().isEmpty():
            screen = self._fallback_screen(anchor_rect)
        avail = screen.availableGeometry()
        x = max(avail.left() + SCREEN_PADDING,
                min(x, avail.right() - self.width() - SCREEN_PADDING))
        y = max(avail.top() + SCREEN_PADDING,
                min(y, avail.bottom() - self.height() - SCREEN_PADDING))
        self.move(x, y)
        self.show()
        self.raise_()

    def _fallback_screen(self, anchor_rect):
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication.screenAt(anchor_rect.center())

    # ------------------------------------------------------------ 关闭

    def _on_ok(self) -> None:
        self.hide()
        self.dismissed.emit()

    def dismiss(self) -> None:
        self.hide()
