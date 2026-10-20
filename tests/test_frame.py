#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 5（维度框架配置化）的契约测试（标准库 unittest，无第三方依赖）。

把两个「跑完即删」的探针固化下来，并补上框架抽象本身的断言：

1. ``TestMiniYaml``        —— 零依赖 YAML 子集解析器：能解出 frames/*.yaml 的全部字段
2. ``TestConsumerContract`` —— **不换框架时，模型与读者看到的每个字都不变**
   （文案逐字节比对改造前的硬编码；提示词逐字节比对 ``tests/fixtures/prompts_golden``）
3. ``TestFrameMetadata``   —— 框架元数据的上/下游同构：``to_metadata`` → ``frame_from_product``
   必须原样读回来（产物自描述的前提）
4. ``TestFrameSwitch``     —— 换框架该变什么、不该变什么：轴/桶/键跟着变；闸门逻辑不变
5. ``TestArtifactInvariants`` —— 有 ``out/verify/stage5/*`` 产物时，跑换框架不变量与看板结构指纹
   （没有产物就 skip，不让单元测试依赖跑过全链路）

运行::

    python -m unittest tests.test_frame -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from voc_core import miniyaml  # noqa: E402
from voc_core.analysis_frame import (  # noqa: E402
    FRAME_DIR,
    available_frames,
    frame_from_product,
    frame_path,
    load_frame,
)

#: 渲染文案的上下文 —— 与真实调用点（报告 / 看板的 ``axis_ctx``）同形。
CTX = dict(
    scope="帖子单元 + 评论单元合并计数",
    unit="帖子", secondary="评论", secondary_speaker="评论者",
    unit_pair="帖子 / 评论", merged="帖子 + 评论", unit_char="帖",
    n=7287, share="（占全部单元的 93.2%）", note="",
)


class TestMiniYaml(unittest.TestCase):
    """零依赖 YAML 子集解析器。"""

    def test_scalars_lists_dicts(self) -> None:
        d = miniyaml.loads(
            "name: x\n"
            "n: 3\n"
            "flag: true\n"
            "empty:\n"
            "items:\n"
            "  - a\n"
            "  - b\n"
            "nested:\n"
            "  k: v\n"
        )
        self.assertEqual(d["name"], "x")
        self.assertEqual(d["n"], 3)
        self.assertIs(d["flag"], True)
        self.assertIn(d["empty"], (None, ""))
        self.assertEqual(d["items"], ["a", "b"])
        self.assertEqual(d["nested"]["k"], "v")

    def test_inline_list_and_quotes(self) -> None:
        d = miniyaml.loads('tags: [a, b, c]\nnote: "含 : 冒号的引号串"\n')
        self.assertEqual(d["tags"], ["a", "b", "c"])
        self.assertEqual(d["note"], "含 : 冒号的引号串")

    def test_block_scalar_and_comments(self) -> None:
        d = miniyaml.loads(
            "# 整行注释\n"
            "key: value  # 行尾注释\n"
            "text: |\n"
            "  第一行\n"
            "  第二行\n"
        )
        self.assertEqual(d["key"], "value")
        self.assertIn("第一行", d["text"])
        self.assertIn("第二行", d["text"])

    def test_frames_are_parseable(self) -> None:
        """两个内置框架都必须能被自家解析器读出来（这是硬约束：零依赖）。"""
        self.assertTrue(FRAME_DIR.is_dir(), FRAME_DIR)
        for name in ("consumer", "b2b_role"):
            p = frame_path(name)
            self.assertIsNotNone(p, name)
            raw = miniyaml.load(str(p))
            self.assertIn("dimensions", raw, name)
            self.assertIn("segmentation_axis", raw, name)


class TestConsumerContract(unittest.TestCase):
    """consumer = 改造前行为。**一个字节都不许变**。"""

    #: 改造前写死在 report/dashboard 里的句子（Stage 4 的产物逐字抄下来当基准）
    EXPECT_TEXTS = {
        "scope_rule": "人群画像 = 帖子单元 + 评论单元合并计数；每个单元先由 purchase_status 判为已购 / 未购 / 无法判断，只把「已购」计入购买者人群画像、「未购」计入未购买者人群画像，两桶严格互斥。",
        "excluded_note": "购买状态判为「无法判断」的单元（纯附和短评、泛泛围观、第三人称转述等）不计入任何人群画像桶，已从画像统计中排除；此处披露其数量，以免读者误以为画像覆盖了全部语料。",
        "report_data_limit": "购买状态判为「无法判断」的单元（纯附和短评、泛泛围观、第三人称转述等）已从人群画像中排除、不在两桶之内——其数量在「二、人群画像」节披露，读画像结论时应知道这部分语料未被覆盖。",
        "report_excluded_line": "**已排除单元**：另有 **7287** 个单元（占全部单元的 93.2%）无法判定购买状态（纯附和短评、泛泛围观、第三人称转述等），未计入下表任何画像桶。",
        "report_persona_intro": "本节不再区分「发帖人 / 评论者」，而是**按购买状态**分两个画像——购买者 / 未购买者，且**帖子单元与评论单元合并计数**——因为评论者往往本身就是该品类的使用者或潜在买家，拆成两套会人为割裂同一批人；购买状态无法判定的单元不计入任何画像桶（数量见下方披露）。",
        "report_persona_intro_solo": "本节**按购买状态**分两个画像——购买者 / 未购买者；购买状态无法判定的单元不计入任何画像桶（数量见下方披露）。",
        "report_distribution_title": "购买状态分布（归桶依据）",
        "report_distribution_col": "购买状态",
        "report_matrix_title": "**已购人群画像 × 功能体验矩阵**（单元格 = 帖粒度交集计数 / 满意度；画像轴 = **仅已购人群**画像标签——未购人群与购买状态不明人群均不入矩阵；评论单元无功能标注不进矩阵）：",
        "matrix_note": "；评论单元无功能标注不进矩阵",
        "matrix_note_solo": "",
    }

    EXPECT_DASH = {
        "framework_line": "分析框架：购买状态分布 &rarr; 购买者人群画像 &rarr; 未购买者人群画像 &rarr; 使用场景 &rarr; 购买前关注点 &rarr; 购买后体验 &rarr; 品牌分布 &rarr; 未满足需求 | 点击图表分类可查看支撑证据；人群画像为「帖子 + 评论」合并计数",
        "axis_chart_title": "a. 购买状态分布（购买意向漏斗）",
        "axis_chart_subtitle": "每个发言单元（帖子 / 评论）先判定「已购买 / 未购买 / 无法判断」：已购计入购买者桶、未购计入未购买者桶，无法判断的单元不计入任何画像桶",
        "matrix_title": "已购人群画像 × 功能 交叉分析热力图",
        "matrix_subtitle": "纵轴：TOP 8 <b>已购</b>画像标签（不含未购 / 状态不明人群） | 横轴：TOP 10 功能 | 单元格：帖子粒度交集计数 / 满意度 | 仅已购帖子入矩阵；评论单元无功能标注，不进矩阵",
        "matrix_note": " | 仅已购帖子入矩阵；评论单元无功能标注，不进矩阵",
        "matrix_note_solo": " | 仅已购帖子入矩阵",
    }

    def test_default_frame_is_consumer(self) -> None:
        f = load_frame(None)
        self.assertEqual(f.name, "consumer")
        self.assertEqual(f.axis.dim, "purchase_status")
        self.assertEqual(f.axis.distribution_key, "purchase_status_distribution")
        self.assertEqual(f.axis.unit_counts_key, "purchase_status")
        self.assertEqual(f.axis.matrix_bucket, "purchased")
        self.assertEqual(f.axis.unknown_bucket, "unknown")
        self.assertEqual([b.id for b in f.axis.buckets],
                         ["purchased", "non_purchased", "unknown"])
        self.assertEqual(f.persona_dims(),
                         {"user_personas_purchased": "purchased",
                          "user_personas_non_purchased": "non_purchased"})

    def test_report_texts_byte_exact(self) -> None:
        f = load_frame(None)
        got = f.axis.render_texts(**CTX)
        # ``report_matrix_title`` 尾部要拼 ``matrix_note`` —— 真实调用点是
        # 「先渲染一遍拿到 note，再带 note 渲染第二遍」。这里照做。
        got = f.axis.render_texts(**{**CTX, "note": got["matrix_note"]})
        for k, want in self.EXPECT_TEXTS.items():
            self.assertIn(k, got, f"框架缺少文案 {k}")
            self.assertEqual(got[k], want, f"texts.{k} 与改造前不一致")

    def test_dashboard_texts_byte_exact(self) -> None:
        f = load_frame(None)
        got = f.axis.render_dashboard(**CTX)
        for k, want in self.EXPECT_DASH.items():
            self.assertIn(k, got, f"框架缺少看板文案 {k}")
            self.assertEqual(got[k], want, f"dashboard.{k} 与改造前不一致")

    def test_bucket_level_texts(self) -> None:
        f = load_frame(None)
        by_id = {b.id: b for b in f.axis.buckets}
        self.assertEqual(by_id["purchased"].dashboard["card_title"], "b. 购买者人群画像")
        self.assertEqual(by_id["non_purchased"].dashboard["card_title"], "c. 未购买者人群画像")
        self.assertEqual(by_id["purchased"].dashboard["core_finding"], "核心购买人群")
        self.assertEqual(by_id["non_purchased"].dashboard["core_finding"], "核心未购人群")
        # 排除桶不出卡：没有 card_title，也不在活跃桶里
        self.assertFalse(by_id["unknown"].dashboard.get("card_title"))
        self.assertTrue(by_id["unknown"].excluded)
        self.assertNotIn("unknown", [b.id for b in f.axis.active_buckets])

    def test_text_groups_are_separate_namespaces(self) -> None:
        """``texts`` 与 ``dashboard`` 是**两组**文案，``render_text`` 只认前者。

        这不是挑剔实现，是防回归：两组里同名的 ``matrix_note`` 刻意取不同的值
        （报告版短、看板版带「仅 X 帖入矩阵」），混用会静默串台。
        看板的 ``matrix_subtitle`` 尾部要拼 ``matrix_note``，所以只能整组渲染。
        """
        f = load_frame(None)
        # render_text 只查 texts：拿 dashboard 组的键会得到空串，不会串台
        self.assertEqual(f.axis.render_text("matrix_subtitle", **CTX), "")
        self.assertEqual(f.axis.render_text("matrix_title", **CTX), "")
        self.assertNotEqual(f.axis.texts.get("matrix_note"), f.axis.dashboard.get("matrix_note"))
        # 整组渲染时把拼好的 note 传进去，尾注才接得上
        note = f.axis.render_text("matrix_note", **{**CTX, "note": ""})
        dash = f.axis.render_dashboard(matrix_note=note, **CTX)
        self.assertTrue(dash["matrix_subtitle"].endswith(note), dash["matrix_subtitle"][-60:])


class TestPromptsUnchanged(unittest.TestCase):
    """提示词 golden：不换框架时，模型收到的每一个字都不变。"""

    GOLD = ROOT / "tests" / "fixtures" / "prompts_golden" / "rendered"

    def test_prompt_golden_byte_exact(self) -> None:
        if not self.GOLD.is_dir():
            self.skipTest("没有 prompts_golden fixture")
        import importlib

        kdir = str(ROOT / "voc_core" / "kernel")
        if kdir not in sys.path:
            sys.path.insert(0, kdir)
        dt = importlib.import_module("design_tags")
        ap = importlib.import_module("annotate_posts")

        frame = load_frame(None)
        category = "Nursery Glider Recliner"
        samples = "[1] 标题: Best glider ever\n正文: I bought this glider and it is comfortable"
        comment_block = (
            "\n\n## 各帖的评论片段（共 2 条，覆盖 1 个帖子）\n"
            "**注意：以下内容全部是「评论片段，非发帖人自述」**—（fixture 截断）\n\n"
            "- c1: I love the swivel\n- c2: mine squeaks"
        )
        posts = [
            {"reddit_id": "p1", "title": "Best glider ever",
             "summary": "I bought this glider and it is comfortable",
             "mentioned_brands": "Babyletto", "_seq": 0},
            {"reddit_id": "p2", "title": "Looking for a recliner",
             "summary": "Any recommendations for a nursery chair?",
             "mentioned_brands": "", "_seq": 1},
        ]
        flat = {
            "nursery_glider": {
                "dim": "features",
                "def": {
                    "category": "feature",
                    "thoughts": "用户提到摇篮椅的摇摆功能",
                    "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                    "original_text": "抽取打标对应的原文片段",
                    "nursery_glider_summary": "具体功能点",
                    "nursery_glider_reason": ["省力", "哄睡快"],
                    "sentiment": "positive / neutral / negative",
                },
            },
            "budget_shopper": {
                "dim": "user_personas_non_purchased",
                "def": {
                    "category": "user",
                    "thoughts": "仅当发言者尚未购买时才标 yes",
                    "mentioned": "Output 'yes' or 'no'. If 'no', leave all other fields blank.",
                    "original_text": "抽取打标对应的原文片段",
                    "budget_shopper_summary": "预算区间与比较维度",
                    "sentiment": "n/a",
                },
            },
        }

        system, user = dt.build_design_prompts(
            frame, category=category, nsample=len(posts), samples=samples,
            comment_block=comment_block, unit="帖子", secondary="评论",
            secondary_speaker="评论者", lang="zh")
        cases = {
            "design_tags.system.zh.txt": system,
            "design_tags.user.zh.txt": user,
            "annotate.system.zh.txt": ap.build_annotate_system(
                frame, unit="帖子", secondary="评论",
                secondary_speaker="评论者", lang="zh"),
            "annotate.user.zh.txt": ap.build_user_prompt(posts[0], flat),
            "annotate.batch.user.zh.txt": ap.build_batch_user_prompt(posts, flat),
        }
        for name, got in cases.items():
            want = (self.GOLD / name).read_text(encoding="utf-8")
            self.assertEqual(got, want, f"{name} 与冻结的渲染稿不一致")


class TestFrameMetadata(unittest.TestCase):
    """产物自描述：``to_metadata`` 写出去的块必须能被 ``frame_from_product`` 原样读回来。"""

    def test_roundtrip_identity(self) -> None:
        for name in available_frames():
            f = load_frame(name)
            back = frame_from_product({"analysis_frame": f.to_metadata()})
            self.assertIsNotNone(back, name)
            self.assertEqual(back.to_metadata(), f.to_metadata(), f"{name} 元数据往返不等")
            self.assertEqual([b.id for b in back.axis.buckets], [b.id for b in f.axis.buckets])
            self.assertEqual(back.axis.distribution_key, f.axis.distribution_key)
            self.assertEqual(back.persona_dims(), f.persona_dims())

    def test_absent_metadata_falls_back_to_consumer(self) -> None:
        """老产物（没有那个块）→ 内置 consumer，于是老产物照样能过闸门。"""
        self.assertIsNone(frame_from_product({}))
        self.assertIsNone(frame_from_product({"tagset": {}}))
        self.assertEqual(load_frame(None).name, "consumer")

    def test_metadata_is_json_serialisable(self) -> None:
        for name in available_frames():
            blob = json.dumps(load_frame(name).to_metadata(), ensure_ascii=False)
            self.assertGreater(len(blob), 100, name)


class TestFrameSwitch(unittest.TestCase):
    """换框架：轴该变的变，闸门逻辑不该动。"""

    def test_every_frame_renders_design_and_annotate_prompts(self) -> None:
        """**每个**内置框架都必须能渲染出全套 design/annotate 提示词。

        b2b_role 曾整个漏掉顶层 ``prompt:`` 段——下游渲染、元数据往返、闸门不变量
        全部照常通过（错得很安静），直到 Web 上真选 b2b 框架跑标签设计才
        ``KeyError: 'purpose_line'`` 退出码 1。这条测试把「每个框架都至少把
        提示词构建跑一遍」钉成契约，新框架落地时跑不到就当场红。
        """
        from voc_core.frame_prompts import annotate_rules, prompt_parts
        from voc_core.kernel import annotate_posts as ap
        from voc_core.kernel import design_tags as dt

        for name in available_frames():
            with self.subTest(frame=name):
                f = load_frame(name)
                parts = prompt_parts(f, category="测试品类", unit="帖子",
                                     secondary="评论", secondary_speaker="评论者")
                for key in ("purpose_line", "dim_block", "axis_note", "summary_line"):
                    self.assertTrue(parts.get(key), f"{name} 缺提示词片段 {key}")
                for key in ("experience_note", "category_note",
                            "persona_requirements", "json_shape"):
                    self.assertIn(key, parts, f"{name} 缺 system 片段 {key}")
                system, user = dt.build_design_prompts(
                    f, category="测试品类", nsample=1, samples="- r1 | t | s",
                    comment_block="", unit="帖子", secondary="评论",
                    secondary_speaker="评论者", lang="zh")
                self.assertGreater(len(system), 500, f"{name} system 提示词异常地短")
                self.assertGreater(len(user), 200, f"{name} user 提示词异常地短")
                self.assertIn("维度框架", user, f"{name} user 提示词缺维度框架块")
                rules = annotate_rules(f)
                self.assertGreater(len(rules["annotate_axis_rule"]), 50,
                                   f"{name} 缺 annotate_axis_rule")
                sys_txt = ap.build_annotate_system(
                    f, unit="帖子", secondary="评论",
                    secondary_speaker="评论者", lang="zh")
                self.assertGreater(len(sys_txt), 500, f"{name} annotate system 异常地短")

    def test_b2b_has_no_purchase_status_axis(self) -> None:
        f = load_frame("b2b_role")
        self.assertEqual(f.axis.dim, "decision_role")
        self.assertEqual(f.axis.distribution_key, "decision_role_distribution")
        self.assertEqual(f.axis.unit_counts_key, "decision_role")
        self.assertNotIn("purchase_status", f.dim_ids())
        self.assertNotIn("purchase_status_distribution", f.dim_ids())
        for b in f.axis.buckets:
            self.assertNotIn("purchase", b.id)

    def test_active_vs_excluded_buckets(self) -> None:
        for name, expected_active in (("consumer", ["purchased", "non_purchased"]),
                                      ("b2b_role", ["decision_maker", "influencer", "end_user"])):
            f = load_frame(name)
            self.assertEqual([b.id for b in f.axis.active_buckets], expected_active, name)
            excluded = [b.id for b in f.axis.buckets if b.excluded]
            self.assertEqual(excluded, [f.axis.unknown_bucket], name)
            self.assertEqual(f.axis.excluded_bucket.id, f.axis.unknown_bucket, name)
            # 排除桶不得混进活跃桶
            self.assertNotIn(f.axis.unknown_bucket, expected_active, name)
            # 进矩阵的桶必须是活跃桶
            self.assertIn(f.axis.matrix_bucket, expected_active, name)

    def test_scope_rule_switches_vocabulary(self) -> None:
        """B2B 的口径说明里不许出现「已购 / 未购」（这正是换框架的意义）。

        ``scope_rule`` 里的轴名用 ``${axis_dim}``（维度 id，如 ``decision_role``），
        与改造前 consumer 串里的 ``purchase_status`` 同一写法 —— 保持一致。
        """
        f = load_frame("b2b_role")
        s = f.axis.render_text("scope_rule", scope="帖子单元 + 评论单元合并计数")
        self.assertIn("decision_role", s)
        self.assertIn("角色不明", s)
        for w in ("已购", "未购", "购买状态"):
            self.assertNotIn(w, s)
        self.assertNotIn("${", s)

    def test_bucket_labels_are_frame_specific(self) -> None:
        f = load_frame("b2b_role")
        by_id = {b.id: b for b in f.axis.buckets}
        self.assertEqual(by_id["decision_maker"].label_cn, "决策者")
        self.assertEqual(by_id["decision_maker"].dashboard["card_title"], "b. 决策者人群画像")
        self.assertEqual(by_id["unclear"].excluded, True)
        # 三张卡的副标题都渲染得出来（${merged} 由调用方给）
        for b in f.axis.active_buckets:
            got = f.axis.render(b.dashboard["card_subtitle"], **CTX)
            self.assertNotIn("${", got)
            self.assertIn("计数单元", got)

    def test_gate_logic_is_frame_independent(self) -> None:
        """归桶闸门：恰好一个标签命中才生效；0 个或 ≥2 个都判 unknown_bucket。

        这段逻辑**不随框架变** —— 变的只是「标签 → 桶」的映射表。
        """
        from voc_core.kernel.aggregate_annotations import axis_bucket_of

        pairs = [("t_dm", "decision_maker"), ("t_infl", "influencer"), ("t_eu", "end_user")]

        def rec(**anns):
            return {"annotations": {k: {"mentioned": v} for k, v in anns.items()}}

        self.assertEqual(axis_bucket_of(rec(t_dm="yes"), pairs, "unclear"), "decision_maker")
        self.assertEqual(axis_bucket_of(rec(t_infl="yes"), pairs, "unclear"), "influencer")
        # 命中 0 个
        self.assertEqual(axis_bucket_of(rec(t_dm="no"), pairs, "unclear"), "unclear")
        self.assertEqual(axis_bucket_of(rec(), pairs, "unclear"), "unclear")
        # 命中 ≥2 个：宁可不计入，也不要把人放错桶
        self.assertEqual(axis_bucket_of(rec(t_dm="yes", t_eu="yes"), pairs, "unclear"), "unclear")
        # 大小写不敏感
        self.assertEqual(axis_bucket_of(rec(t_dm="YES"), pairs, "unclear"), "decision_maker")

    def test_bucket_map_comes_from_frame_declaration(self) -> None:
        """``{标签键: 桶 id}`` 由框架**显式声明**（``buckets[].tags``），不靠按键名猜。

        猜错的后果是把人放进错的桶，而份额数字错了**不会有任何报错**。
        """
        from voc_core.kernel.aggregate_annotations import bucket_map, use_frame

        expected = {
            "consumer": {"ps_purchased": "purchased", "ps_non_purchased": "non_purchased",
                         "ps_unknown": "unknown"},
            "b2b_role": {"dr_decision_maker": "decision_maker", "dr_influencer": "influencer",
                         "dr_end_user": "end_user", "dr_unclear": "unclear"},
        }
        try:
            for name, want in expected.items():
                use_frame(load_frame(name))
                self.assertEqual(bucket_map(), want, name)
        finally:
            use_frame(None)  # 复原，别把全局状态漏给别的用例


class TestArtifactInvariants(unittest.TestCase):
    """有真实产物时跑不变量断言；没有就 skip（单元测试不依赖跑过全链路）。"""

    STAGE5 = ROOT / "out" / "verify" / "stage5"

    def _load_tool(self, mod: str):
        tdir = str(ROOT / "tools")
        if tdir not in sys.path:
            sys.path.insert(0, tdir)
        import importlib
        return importlib.import_module(mod)

    def test_invariants_on_produced_artifacts(self) -> None:
        base = self.STAGE5
        if not base.is_dir():
            self.skipTest("没有 out/verify/stage5 产物")
        try:
            S = self._load_tool("stage5_baseline")
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"载入基线工具失败：{e}")

        seen = 0
        for tag in ("after_30", "b2b"):
            d = base / tag
            if not (d / "dimensions.json").exists():
                continue
            inv = S.probe_invariants(d)
            self.assertTrue(inv.get("applicable"), f"{tag} 产物缺少框架元数据")
            for c in inv["checks"]:
                self.assertTrue(c["ok"], f"{tag} 不变量失败：{c['name']} —— {c['detail']}")
            # 画像卡片数 == 活跃桶数（「按桶生成」的可机器验证形式）
            probe = S.probe_dashboard(d)
            self.assertEqual(probe["persona_card_count"], len(inv["active_buckets"]),
                             f"{tag} 画像卡片数与活跃桶数不符")
            self.assertFalse(probe["embedded_has_analysis_frame"],
                             f"{tag} 看板不该内嵌 analysis_frame 元数据")
            seen += 1
        if not seen:
            self.skipTest("没有可用的 Stage 5 产物")

    def test_frame_switch_probe_on_b2b(self) -> None:
        d = self.STAGE5 / "b2b"
        if not (d / "dimensions.json").exists():
            self.skipTest("没有 b2b 产物")
        try:
            P = self._load_tool("_probe_frame_switch")
        except Exception as e:  # noqa: BLE001
            self.skipTest(f"载入探针失败：{e}")
        dim = json.loads((d / "dimensions.json").read_text(encoding="utf-8"))
        checks = [P.check_axis_switched(dim),
                  P.check_no_old_axis_words(d, "购买状态"),
                  P.check_empty_modules_hidden(d),
                  P.check_no_errors(d)]
        for c in checks:
            self.assertTrue(c["ok"], f"{c['name']} —— {c['detail']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
