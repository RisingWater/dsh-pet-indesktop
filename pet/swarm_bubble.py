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

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QTextCursor
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

# 竖滚动条预留宽度：_apply_theme 里 QScrollBar:vertical 定宽 7px，与其保持一致
_VSCROLL_RESERVE = 7

# 边缘拖拽热区厚度（px）：四边与四角在此范围内触发改变大小光标/拖拽
RESIZE_GRIP = 8

# 用户手拉大小的界限：下限取权限小卡基准（再小内容就不可读了），
# 上限放宽到基准 3 倍宽 / 4 倍高——自由调整大小后旧「2 倍封顶」不再合理，
# 大屏用户需要更大的简报卡；超出仍由屏幕可用区收口兜底。
MIN_BUBBLE_W = 274
MIN_BUBBLE_H = 180
MAX_BUBBLE_W = 274 * 3
MAX_BUBBLE_H = 180 * 4


def _sanitize_markdown(text: str) -> str:
    """转义原始 HTML，避免字面量标签破坏 md 渲染（2026-10-02 根因）。

    agent 回答里常有字面量标签（如 ``<L>``、``<http://…>``）。Qt 的 markdown
    解析器把未知标签当内联 HTML 原样透传，会污染后续块——实测普通文本里的
    ``<L>`` 会让紧跟其后的 GFM 表格列宽塌缩到每列约 1 个字符（整个表格
    竖着排，完全不可读）。只转义 ``&``（先）与 ``<``：HTML 标签由 ``<``
    起始，``>`` 是 markdown 块引用语法（``> quote``），不能动。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;")


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

        # —— 边缘拖拽改变大小（用户要求：气泡可自由调整尺寸）——
        self._resize_edge = 0          # 拖拽中的边/角掩码（Qt.Edge 组合）
        self._resize_start_global = QPoint()
        self._resize_start_geom = QRect()
        self.setMouseTracking(True)    # 悬停即探测边缘（不按键也要收 move 事件）

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
        # 横向永不出滚动条（用户要求）：代码块/超长 token 把文档理想宽撑得
        # 比视口宽时，宁可折行/裁剪也不横向滚——竖滚动条出现再挤掉 7px 宽度，
        # 文档重排变宽的连锁反应也会被掐断在源头。
        self._body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._body.setLineWrapMode(QTextBrowser.LineWrapMode.WidgetWidth)
        layout.addWidget(self._body, 1)

        # ④ 按钮行：「知道了」（不带 parent：_card 已有 layout，二次安装会告警）
        btn_row = QHBoxLayout()
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

    def _set_markdown(self, text: str) -> None:
        """渲染 md：转义字面量 HTML + 解除代码块不换行（消除横向溢出）。

        两处「太宽」来源：① agent 回答里的字面量标签（如 ``<L>``）会被
        Qt 当内联 HTML，毒化后续表格；② markdown 代码块默认
        ``nonBreakableLines``，长命令行/JSON 不换行会把文档撑宽、右侧被
        裁掉（横向滚动条是关闭的）。转义解决①；逐块清掉②让代码块按宽度
        换行。GFM 表格本身由 QTextDocument 自动适配页宽（实测 501/350/227
        三档均不溢出），无需额外处理。
        """
        self._body.setMarkdown(_sanitize_markdown(text))
        doc = self._body.document()
        cursor = QTextCursor(doc)
        block = doc.begin()
        while block.isValid():
            fmt = block.blockFormat()
            if fmt.nonBreakableLines():
                cursor.setPosition(block.position())
                fmt.setNonBreakableLines(False)
                cursor.setBlockFormat(fmt)
            block = block.next()

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
        self._set_markdown(body[:20000])
        self._show_sized(anchor_rect, wide=True)

    def show_notice(self, *, title: str, message: str, workspace_name: str,
                    anchor_rect) -> None:
        """权限/提问卡（小）：只提示哪个工作区有事要决定，不放选项。"""
        now = datetime.now().strftime("%H:%M")
        self._meta_label.setText(f"{now} {title} · {workspace_name}")
        self._task_label.hide()
        self._set_markdown(message)
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
        # 宽/高：固定 max（2 倍基准），不做内容测量——用户实测反馈：
        # 指令一行装不下的计算不准（被折成两行），高度太高还会挡住桌宠。
        # 固定 548x360 一刀切，md 区滚动兜底长内容。
        self._resize_to(self._max_w, self._max_h)
        self._popup(anchor_rect)

    def _resize_to(self, width: int, height: int) -> None:
        # 预扣竖滚动条宽度：ScrollbarAsNeeded 下滚动条出现会把视口挤窄、文档
        # 重排，个别内容（代码块）会借机横向溢出；固定宽一开始就留出 7px，
        # 滚动条出现前后视口宽度不变，杜绝那条连锁路径（用户要求无横向滚动）。
        self._body.setFixedWidth(width - CARD_MARGIN * 2 - _VSCROLL_RESERVE)
        # 最小尺寸兜底（用户拖得过小），最大不设死——自由拖拽大小由
        # _perform_resize 的钳位与屏幕收口负责（用户要求：可自由改变大小）。
        self._body.setMinimumWidth(MIN_BUBBLE_W - CARD_MARGIN * 2 - _VSCROLL_RESERVE)
        self.setMinimumSize(MIN_BUBBLE_W, MIN_BUBBLE_H)
        self.resize(width, height)

    # ------------------------------------------------------------ 边缘拖拽改变大小

    def _edges_at(self, pos: QPoint) -> int:
        """pos（本窗口坐标）落在哪些边/角上；返回 Qt.Edge 组合，0 = 不在热区。"""
        edges = 0
        if pos.x() <= RESIZE_GRIP:
            edges |= Qt.Edge.LeftEdge.value
        if pos.x() >= self.width() - RESIZE_GRIP:
            edges |= Qt.Edge.RightEdge.value
        if pos.y() <= RESIZE_GRIP:
            edges |= Qt.Edge.TopEdge.value
        if pos.y() >= self.height() - RESIZE_GRIP:
            edges |= Qt.Edge.BottomEdge.value
        return edges

    _CURSORS = {
        Qt.Edge.LeftEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.RightEdge.value: Qt.CursorShape.SizeHorCursor,
        Qt.Edge.TopEdge.value: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeVerCursor,
        Qt.Edge.LeftEdge.value | Qt.Edge.TopEdge.value: Qt.CursorShape.SizeFDiagCursor,
        Qt.Edge.RightEdge.value | Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeFDiagCursor,
        Qt.Edge.LeftEdge.value | Qt.Edge.BottomEdge.value: Qt.CursorShape.SizeBDiagCursor,
        Qt.Edge.RightEdge.value | Qt.Edge.TopEdge.value: Qt.CursorShape.SizeBDiagCursor,
    }

    def _cursor_for(self, edges: int):
        return self._CURSORS.get(edges)

    def mousePressEvent(self, event) -> None:   # noqa (Qt naming)
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._edges_at(event.position().toPoint())
            if edges:
                self._resize_edge = edges
                self._resize_start_global = event.globalPosition().toPoint()
                self._resize_start_geom = QRect(self.pos(), self.size())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:   # noqa (Qt naming)
        pos = event.position().toPoint()
        if self._resize_edge:
            self._perform_resize(event.globalPosition().toPoint())
            event.accept()
            return
        cursor = self._cursor_for(self._edges_at(pos))
        self.setCursor(cursor) if cursor else self.unsetCursor()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:   # noqa (Qt naming)
        if self._resize_edge:
            self._resize_edge = 0
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _perform_resize(self, global_pos: QPoint) -> None:
        """按拖拽位移计算新几何：左/上边移动原点并缩尺寸，右/下边只缩尺寸；
        钳位到 MIN/MAX 后对超出部分回弹原点（拖左/上边时不让窗口平移）。"""
        d = global_pos - self._resize_start_global
        e = self._resize_edge
        left, top = bool(e & Qt.Edge.LeftEdge.value), bool(e & Qt.Edge.TopEdge.value)
        right, bottom = bool(e & Qt.Edge.RightEdge.value), bool(e & Qt.Edge.BottomEdge.value)

        new_w = self._resize_start_geom.width()
        new_h = self._resize_start_geom.height()
        dx = d.x()
        dy = d.y()
        if right:
            new_w += dx
        if left:
            new_w -= dx
        if bottom:
            new_h += dy
        if top:
            new_h -= dy

        # 钳位，并记录被截断的量用于回弹原点
        clamped_w = max(MIN_BUBBLE_W, min(MAX_BUBBLE_W, new_w))
        clamped_h = max(MIN_BUBBLE_H, min(MAX_BUBBLE_H, new_h))

        x = self._resize_start_geom.x()
        y = self._resize_start_geom.y()
        if left:
            x += self._resize_start_geom.width() - clamped_w
        if top:
            y += self._resize_start_geom.height() - clamped_h

        self.setGeometry(x, y, clamped_w, clamped_h)
        # body 固定宽跟随（card margin + 滚动条预留），高度交给 layout 分配
        self._body.setFixedWidth(clamped_w - CARD_MARGIN * 2 - _VSCROLL_RESERVE)

    def leaveEvent(self, event) -> None:   # noqa (Qt naming)
        if not self._resize_edge:
            self.unsetCursor()
        super().leaveEvent(event)

    def _popup(self, anchor_rect) -> None:
        """锚点上方居中弹出 + 屏幕边界收口（四周 SCREEN_PADDING）。

        气泡不跟随桌宠移动（与 SpeechBubble 行为一致）；桌宠移动过来时
        必须保持在气泡之上（用户要求：桌宠永不被本窗口遮挡）——置顶
        层内桌宠 > 气泡的顺序由 AgentLinkManager 在桌宠 show/raise 时
        调 ensure_pet_above() 维持。
        """
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

    def ensure_pet_above(self, pet_window) -> None:
        """把桌宠窗口提到本气泡之上（同为置顶层，仅调整层内顺序）。

        桌宠 show/move/raise 时由 AgentLinkManager 调用；用户要求桌宠
        永不被气泡挡住。气泡可见才需要调整，隐藏时无事可做。
        """
        if not self.isVisible():
            return
        try:
            pet_window.raise_()
            pet_window.activateWindow()
        except RuntimeError:
            pass  # 桌宠窗口底层已销毁
