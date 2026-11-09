# -*- coding: utf-8 -*-
"""FlowLayout：隐藏页面时高度不能塌成 0（否则切回页面整行控件消失）。"""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import (QApplication, QLabel, QStackedWidget,  # noqa: E402
                                QVBoxLayout, QWidget)

from gui.widgets import FlowLayout  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _make_strip():
    strip = QWidget()
    lay = FlowLayout(strip, margin=5, spacing=6)
    labels = [QLabel("APP设备"), QLabel("测试APP"), QLabel("设备名称")]
    for lbl in labels:
        lay.addWidget(lbl)
    return strip, lay, labels


def test_height_survives_parent_page_hidden(qapp):
    """切到别的页时 isVisible() 全为 False —— 不能用它判断"隐藏控件"。

    用户实测：在「设备参数」页滚动后切回自动化测试，APP设备那一行整行消失。
    """
    stack = QStackedWidget()
    page = QWidget()
    v = QVBoxLayout(page)
    strip, lay, _labels = _make_strip()
    v.addWidget(strip)
    stack.addWidget(page)
    stack.addWidget(QLabel("设备参数"))
    stack.resize(600, 400)
    stack.show()
    qapp.processEvents()
    shown = lay.heightForWidth(600)
    assert shown > 10

    stack.setCurrentIndex(1)          # 页面被 QStackedWidget 隐藏
    qapp.processEvents()
    assert lay.heightForWidth(600) == shown
    assert lay.minimumSize().width() > 10
    stack.close()


def test_explicitly_hidden_item_does_not_take_space(qapp):
    """自己 hide() 掉的控件仍然不占位（原有行为要保留）。"""
    strip, lay, _labels = _make_strip()
    long_label = QLabel("x" * 200)
    lay.addWidget(long_label)
    strip.resize(600, 100)
    strip.show()
    qapp.processEvents()
    wide = lay.minimumSize().width()
    long_label.hide()
    assert lay.minimumSize().width() < wide
    strip.close()
