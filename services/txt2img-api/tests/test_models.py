"""GenerationRequest 的输入校验。

提示词三个字段此前完全没有约束（无长度上限、无 strip），
裸字符串直接进入模板拼接。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import GenerationRequest


class TestTextValidation:
    """text 字段：折叠空白、允许为空、超长拒绝。"""

    def test_text_is_stripped_and_collapsed(self) -> None:
        """首尾空白去除，内部连续空白折叠为单空格。"""
        assert GenerationRequest(text="  咖啡   Latte  ").text == "咖啡 Latte"

    def test_empty_text_is_allowed(self) -> None:
        """空 text 合法：走「只出背景 + 风格」的路径。"""
        assert GenerationRequest(text="").text == ""

    def test_text_over_max_length_rejected(self) -> None:
        """超过 60 字上限应触发校验错误。"""
        with pytest.raises(ValidationError):
            GenerationRequest(text="字" * 61)


class TestPromptValidation:
    """prompt 字段：去空白与长度上限。"""

    def test_prompt_is_stripped(self) -> None:
        """首尾空白应被去除。"""
        assert GenerationRequest(prompt="  水墨  ").prompt == "水墨"

    def test_prompt_over_max_length_rejected(self) -> None:
        """超过 600 字上限应触发校验错误。"""
        with pytest.raises(ValidationError):
            GenerationRequest(prompt="x" * 601)


class TestNegativePromptValidation:
    """negative_prompt 字段：去空白与长度上限。"""

    def test_negative_prompt_is_stripped(self) -> None:
        """首尾空白应被去除。"""
        assert GenerationRequest(negative_prompt="  blurry  ").negative_prompt == "blurry"

    def test_negative_prompt_over_max_length_rejected(self) -> None:
        """超过 600 字上限应触发校验错误。"""
        with pytest.raises(ValidationError):
            GenerationRequest(negative_prompt="x" * 601)


class TestWorkflowValidation:
    """workflow 字段：长度上限。"""

    def test_workflow_over_max_length_rejected(self) -> None:
        """超过 64 字上限应触发校验错误。"""
        with pytest.raises(ValidationError):
            GenerationRequest(workflow="w" * 65)


class TestStyleField:
    """style 是保留字段（当前不参与提示词构造），但仍应有边界。"""

    def test_default_is_default(self) -> None:
        """默认值应为 'default'。"""
        assert GenerationRequest(style="default").style == "default"

    def test_style_over_max_length_rejected(self) -> None:
        """超过 40 字上限应触发校验错误。"""
        with pytest.raises(ValidationError):
            GenerationRequest(style="s" * 41)


class TestResolutionValidation:
    """resolution 字段：格式归一化与非法输入拒绝。"""

    @pytest.mark.parametrize("value", ["1024x1024", "1024 x 1024", "2048X1024"])
    def test_valid_forms_normalize(self, value: str) -> None:
        """带空格 / 大写 X 等写法应归一化为小写无空格的规范形式。"""
        expected = value.strip().lower().replace(" ", "")
        assert GenerationRequest(resolution=value).resolution == expected

    @pytest.mark.parametrize("value", ["", "invalid", "0x0", "1024", "axb", "1024x-5"])
    def test_invalid_forms_rejected(self, value: str) -> None:
        """空串、非数字、非正数、缺少分隔符等一律拒绝。"""
        with pytest.raises(ValidationError):
            GenerationRequest(resolution=value)
