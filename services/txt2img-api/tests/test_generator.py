"""generator 模块的单元测试。

覆盖范围：工作流加载与校验、文本分析与路由、提示词渲染与负面词反演、
负面 conditioning 能力探测、节点注入与清理、以及降级兜底。

测试原则：凡是会触达 ComfyUI 的边界一律打桩，断言的是「本模块的产物」而非
mock 的调用行为，从而既能在无 GPU 的机器上秒级跑完，又能验证真实逻辑。
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import pytest

import app.generator as generator

from app.generator import (
    _DEFAULT_NEGATIVE,
    _FLUX_BG_SUPPRESS,
    _FLUX_PROFILE,
    _INVERSION_RULES,
    _ZIMAGE_BG_SUPPRESS,
    _ZIMAGE_PROFILE,
    _analyze_text,
    _build_flux_prompt,
    _build_negative_prompt,
    _build_zimage_prompt,
    _call_comfyui_api,
    _classify_text,
    _detect_layout_intent,
    _detect_workflow_model,
    _find_nodes_by_class,
    _invert_negative,
    _load_workflow,
    _local_stub_generate,
    _node_sort_key,
    _parse_resolution,
    _patch_workflow,
    _probe_negative_capability,
    _resolve_conditioning_nodes,
    _resolve_workflow_path,
    GenerationArtifact,
    generate_artwork,
)
from app.models import GenerationRequest


# ── Fixtures ──


@pytest.fixture
def workflow() -> dict:
    """Load the bundled workflow template."""
    path = _resolve_workflow_path("test_z_image_turbo")
    return _load_workflow(path)


@pytest.fixture
def sample_request() -> GenerationRequest:
    """一个含风格与负面词的典型请求（text 为空，走背景 + 风格路径）。"""
    return GenerationRequest(
        prompt="晨曦之城",
        negative_prompt="模糊, 断裂",
        resolution="1024 x 1024",
        seed=42,
        style="neon",
        format="PNG",
    )


# ── _resolve_workflow_path ──


class TestResolveWorkflowPath:
    """工作流路径解析：默认值、后缀补全与显式后缀保留。"""

    def test_default_path_points_to_existing_file(self) -> None:
        """按名称解析出的路径必须真实存在，且为 .json。"""
        # 解析结果直接指向随包分发的工作流文件
        path = _resolve_workflow_path("test_z_image_turbo")
        assert path.exists(), f"Workflow file should exist at {path}"
        assert path.suffix == ".json"

    def test_default_without_name_uses_flux_schnell(self) -> None:
        """此前的默认值 txt2img_api.json 在 workflows/ 下并不存在，是死分支。"""
        # 不传名称时必须落到真实存在的默认模板
        path = _resolve_workflow_path()
        assert path.name == "flux_schnell.json"
        assert path.exists()

    def test_appends_json_extension(self) -> None:
        """未带后缀的名称应自动补上 .json。"""
        path = _resolve_workflow_path("my_workflow")
        assert path.name == "my_workflow.json"

    def test_preserves_explicit_extension(self) -> None:
        """已带 .json 后缀的名称不应被重复补全。"""
        path = _resolve_workflow_path("flow.json")
        assert path.name == "flow.json"


# ── _load_workflow ──


class TestLoadWorkflow:
    """工作流加载：正常解析与各类非法输入的拒绝。"""

    def test_loads_valid_workflow(self, workflow: dict) -> None:
        """内置模板应能解析为非空 dict。"""
        # fixture 已加载 test_z_image_turbo，此处只校验基本形状
        assert isinstance(workflow, dict)
        assert len(workflow) > 0

    def test_every_node_has_class_type_and_inputs(self, workflow: dict) -> None:
        """每个节点都必须带 class_type 与 inputs，否则不是 API 格式。"""
        # 逐节点校验，错误信息里带上节点 id 便于定位
        for node_id, node in workflow.items():
            assert "class_type" in node, f"Node {node_id} missing class_type"
            assert "inputs" in node, f"Node {node_id} missing inputs"

    def test_raises_on_missing_file(self) -> None:
        """文件不存在时应抛出 FileNotFoundError。"""
        # 故意指向一个不存在的绝对路径
        with pytest.raises(FileNotFoundError):
            _load_workflow(Path("/nonexistent/workflow.json"))

    def test_raises_on_invalid_json(self, tmp_path: Path) -> None:
        """内容不是合法 JSON 时应抛出 JSONDecodeError。"""
        # 写入一段非法 JSON 文本
        f = tmp_path / "bad.json"
        f.write_text("not json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            _load_workflow(f)

    def test_raises_on_non_dict_json(self, tmp_path: Path) -> None:
        """顶层不是 dict 时（如数组）应被拒绝。"""
        # 顶层是数组 → 不是 {node_id: node} 结构
        f = tmp_path / "bad.json"
        f.write_text('["not", "a", "dict"]', encoding="utf-8")
        with pytest.raises(ValueError, match="must be a JSON object"):
            _load_workflow(f)

    def test_raises_on_missing_class_type(self, tmp_path: Path) -> None:
        """节点缺少 class_type 时应报错并指出节点 id。"""
        # 节点只有 inputs、没有 class_type
        f = tmp_path / "bad.json"
        f.write_text('{"1": {"inputs": {}}}', encoding="utf-8")
        with pytest.raises(ValueError, match="missing 'class_type'"):
            _load_workflow(f)


# ── _find_nodes_by_class ──


class TestFindNodesByClass:
    """按 class_type 定位节点。"""

    def test_finds_clip_text_encode(self, workflow: dict) -> None:
        """模板中至少存在一个 CLIPTextEncode 文本节点。"""
        # 文本节点数量不固定，只要求至少一个
        nodes = _find_nodes_by_class(workflow, "CLIPTextEncode")
        assert len(nodes) >= 1

    def test_finds_ksampler(self, workflow: dict) -> None:
        """模板中恰有一个 KSampler。"""
        nodes = _find_nodes_by_class(workflow, "KSampler")
        assert len(nodes) == 1

    def test_finds_save_image(self, workflow: dict) -> None:
        """模板中恰有一个 SaveImage 输出节点。"""
        nodes = _find_nodes_by_class(workflow, "SaveImage")
        assert len(nodes) == 1

    def test_finds_empty_sd3_latent(self, workflow: dict) -> None:
        """模板中恰有一个 EmptySD3LatentImage 潜空间节点。"""
        nodes = _find_nodes_by_class(workflow, "EmptySD3LatentImage")
        assert len(nodes) == 1

    def test_returns_empty_for_unknown_class(self, workflow: dict) -> None:
        """未知 class_type 应返回空列表而非报错。"""
        # 查找不存在的节点类型是合法用法（用于可选节点的探测）
        nodes = _find_nodes_by_class(workflow, "NonExistentNode")
        assert nodes == []

    def test_returns_node_id_and_data(self, workflow: dict) -> None:
        """返回项应为 (node_id, node_data) 二元组。"""
        # 调用方依赖这个解包顺序
        nodes = _find_nodes_by_class(workflow, "SaveImage")
        nid, node = nodes[0]
        assert isinstance(nid, str)
        assert node["class_type"] == "SaveImage"


# ── _patch_workflow ──


class TestPatchWorkflow:
    """参数注入：提示词、种子、分辨率，以及不污染原模板。"""

    def test_sets_positive_prompt(self, workflow: dict, sample_request: GenerationRequest) -> None:
        """sample_request 的 text 为空，正向串 = 背景抑制 + 风格（不再短路）。"""
        patched = _patch_workflow(workflow, sample_request)
        clip_nodes = _find_nodes_by_class(patched, "CLIPTextEncode")
        text = clip_nodes[0][1]["inputs"]["text"]
        assert _ZIMAGE_BG_SUPPRESS in text
        assert "晨曦之城" in text
        assert text != "晨曦之城"

    def test_sets_seed(self, workflow: dict, sample_request: GenerationRequest) -> None:
        """请求中的种子应写入 KSampler。"""
        # sample_request 的 seed 为 42
        patched = _patch_workflow(workflow, sample_request)
        ksamplers = _find_nodes_by_class(patched, "KSampler")
        assert ksamplers[0][1]["inputs"]["seed"] == 42

    def test_sets_resolution(self, workflow: dict, sample_request: GenerationRequest) -> None:
        """请求中的分辨率应写入潜空间节点的宽高。"""
        patched = _patch_workflow(workflow, sample_request)
        # User's workflow uses EmptySD3LatentImage
        latents = _find_nodes_by_class(patched, "EmptySD3LatentImage")
        assert latents[0][1]["inputs"]["width"] == 1024
        assert latents[0][1]["inputs"]["height"] == 1024

    def test_does_not_mutate_original(self, workflow: dict, sample_request: GenerationRequest) -> None:
        """注入应作用于深拷贝，原始模板不得被修改。"""
        # 先记下原始文本，patch 后应保持不变
        original_text = _find_nodes_by_class(workflow, "CLIPTextEncode")[0][1]["inputs"]["text"]
        _patch_workflow(workflow, sample_request)
        assert _find_nodes_by_class(workflow, "CLIPTextEncode")[0][1]["inputs"]["text"] == original_text

    def test_sets_seed_zero(self, workflow: dict) -> None:
        """seed=0 是合法值，必须被真实写入而非被当作「未设置」。"""
        # 这是「假值被误判为空」的典型回归点
        req = GenerationRequest(prompt="test", seed=0)
        patched = _patch_workflow(workflow, req)
        ksamplers = _find_nodes_by_class(patched, "KSampler")
        assert ksamplers[0][1]["inputs"]["seed"] == 0


# ── _parse_resolution ──


class TestParseResolution:
    """分辨率字符串解析与非法输入兜底。"""

    # 前四组为合法写法（含空格 / 大写 X），后三组为非法输入（应回退默认值）
    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            ("1024x1024", (1024, 1024)),
            ("1024 x 1024", (1024, 1024)),
            ("2048x1024", (2048, 1024)),
            ("1024 X 768", (1024, 768)),
            ("", (1024, 1024)),
            ("invalid", (1024, 1024)),
            ("0x0", (1024, 1024)),
        ],
    )
    def test_parses_various_formats(self, input_str: str, expected: tuple[int, int]) -> None:
        """各种合法写法正确解析；非法输入一律回退默认尺寸。"""
        # 直接比对返回值，无需捕获异常（内部已兜底）
        assert _parse_resolution(input_str) == expected


# ── _local_stub_generate ──


class TestLocalStubGenerate:
    """本地降级兜底产物的形状与确定性。"""

    def test_returns_data_url(self, sample_request: GenerationRequest) -> None:
        """产物应为 PNG data URL。"""
        # stub 也必须返回可直接渲染的 data URL，前端无需区分引擎
        artifact = _local_stub_generate(sample_request)
        assert artifact.image_base64.startswith("data:image/png;base64,")

    def test_returns_png_extension(self, sample_request: GenerationRequest) -> None:
        """产物文件名应以 .png 结尾。"""
        # 文件名由提示词派生，扩展名固定为 png
        artifact = _local_stub_generate(sample_request)
        assert artifact.image_name.endswith(".png")

    def test_includes_metadata(self, sample_request: GenerationRequest) -> None:
        """元数据应带降级引擎标记并回显请求参数。"""
        # engine=local-studio 是判定「已降级」的关键字段
        artifact = _local_stub_generate(sample_request)
        assert artifact.metadata["engine"] == "local-studio"
        assert artifact.metadata["prompt"] == "晨曦之城"
        assert artifact.metadata["seed"] == 42

    def test_deterministic_seed(self) -> None:
        """同一请求重复调用应得到相同种子（可复现）。"""
        # 显式种子应原样回填，不受随机性影响
        req = GenerationRequest(prompt="test", seed=123)
        a1 = _local_stub_generate(req)
        a2 = _local_stub_generate(req)
        assert a1.metadata["seed"] == a2.metadata["seed"] == 123

    def test_slug_contains_prompt_text(self) -> None:
        """文件名 slug 应包含提示词中的可读文本。"""
        # slug 用于让产物文件名可读、可检索
        req = GenerationRequest(prompt="hello-world")
        artifact = _local_stub_generate(req)
        assert "hello-world" in artifact.image_name


# ── _classify_text ──


class TestClassifyText:
    """文本分类只统计字母，数字/符号/空白不参与判定。"""

    # 纯汉字样本
    @pytest.mark.parametrize("text", ["满庭芳", "念奴娇", "醉太平", "墨心堂"])
    def test_pure_chinese(self, text: str) -> None:
        """纯汉字应判为 chinese。"""
        assert _classify_text(text) == "chinese"

    # 纯拉丁字母样本
    @pytest.mark.parametrize("text", ["ICE CRUSH", "TECHNO CORE", "VINTAGE CAFE"])
    def test_pure_english(self, text: str) -> None:
        """纯拉丁字母应判为 english。"""
        assert _classify_text(text) == "english"

    # 汉字与拉丁字母并存的样本（含数字 / 符号也仍是 mixed）
    @pytest.mark.parametrize(
        "text",
        [
            "咖啡 Latte 2.0",
            "溯源 Source Code",
            "汇智 AI Lab",
            "灵动 UI Design",
            "冰川 Ice 100%",
        ],
    )
    def test_mixed_chinese_and_latin(self, text: str) -> None:
        """汉字与拉丁字母并存应判为 mixed。"""
        assert _classify_text(text) == "mixed"

    # 汉字 + 数字：数字不参与脚本判定
    @pytest.mark.parametrize("text", ["冰川 100%", "满庭芳 2026"])
    def test_digits_are_invisible_to_detection(self, text: str) -> None:
        """中文+数字（无拉丁字母）仍算纯中文。"""
        assert _classify_text(text) == "chinese"

    # 纯数字 / 纯符号 / 空串：无字母，统一并入 english
    @pytest.mark.parametrize("text", ["2026", "100%", "3.14", "!!!", "", "   ", "---"])
    def test_no_letters_falls_back_to_english(self, text: str) -> None:
        """无字母（纯数字/符号/空）并入英文链，与既有行为一致。"""
        assert _classify_text(text) == "english"


# ── _analyze_text / TextAnalysis ──


class TestTextAnalysis:
    """统一文本分析：路由与模板共用的唯一真相源。

    residue 永不包含汉字，这是修复「文字被渲染两遍」的关键。
    """

    # 覆盖各脚本类型、含数字 / 符号 / 空白的混合输入，以及空串等边界
    SAMPLES = [
        "满庭芳", "念奴娇", "醉太平", "墨心堂",
        "ICE CRUSH", "TECHNO CORE", "VINTAGE CAFE",
        "咖啡 Latte 2.0", "溯源 Source Code", "汇智 AI Lab",
        "灵动 UI Design", "冰川 Ice 100%", "幻影 Phantom X",
        "单依纯 X", "冰川 100%", "满庭芳 2026",
        "2026", "100%", "3.14", "!!!", "", "   ", "---",
        "咪哄之风 98% hey you", "单依纯 0 egg",
    ]

    def test_classify_is_thin_wrapper(self) -> None:
        """_classify_text 必须与 TextAnalysis.script 逐位一致。"""
        # 逐样本比对，任一不一致即说明两份实现已分叉
        for s in self.SAMPLES:
            assert _classify_text(s) == _analyze_text(s).script, s

    def test_residue_drops_all_hanzi(self) -> None:
        """residue 应剔除全部汉字，保留其余部分。"""
        # 汉字被剔除后，拉丁与数字应原样保留
        assert _analyze_text("咪哄之风 98% hey you").residue == "98% hey you"

    def test_residue_of_pure_chinese_is_empty(self) -> None:
        """纯中文的 residue 应为空串。"""
        # 空 residue 意味着不会生成多余的「同时包含」子句
        assert _analyze_text("满庭芳").residue == ""

    def test_residue_keeps_latin_and_digits(self) -> None:
        """residue 应保留拉丁与数字部分。"""
        # 两端的分隔符与空白应被剥离，只留有效内容
        assert _analyze_text("咖啡 Latte 2.0").residue == "Latte 2.0"
        assert _analyze_text("冰川 Ice 100%").residue == "Ice 100%"

    def test_residue_of_single_latin_letter(self) -> None:
        """缺陷 2 的窄条件：拉丁片段最长仅为 1 时，旧模板会把它丢掉。"""
        # max_latin_run==1 是旧模板漏字的触发条件
        a = _analyze_text("单依纯 X")
        assert a.residue == "X"
        assert a.script == "mixed"
        assert a.max_latin_run == 1

    def test_residue_never_contains_hanzi(self) -> None:
        """守门：无论何种输入，residue 都不得含汉字。"""
        # residue 含汉字会让「同时包含」子句把汉字再画一遍，故必须为空
        for s in ["咪哄之风 98% hey you", "单依纯 0 egg", "咖啡 Latte 2.0", "满庭芳"]:
            assert not re.search(r"[一-鿿]", _analyze_text(s).residue), s

    def test_hanzi_spaced_is_single_space(self) -> None:
        """汉字应逐字以单空格分隔。"""
        # 逐字拆分是为了强制模型按字数逐个绘制
        assert _analyze_text("咖啡 Latte 2.0").hanzi_spaced == "咖 啡"
        assert _analyze_text("咪哄之风 98% hey you").hanzi_spaced == "咪 哄 之 风"

    def test_hanzi_count(self) -> None:
        """汉字计数应准确。"""
        assert _analyze_text("咪哄之风 98% hey you").hanzi_count == 4
        assert _analyze_text("满庭芳").hanzi_count == 3

    def test_symbols_dedup_and_order(self) -> None:
        """符号应去重并保持首次出现顺序。"""
        # "%" 重复出现两次，去重后仍排在 "." 之前
        assert _analyze_text("100% 3.14%").symbols == ("%", ".")

    def test_symbols_empty_without_symbols(self) -> None:
        """无符号时返回空元组。"""
        # 空元组使符号子句的触发条件为假
        assert _analyze_text("满庭芳").symbols == ()
        assert _analyze_text("ICE CRUSH").symbols == ()

    def test_symbols_spaced(self) -> None:
        """符号应逐字以单空格分隔。"""
        # 便于模板逐个点名每个符号
        assert _analyze_text("100% 3.14%").symbols_spaced == "% ."

    def test_is_empty(self) -> None:
        """空串与纯空白都应判为空；含内容则否。"""
        # is_empty 用于决定是否发版式段
        assert _analyze_text("").is_empty
        assert _analyze_text("   ").is_empty
        assert not _analyze_text("满庭芳").is_empty

    def test_stripped_collapses_internal_whitespace(self) -> None:
        """stripped 应折叠内部连续空白。"""
        # 与 GenerationRequest.normalize_text 的行为保持一致
        assert _analyze_text("  咖啡   Latte  ").stripped == "咖啡 Latte"

    def test_analysis_is_cached(self) -> None:
        """相同输入应命中 lru_cache，返回同一对象。"""
        # 同一对象（而非仅相等）证明命中了缓存
        assert _analyze_text("满庭芳") is _analyze_text("满庭芳")

    def test_latin_runs(self) -> None:
        """拉丁片段应被完整提取。"""
        # 数字不算拉丁片段，故只应有 "Latte"
        a = _analyze_text("咖啡 Latte 2.0")
        assert a.latin_runs == ("Latte",)
        assert a.has_latin

    def test_digit_runs(self) -> None:
        """数字串应被完整提取（含小数）。"""
        # 小数点被视为数字的一部分，不会被拆成两段
        assert _analyze_text("咖啡 Latte 2.0").digit_runs == ("2.0",)
        assert _analyze_text("满庭芳").digit_runs == ()

    def test_script_unchanged_for_digit_symbol_inputs(self) -> None:
        """无字母输入一律归入 english，保持与既有路由契约一致。"""
        # 这是历史契约，改动会影响路由分派，故用参数化锁死
        for s in ["2026", "100%", "3.14", "!!!", "", "   ", "---"]:
            assert _analyze_text(s).script == "english", s


# ── generate_artwork (fallback) ──


class TestGenerateArtwork:
    """顶层入口在降级场景下的行为。"""

    def test_metadata_includes_fallback_fields(self, sample_request: GenerationRequest) -> None:
        """无论走 ComfyUI 还是 stub，metadata 必须包含降级链追踪字段。"""
        artifact = generate_artwork(sample_request)
        # 无 ComfyUI 环境下会降级到 stub，故只断言两种引擎都合法的形状
        assert artifact.image_base64.startswith("data:image/png;base64,")
        assert artifact.image_name.endswith(".png")
        assert artifact.metadata["engine"] in ("comfyui", "local-studio")
        assert "fallback_tier" in artifact.metadata
        assert "workflow_used" in artifact.metadata
        assert "attempted_workflows" in artifact.metadata

    def test_stub_metadata_matches_request(self, sample_request: GenerationRequest) -> None:
        """元数据应回显请求的提示词、种子与归一化后的分辨率。"""
        artifact = generate_artwork(sample_request)
        # 元数据须回显请求参数，分辨率还应被归一化
        assert artifact.metadata["prompt"] == "晨曦之城"
        assert artifact.metadata["seed"] == 42
        assert artifact.metadata["resolution"] == "1024x1024"


# ── 路由分派 ──


class TestRouting:
    """混排只走 z-image 单层链；纯中文/纯英文维持原链。"""

    @staticmethod
    def _attempted_workflows(monkeypatch: pytest.MonkeyPatch, text: str) -> list[str]:
        """打桩 ComfyUI 边界后跑一次生成，返回实际尝试过的工作流序列。

        打桩的是 ComfyUI 边界（而非断言 mock 行为），否则在装有 ComfyUI 的
        机器上会真的跑一次最长 900s 的生成。断言的是真实路由产物。
        """
        monkeypatch.setattr("app.generator._call_comfyui_api", lambda request, workflow: None)
        artifact = generate_artwork(GenerationRequest(text=text, prompt="test style"))
        return artifact.metadata["attempted_workflows"]

    def test_mixed_text_targets_z_image_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """中英混排只尝试 z-image 单层链。"""
        # 混排无兜底候选，链长恰为 1
        assert self._attempted_workflows(monkeypatch, "咖啡 Latte 2.0") == ["test_z_image_turbo"]

    def test_pure_chinese_targets_qwen_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """纯中文按 Qwen → Z-Image 顺序尝试。"""
        # 顺序即优先级，Qwen 更适合中文
        assert self._attempted_workflows(monkeypatch, "满庭芳") == [
            "qwen_image_2512_gguf",
            "test_z_image_turbo",
        ]

    def test_pure_english_targets_flux_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """纯英文按 Flux → Z-Image 顺序尝试。"""
        # 顺序即优先级，Flux 更适合拉丁字形
        assert self._attempted_workflows(monkeypatch, "ICE CRUSH") == [
            "flux_schnell",
            "test_z_image_turbo",
        ]


class TestMixedTextPromptTemplate:
    """提示词模板跟的是「工作流」而非「路由分类」。

    这是症状 1 修复的实际机制：把混排内容路由到 z-image 之后，必须
    拿到中文模板，否则中文笔画问题原样存在。
    """

    def test_mixed_text_gets_zimage_chinese_template(self) -> None:
        """混排内容落到 z-image 时应使用中文模板而非英文模板。"""
        workflow = _load_workflow(_resolve_workflow_path("test_z_image_turbo"))
        req = GenerationRequest(text="咖啡 Latte 2.0", prompt="咖啡质感")
        patched = _patch_workflow(workflow, req)
        # 取注入后的文本节点内容，确认用的是中文模板
        text = _find_nodes_by_class(patched, "CLIPTextEncode")[0][1]["inputs"]["text"]
        assert _ZIMAGE_BG_SUPPRESS in text
        assert _FLUX_BG_SUPPRESS not in text

    def test_mixed_text_prompt_keeps_both_scripts(self) -> None:
        """混排模板需同时点名汉字（逐字拆分）与拉丁部分，且原文整串不得出现。"""
        workflow = _load_workflow(_resolve_workflow_path("test_z_image_turbo"))
        req = GenerationRequest(text="咖啡 Latte 2.0", prompt="咖啡质感")
        patched = _patch_workflow(workflow, req)
        text = _find_nodes_by_class(patched, "CLIPTextEncode")[0][1]["inputs"]["text"]
        assert "咖 啡" in text       # 中文逐字拆开，强调字数为 2
        assert "Latte 2.0" in text   # 英文部分独立点名
        assert '"咖啡 Latte 2.0"' not in text   # 原文整串不得出现，否则汉字被画两遍


# ── Prompt profile rendering ──


class TestPromptProfileRendering:
    """profile 化渲染：段序、内容规则、布局意图。"""

    def test_style_precedes_content_and_accuracy(self) -> None:
        """段序：背景 -> 风格 -> 版式 -> 内容 -> 约束。

        用户把布局写进 prompt（不拆字段），所以「风格段提前」就是
        「用户意图排到约束之前」的落地方式。
        """
        result = _build_flux_prompt("咖啡 Latte 2.0", "咖啡质感")
        # 用各段的起始下标比较先后顺序
        i_bg = result.index(_FLUX_BG_SUPPRESS)
        i_style = result.index("咖啡质感")
        i_content = result.index('Chinese text "咖 啡"')
        i_accuracy = result.index("crisp letterforms")
        assert i_bg < i_style < i_content < i_accuracy

    def test_full_raw_text_is_never_quoted(self) -> None:
        """缺陷 1 守门：原始文本不得作为整体引号串出现，否则汉字会被画两遍。"""
        raw = "咖啡 Latte 2.0"
        # 整串被引号包裹即意味着汉字会随整串再画一遍
        assert f'"{raw}"' not in _build_flux_prompt(raw, "咖啡质感")

        raw2 = "咪哄之风 98% hey you"
        assert f'"{raw2}"' not in _build_zimage_prompt(raw2, "酷炫")

    def test_residue_clause_names_latin_without_hanzi(self) -> None:
        """residue 子句应点名拉丁与数字，汉字则逐字拆分，原文整串不出现。"""
        # 三条断言共同确保「汉字只被画一次」
        result = _build_zimage_prompt("咪哄之风 98% hey you", "酷炫")
        assert '"98% hey you"' in result      # residue 点名拉丁与数字
        assert "咪 哄 之 风" in result         # 汉字逐字拆分
        assert "咪哄之风" not in result        # 原文不再整串出现

    def test_single_latin_letter_is_named(self) -> None:
        """缺陷 2：单字母此前被模板的 [a-zA-Z]{2,} 漏掉。"""
        # 两个家族模板都必须点名这个单字母
        assert '"X"' in _build_zimage_prompt("单依纯 X", "简洁")
        assert '"X"' in _build_flux_prompt("单依纯 X", "简洁")

    def test_symbol_clause_fires_for_latin_with_symbol(self) -> None:
        """拉丁 + 符号时应触发符号点名子句。"""
        # 无汉字时符号需单独点名，不能只靠 residue
        result = _build_zimage_prompt("ICE CRUSH 100%", "冰霜")
        assert '包含符号"%"' in result

    def test_symbol_clause_absent_without_symbols(self) -> None:
        """无符号时不得出现符号子句。"""
        # 中英文模板都不应产出符号子句
        assert "包含符号" not in _build_zimage_prompt("满庭芳", "水墨")
        assert "includes the symbol" not in _build_flux_prompt("满庭芳", "水墨")

    def test_digits_clause_suppressed_when_residue_present(self) -> None:
        """中文+数字由 residue 一次性点名，不再另起数字子句重复强调。"""
        # 这验证 suppresses 机制确实生效
        result = _build_zimage_prompt("冰川 100%", "冰蓝")
        assert '"100%"' in result
        assert "数字大小比例正确" not in result

    def test_digits_clause_fires_for_latin_with_digits(self) -> None:
        """无汉字时数字需单独触发数字子句。"""
        # 没有 residue 兜底，数字必须由专用子句约束
        result = _build_zimage_prompt("ICE 2026", "冰霜")
        assert "数字大小比例正确" in result

    # 中英文版式关键词都应被识别
    @pytest.mark.parametrize(
        "style",
        ["居中排版", "文字放在中间", "0在底层中间 egg小字右下角", "layout: centered", "text aligned left"],
    )
    def test_neutral_layout_when_prompt_has_layout_words(self, style: str) -> None:
        """用户已表达版式意图时应发中性版式段。"""
        result = _build_zimage_prompt("满庭芳", style)
        assert _ZIMAGE_PROFILE.layout_neutral in result
        assert _ZIMAGE_PROFILE.layout not in result

    # 纯风格描述（无版式词）应落到默认版式段
    @pytest.mark.parametrize("style", ["水墨晕染", "墨绿色金边", "neon gradient"])
    def test_default_layout_used_when_no_intent(self, style: str) -> None:
        """用户未表达版式意图时应发默认版式段。"""
        result = _build_zimage_prompt("满庭芳", style)
        assert _ZIMAGE_PROFILE.layout in result
        assert _ZIMAGE_PROFILE.layout_neutral not in result

    def test_no_layout_clause_for_empty_text(self) -> None:
        """没有文字就无需版式引导。"""
        # 空文本时 layout 段应整体缺席
        result = _build_flux_prompt("", "minimal")
        assert _FLUX_PROFILE.layout not in result
        assert _FLUX_PROFILE.layout_neutral not in result

    # 版式意图探测的真值表：前五组命中，后四组不命中
    @pytest.mark.parametrize(
        ("style", "expected"),
        [
            ("居中排版", True),
            ("文字放在中间", True),
            ("0在底层中间 egg小字右下角", True),
            ("layout: centered", True),
            ("text aligned left", True),
            ("水墨晕染", False),
            ("墨绿色金边", False),
            ("neon gradient", False),
            ("", False),
        ],
    )
    def test_layout_intent_detector_table(self, style: str, expected: bool) -> None:
        """版式意图探测的真值表：命中关键词为 True，否则 False。"""
        assert _detect_layout_intent(style) is expected


# ── 负面词能力探测 ──


class TestNegativeCapability:
    """负面 conditioning 是否真正生效：cfg 与节点结构是与门，两者都要看。"""

    # 三个内置工作流的实测结果
    @pytest.mark.parametrize(
        ("name", "effective", "reason", "cfg", "neg_node"),
        [
            # cfg=1.0 使负面 conditioning 被忽略，尽管节点 9 是正常的文本节点
            ("flux_schnell", False, "cfg_too_low", 1.0, "9"),
            # 唯一 cfg>1 的工作流，负面真正生效
            ("qwen_image_2512_gguf", True, "ok", 2.5, "5"),
            # 负向由 ConditioningZeroOut 从正向派生，且 cfg=1
            ("test_z_image_turbo", False, "derived_from_positive", 1.0, None),
        ],
    )
    def test_real_workflows(
        self, name: str, effective: bool, reason: str, cfg: float, neg_node: str | None
    ) -> None:
        """三个内置工作流的能力判定结果（是否生效、主因、cfg、负面节点）。"""
        workflow = _load_workflow(_resolve_workflow_path(name))
        cap = _probe_negative_capability(workflow)
        # 四项同时校验：是否生效、主因、cfg 取值、负面节点 id
        assert cap.effective is effective
        assert cap.reason == reason
        assert cap.cfg == cfg
        assert cap.negative_node_id == neg_node

    def test_derived_from_positive_detected(self) -> None:
        """z-image 的负向由正向派生，且 cfg 过低，两个阻断原因应同时记录。"""
        # 该用例覆盖「多因并存」的判定路径
        workflow = _load_workflow(_resolve_workflow_path("test_z_image_turbo"))
        cap = _probe_negative_capability(workflow)
        assert cap.derived_from_positive is True
        assert cap.negative_chain_class == "ConditioningZeroOut"
        assert "derived_from_positive" in cap.blockers
        assert "cfg_too_low" in cap.blockers     # 两个原因同时存在

    # 覆盖阈值两侧：<= 1.05 不生效，> 1.05 生效
    @pytest.mark.parametrize(("cfg", "effective"), [(1.0, False), (1.05, False), (1.5, True), (2.5, True)])
    def test_cfg_threshold(self, cfg: float, effective: bool) -> None:
        """cfg 阈值边界：<= 1.05 判为不生效，> 1.05 生效。"""
        # 最小工作流：两个文本节点 + 一个可调 cfg 的采样器
        workflow = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "3": {
                "class_type": "KSampler",
                "inputs": {"positive": ["1", 0], "negative": ["2", 0], "cfg": cfg},
            },
        }
        assert _probe_negative_capability(workflow).effective is effective

    def test_missing_cfg_is_blocked(self) -> None:
        """采样器缺少 cfg 时按「过低」处理。"""
        # 最小工作流，但采样器刻意省略 cfg 字段
        workflow = {
            "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "3": {"class_type": "KSampler", "inputs": {"positive": ["1", 0], "negative": ["2", 0]}},
        }
        cap = _probe_negative_capability(workflow)
        assert cap.effective is False
        assert cap.reason == "cfg_too_low"

    def test_no_sampler_blocked(self) -> None:
        """完全没有采样器时，负面无法生效。"""
        # 只有一个文本节点，完全没有采样器
        workflow = {"1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}}}
        cap = _probe_negative_capability(workflow)
        assert cap.effective is False
        assert cap.reason == "no_sampler"


class TestConditioningNodeResolution:
    """节点识别顺 KSampler 连线解析，不再依赖 JSON 字典插入序。

    字典序是脆弱契约：ComfyUI 重新导出一次就可能打乱键顺序，导致正负向互换。
    """

    def test_sampler_link_beats_dict_order(self) -> None:
        """字典序脆弱性回归：即使 "9" 写在 "4" 之前，正负向仍按连线判定。"""
        workflow = {
            "9": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},   # 负向，却先出现
            "4": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "3": {
                "class_type": "KSampler",
                "inputs": {"positive": ["4", 0], "negative": ["9", 0], "cfg": 2.5},
            },
        }
        res = _resolve_conditioning_nodes(workflow)
        assert res.positive_node_id == "4"
        assert res.negative_node_id == "9"

    def test_numeric_aware_fallback_sort(self) -> None:
        """无 sampler 时按数字感知 id 序回退："57:27" 应排在 "9" 之后。"""
        # 纯字符串比较会得出 "57:27" < "9"，数字感知排序修正了这一点
        assert _node_sort_key("57:27") == (57, 27)
        assert _node_sort_key("9") == (9,)
        assert _node_sort_key("9") < _node_sort_key("57:27")

        workflow = {
            "57:27": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "9": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
        }
        res = _resolve_conditioning_nodes(workflow)
        assert res.positive_node_id == "9"
        assert res.negative_node_id == "57:27"

    def test_flux_resolution(self) -> None:
        """flux 工作流：正向 4、负向 9，负向非派生。"""
        workflow = _load_workflow(_resolve_workflow_path("flux_schnell"))
        res = _resolve_conditioning_nodes(workflow)
        # flux 的正负向是两个独立的 CLIPTextEncode 节点
        assert res.positive_node_id == "4"
        assert res.negative_node_id == "9"
        assert res.derived_from_positive is False

    def test_zimage_has_no_negative_text_node(self) -> None:
        """z-image 结构上没有负面文本节点，且负向由正向派生。"""
        workflow = _load_workflow(_resolve_workflow_path("test_z_image_turbo"))
        res = _resolve_conditioning_nodes(workflow)
        # 只有一个文本节点（正向），负向槽位指向 ConditioningZeroOut
        assert res.positive_node_id == "57:27"
        assert res.negative_node_id is None
        assert res.derived_from_positive is True


# ── 负面词语义反演 ──


class TestNegativeInversion:
    """「否定 → 肯定」反演。

    核心不变量：进入正向串的只能是词表字面量，用户负面词本身永不注入；
    已被正向段覆盖的语义只记账、不重复注入；词表未覆盖的词只上报、不注入。
    """

    def test_no_rule_clause_is_affirmative(self) -> None:
        """守门：反演子句本身不得含否定词（否则等于把缺陷写进正向串）。"""
        # 逐条规则检查英文与中文子句，任一含否定词即失败
        for rule in _INVERSION_RULES:
            assert not re.search(r"\b(no|not|without)\b", rule.clause_en, re.I), rule.key
            assert not re.search(r"[无没不]", rule.clause_zh), rule.key

    def test_default_negative_injects_only_uncovered(self) -> None:
        """默认负面词中，仅「未被正向段覆盖」的两条会真正注入。"""
        res = _invert_negative(_DEFAULT_NEGATIVE, _ZIMAGE_PROFILE)
        # 只有「清晰度」与「无水印」两条需要注入，其余均已被正向段覆盖
        assert res.clauses == ("边缘锐利、高清、矢量感清晰的字形", "画面只有字形本身，干净利落")
        assert "complex_background" in res.covered
        assert "broken_missing_strokes" in res.covered
        assert res.unmapped_terms == ()

    def test_en_profile_uses_english_clauses(self) -> None:
        """英文 profile 应取英文反演子句。"""
        # 同一语义在英文 profile 下应产出英文子句
        res = _invert_negative("blurry text, watermark", _FLUX_PROFILE)
        assert res.clauses == (
            "sharp high-resolution edges, crisp vector-clean outlines",
            "clean unmarked typography, only the lettering itself",
        )

    def test_match_is_case_insensitive(self) -> None:
        """匹配应大小写不敏感。"""
        # 干草堆会转小写，故大小写混写的输入也能命中
        res = _invert_negative("Blurry Text", _ZIMAGE_PROFILE)
        assert "blurry_low_quality" in res.matched

    def test_unmapped_terms_reported_and_never_injected(self) -> None:
        """守门：词表覆盖不到的用户负面词绝不进入正向串。"""
        res = _invert_negative("赛博朋克, blob", _ZIMAGE_PROFILE)
        assert res.unmapped_terms == ("赛博朋克", "blob")
        assert res.clauses == ()
        assert not any("赛博朋克" in c or "blob" in c for c in res.clauses)

    def test_clause_not_duplicated_when_already_present(self) -> None:
        """正向串已含等价内容时，该子句不再重复注入。"""
        # 把该子句预先放进正向串，反演应识别出重复而不注入
        clause = "边缘锐利、高清、矢量感清晰的字形"
        res = _invert_negative("blurry text", _ZIMAGE_PROFILE, already_present=f"酷炫，{clause}")
        assert clause not in res.clauses

    def test_empty_negative_yields_nothing(self) -> None:
        """空负面词应产出空结果。"""
        # 四个字段都应为空，避免误注入
        res = _invert_negative("", _ZIMAGE_PROFILE)
        assert res.clauses == ()
        assert res.matched == ()
        assert res.covered == ()
        assert res.unmapped_terms == ()

    def test_terms_split_on_both_punctuation_styles(self) -> None:
        """中英文标点混用时都应正确切分并命中规则。"""
        # 中文逗号、分号混用也要能正确切分并命中规则
        res = _invert_negative("blurry text，模糊; watermark", _ZIMAGE_PROFILE)
        assert res.unmapped_terms == ()
        assert "blurry_low_quality" in res.matched
        assert "watermark" in res.matched


# ── 注入点清理 ──


class _BoomClient:
    """进入上下文成功、发起请求时失败——保证代码走到 patch 那一步才进 except。"""

    def __init__(self, *args: object, **kwargs: object) -> None:
        # 忽略全部构造参数（httpx.Client 会传入 timeout 等）
        pass

    def __enter__(self) -> "_BoomClient":
        # 进入上下文成功，把失败推迟到真正发请求时
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def post(self, *args: object, **kwargs: object) -> None:
        # 提交工作流时抛错，模拟 ComfyUI 不可达
        raise RuntimeError("no comfyui in tests")

    def get(self, *args: object, **kwargs: object) -> None:
        # 轮询阶段同样抛错
        raise RuntimeError("no comfyui in tests")


class TestPatchWorkflowCleanup:
    """注入点清理的三条不变量。

    1. 只写解析出的正负向节点，游离文本节点不动；
    2. patch 由调用方负责，``_call_comfyui_api`` 不重复 patch；
    3. flux 的 ``clip_l`` 与 ``t5xxl`` 分别收短摘要与全长。
    """

    def test_patch_is_idempotent(self, workflow: dict, sample_request: GenerationRequest) -> None:
        """对同一模板重复 patch 应得到相同结果（幂等）。"""
        # 对已 patch 的结果再 patch 一次，结果应完全一致
        once = _patch_workflow(workflow, sample_request)
        twice = _patch_workflow(once, sample_request)
        assert once == twice

    def test_call_comfyui_api_does_not_repatch(
        self, monkeypatch: pytest.MonkeyPatch, workflow: dict
    ) -> None:
        """patch 由调用方负责；_call_comfyui_api 不得再 patch 一次。"""
        calls: list[int] = []
        real = generator._patch_workflow
        monkeypatch.setattr(
            generator,
            "_patch_workflow",
            lambda w, r, **kw: (calls.append(1), real(w, r))[1],
        )
        monkeypatch.setattr(generator.httpx, "Client", _BoomClient)

        _call_comfyui_api(GenerationRequest(text="x", prompt="y"), workflow)
        assert calls == []

    def test_unreferenced_encode_node_untouched(self, workflow: dict) -> None:
        """未接线的文本节点不得被灌入提示词。"""
        # 追加一个游离的文本节点，它不在采样器连线上，不应被写入
        workflow = copy.deepcopy(workflow)
        workflow["extra"] = {"class_type": "CLIPTextEncode", "inputs": {"text": "KEEP"}}
        patched = _patch_workflow(workflow, GenerationRequest(text="满庭芳", prompt="水墨"))
        assert patched["extra"]["inputs"]["text"] == "KEEP"

    def test_flux_resolved_nodes_get_positive_and_negative(self) -> None:
        """flux 的正负向节点应分别收到不同的文本。"""
        workflow = _load_workflow(_resolve_workflow_path("flux_schnell"))
        req = GenerationRequest(text="ICE CRUSH", prompt="frozen", negative_prompt="watermark")
        patched = _patch_workflow(workflow, req)
        # 节点 4 为正向、9 为负向，两者文本必须不同
        pos = patched["4"]["inputs"]
        neg = patched["9"]["inputs"]
        assert _FLUX_BG_SUPPRESS in pos["t5xxl"]
        assert "watermark" in neg["t5xxl"]
        assert pos["t5xxl"] != neg["t5xxl"]

    def test_clip_l_is_short_digest_for_flux(self) -> None:
        """clip_l 只有 77 token，不能和 t5xxl 塞同一串长文本。"""
        workflow = _load_workflow(_resolve_workflow_path("flux_schnell"))
        req = GenerationRequest(text="咖啡 Latte 2.0", prompt="咖啡质感")
        patched = _patch_workflow(workflow, req)
        # 正向节点同时带 t5xxl（全长）与 clip_l（摘要）
        node = patched["4"]["inputs"]
        assert node["clip_l"] != node["t5xxl"]
        assert len(node["clip_l"]) < len(node["t5xxl"])

    def test_zimage_conditional_zero_out_untouched(self) -> None:
        """z-image 无负面文本节点：ConditioningZeroOut 不得被当作负面节点写入。"""
        workflow = _load_workflow(_resolve_workflow_path("test_z_image_turbo"))
        before = copy.deepcopy(workflow)
        req = GenerationRequest(text="满庭芳", prompt="水墨", negative_prompt="blurry")
        patched = _patch_workflow(workflow, req)
        # 57:33 是 ConditioningZeroOut，必须原样保留
        assert patched["57:33"] == before["57:33"]


# ── prompt_synthesis metadata ──


class TestPromptSynthesisMetadata:
    """metadata 里的 prompt_synthesis 是「实际用了哪个模型、注入了什么」的权威记录。"""

    @staticmethod
    def _run(monkeypatch: pytest.MonkeyPatch, request: GenerationRequest) -> tuple[dict, dict]:
        """跑一次成功路径，返回 (metadata, 提交给 ComfyUI 的工作流)。"""
        captured: dict = {}

        def fake_call(req: GenerationRequest, workflow: dict) -> GenerationArtifact:
            captured["workflow"] = copy.deepcopy(workflow)
            # 镜像 _call_comfyui_api 真实返回的 legacy 键，才能验证合并不覆盖
            return GenerationArtifact(
                image_base64="data:image/png;base64,AAAA",
                image_name="x.png",
                metadata={
                    "engine": "comfyui",
                    "prompt": req.prompt,
                    "negative_prompt": req.negative_prompt,
                    "style": req.style,
                },
            )

        monkeypatch.setattr(generator, "_call_comfyui_api", fake_call)
        artifact = generate_artwork(request)
        return artifact.metadata, captured["workflow"]

    def test_positive_prompt_matches_injected_node(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """最强一致性断言：metadata 的正向串必须与产物里注入的内容逐字相等。"""
        request = GenerationRequest(text="咖啡 Latte 2.0", prompt="咖啡质感")
        metadata, workflow = self._run(monkeypatch, request)

        # 从产物工作流里取出正向节点实际收到的文本
        resolved = _resolve_conditioning_nodes(workflow)
        inputs = workflow[resolved.positive_node_id]["inputs"]
        injected = inputs.get("t5xxl") or inputs.get("text")

        assert metadata["prompt_synthesis"]["positive_prompt"] == injected

    def test_legacy_keys_unchanged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """新增 prompt_synthesis 不得覆盖原有的 legacy 元数据键。"""
        # 这些 legacy 键是既有消费方的契约，必须保持不变
        request = GenerationRequest(
            text="满庭芳", prompt="水墨", negative_prompt="blurry", style="default"
        )
        metadata, _ = self._run(monkeypatch, request)

        assert metadata["prompt"] == "水墨"          # 用户原始风格串，不是合成串
        assert metadata["negative_prompt"] == "blurry"
        assert metadata["style"] == "default"
        assert metadata["fallback_tier"] == 0
        assert metadata["workflow_used"] == "qwen_image_2512_gguf"

    def test_text_profile_matches_routing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """元数据里的文本画像应与路由分类完全一致。"""
        # 三种脚本类型各跑一次，验证元数据与路由分类始终一致
        for text, expected in [("满庭芳", "chinese"), ("ICE CRUSH", "english"), ("咖啡 Latte 2.0", "mixed")]:
            metadata, _ = self._run(monkeypatch, GenerationRequest(text=text, prompt="风格"))
            assert metadata["prompt_synthesis"]["text_profile"] == expected
            assert metadata["prompt_synthesis"]["text_profile"] == _classify_text(text)

    def test_negative_strategy_matches_capability(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """负面策略应与能力探测结果一致：生效走 conditioning，否则走反演。"""
        # 两种路径各取一个真实工作流验证
        # 纯中文 -> qwen（cfg=2.5，负面真正生效）
        m_cn, _ = self._run(monkeypatch, GenerationRequest(text="满庭芳", prompt="水墨"))
        assert m_cn["prompt_synthesis"]["negative_strategy"] == "negative-conditioning"
        assert m_cn["prompt_synthesis"]["negative_capability"]["effective"] is True

        # 混排 -> z-image（无负面节点且 cfg=1）
        m_mixed, _ = self._run(monkeypatch, GenerationRequest(text="咖啡 Latte 2.0", prompt="咖啡"))
        assert m_mixed["prompt_synthesis"]["negative_strategy"] == "positive-inversion"
        assert m_mixed["prompt_synthesis"]["negative_capability"]["effective"] is False
        assert m_mixed["prompt_synthesis"]["negative_written_to"] is None

    def test_no_raw_text_quoted_in_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """守门：元数据记录的正向串里不得出现原文整串。"""
        # 该断言直接作用于最终产物元数据，是最贴近交付的检查
        text = "咪哄之风 98% hey you"
        metadata, _ = self._run(monkeypatch, GenerationRequest(text=text, prompt="酷炫"))
        assert f'"{text}"' not in metadata["prompt_synthesis"]["positive_prompt"]

    def test_clip_l_risk_only_reported_for_flux(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """clip_l 只有 flux 家族在用；其余家族报「截断风险」会误导。"""
        # 纯英文 -> flux，clip_l 真正被写入
        m_flux, _ = self._run(monkeypatch, GenerationRequest(text="ICE CRUSH", prompt="frozen"))
        assert m_flux["prompt_synthesis"]["clip_l_used"] is True
        assert isinstance(m_flux["prompt_synthesis"]["clip_l_truncation_risk"], bool)

        # 混排 -> z-image，单 text 输入，无 clip_l
        m_z, _ = self._run(monkeypatch, GenerationRequest(text="咖啡 Latte 2.0", prompt="咖啡"))
        assert m_z["prompt_synthesis"]["clip_l_used"] is False
        assert m_z["prompt_synthesis"]["clip_l_truncation_risk"] is None

    def test_stub_metadata_has_prompt_synthesis(self, sample_request: GenerationRequest) -> None:
        """stub 产物也要带 prompt_synthesis 块（applied=False）。"""
        artifact = _local_stub_generate(sample_request)
        # stub 不构造提示词，但块结构需与真实产物一致，仅 applied 为 False
        block = artifact.metadata["prompt_synthesis"]
        assert block["version"] == 1
        assert block["applied"] is False
        assert block["text_profile"] == "english"      # sample_request 的 text 为空

    def test_stub_block_carries_text_profile(self) -> None:
        """stub 的 prompt_synthesis 也需带上正确的文本画像。"""
        # 即便未走 ComfyUI，文本画像仍由 _analyze_text 得出
        artifact = _local_stub_generate(GenerationRequest(text="满庭芳", prompt="水墨"))
        assert artifact.metadata["prompt_synthesis"]["text_profile"] == "chinese"


# ── Prompt template: background suppression ──


class TestDefaultNegativePrompt:
    """校验默认负面词的构成：背景类与字形类缺一不可。"""

    def test_contains_background_terms(self) -> None:
        """默认负面词应包含各类背景相关项。"""
        # 这些背景类负面词用于压制模型生成复杂场景
        assert "complex background" in _DEFAULT_NEGATIVE
        assert "scenery background" in _DEFAULT_NEGATIVE
        assert "landscape background" in _DEFAULT_NEGATIVE
        assert "indoor scene" in _DEFAULT_NEGATIVE
        assert "outdoor scene" in _DEFAULT_NEGATIVE

    def test_retains_original_quality_terms(self) -> None:
        """默认负面词应保留原有的字形质量项。"""
        # 字形类负面词是艺术字场景的核心约束，不能被新增项挤掉
        assert "broken strokes" in _DEFAULT_NEGATIVE
        assert "missing strokes" in _DEFAULT_NEGATIVE
        assert "wrong characters" in _DEFAULT_NEGATIVE
        assert "garbled text" in _DEFAULT_NEGATIVE


class TestBuildFluxPrompt:
    """校验 Flux 正向提示词：背景抑制必发、段序正确、风格不被改写。"""

    def test_chinese_text_includes_bg_suppress(self) -> None:
        """中文输入也应包含背景抑制段。"""
        # 背景抑制对任何脚本类型的输入都应生效
        result = _build_flux_prompt("测试文字", "neon style")
        assert _FLUX_BG_SUPPRESS in result

    def test_english_text_includes_bg_suppress(self) -> None:
        """英文输入也应包含背景抑制段。"""
        # 背景抑制与脚本类型无关
        result = _build_flux_prompt("Hello World", "gold")
        assert _FLUX_BG_SUPPRESS in result

    def test_digits_only_includes_bg_suppress(self) -> None:
        """纯数字输入也应包含背景抑制段。"""
        # 纯数字归 english 链，模板同样是 flux
        result = _build_flux_prompt("123", "digital")
        assert _FLUX_BG_SUPPRESS in result

    def test_background_suppress_comes_before_style(self) -> None:
        """背景抑制段应排在风格段之前。"""
        # 段序约束：背景抑制必须先于用户风格出现
        result = _build_flux_prompt("测试", "neon style")
        assert result.index(_FLUX_BG_SUPPRESS) < result.index("neon style"), \
            "背景抑制应在风格提示词之前"

    def test_style_prompt_still_present(self) -> None:
        """风格描述必须原样保留在正向串中。"""
        # 模板只加约束，不得改写或丢弃用户风格
        result = _build_flux_prompt("测试", "neon style")
        assert "neon style" in result

    def test_empty_text_still_suppresses_background(self) -> None:
        """空文本不再短路：下游仍要抠图，背景抑制照发。"""
        result = _build_flux_prompt("", "minimal")
        assert _FLUX_BG_SUPPRESS in result
        assert result != "minimal"
        assert result.endswith("minimal")


class TestBuildZimagePrompt:
    """校验 Z-Image 正向提示词：背景抑制必发、段序正确、风格不被改写。"""

    def test_chinese_text_includes_bg_suppress(self) -> None:
        """中文输入也应包含中文背景抑制段。"""
        # 背景抑制对任何脚本类型的输入都应生效
        result = _build_zimage_prompt("测试文字", "科技风")
        assert _ZIMAGE_BG_SUPPRESS in result

    def test_english_text_includes_bg_suppress(self) -> None:
        """英文输入也应包含中文背景抑制段。"""
        # 落到 z-image 时无论输入语言，用的都是中文模板
        result = _build_zimage_prompt("Hello", "gold")
        assert _ZIMAGE_BG_SUPPRESS in result

    def test_background_suppress_comes_before_style(self) -> None:
        """背景抑制段应排在风格段之前。"""
        # 段序约束：背景抑制必须先于用户风格出现
        result = _build_zimage_prompt("测试", "科技风")
        assert result.index(_ZIMAGE_BG_SUPPRESS) < result.index("科技风"), \
            "背景抑制应在风格提示词之前"

    def test_style_prompt_still_present(self) -> None:
        """风格描述必须原样保留在正向串中。"""
        # 模板只加约束，不得改写或丢弃用户风格
        result = _build_zimage_prompt("测试", "科技风")
        assert "科技风" in result

    def test_empty_text_still_suppresses_background(self) -> None:
        """空文本不再短路：下游仍要抠图，背景抑制照发。"""
        result = _build_zimage_prompt("", "minimal")
        assert _ZIMAGE_BG_SUPPRESS in result
        assert result != "minimal"
        assert result.endswith("minimal")


class TestBuildNegativePrompt:
    """校验负面提示词的合并逻辑：默认词在前、用户词追加在末尾。"""

    def test_returns_default_when_empty(self) -> None:
        """用户未提供负面词时直接返回默认负面词。"""
        # 空输入时直接返回默认负面词本身
        result = _build_negative_prompt("")
        assert result == _DEFAULT_NEGATIVE

    def test_merges_user_negative(self) -> None:
        """用户负面词应追加在默认负面词之后。"""
        # 默认负面词在前、用户词在末尾，两者都在
        result = _build_negative_prompt("extra term")
        assert _DEFAULT_NEGATIVE in result
        assert "extra term" in result
        assert result.endswith("extra term")

    def test_trims_user_input(self) -> None:
        """用户负面词首尾空白应被去除。"""
        # 用户输入首尾空白应被去除后再拼接
        result = _build_negative_prompt("  padded  ")
        assert "padded" in result


# ── Module-level constants ──


class TestBackgroundSuppressConstants:
    """校验背景抑制常量本身是非空字符串（模板拼装的先决条件）。"""

    def test_flux_bg_suppress_is_string(self) -> None:
        """Flux 背景抑制常量应为非空字符串。"""
        # 常量须为非空字符串，否则模板会产出空洞子句
        assert isinstance(_FLUX_BG_SUPPRESS, str)
        assert len(_FLUX_BG_SUPPRESS) > 0

    def test_zimage_bg_suppress_is_string(self) -> None:
        """Z-Image 背景抑制常量应为非空字符串。"""
        # 常量须为非空字符串，否则模板会产出空洞子句
        assert isinstance(_ZIMAGE_BG_SUPPRESS, str)
        assert len(_ZIMAGE_BG_SUPPRESS) > 0
