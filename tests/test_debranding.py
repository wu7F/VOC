#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 3（内核去域名化）的守门测试。

这里守的不是「代码能跑」，而是**两条口径**：

1. **Reddit 行为逐字不变** —— ``SourceProfile`` 的 REDDIT 画像每个字段都必须
   等于改造前的硬编码文案；``canonicalize_rows`` 对旧 schema 必须是恒等变换。
   一旦有人不小心改了这些，用户手里已有的报告会悄悄变样。
2. **换源真的换对了** —— 电商源不得出现「子版块」「帖子」「评论的评论」这类
   Reddit 专属概念。特别是 ``opt()`` 那条：空串是**有意义的值**（表示该源没有
   这个概念），绝不能被 ``term()`` 的回落机制填成「评论」——
   这个坑真实发生过（wayfair 报告里凭空多出一层「评论单元」）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from voc_core.kernel_common import (  # noqa: E402
    canonicalize_row,
    canonicalize_rows,
    guess_role,
    load_profile,
    row_get,
    strip_bom_keys,
)
from voc_core.naming import (  # noqa: E402
    LEGACY_NAMES,
    NEUTRAL_NAMES,
    artifact_name,
    resolve_artifact,
)
from voc_core.source_profile import AMAZON, GENERIC, REDDIT, WAYFAIR, get_profile  # noqa: E402

VOC = ROOT / "voc.py"

KERNEL = ROOT / "voc_core" / "kernel"
DATA = ROOT.parent / "data" / "reddit_analysis"
SKILL_SCRIPTS = Path.home() / ".workbuddy" / "skills" / "reddit分析" / "scripts"
PY = sys.executable


def _run_kernel(script: str, argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    return subprocess.run([PY, str(KERNEL / script), *argv],
                          capture_output=True, text=True, encoding="utf-8", env=env)


def _run_cli(argv: list[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
    return subprocess.run([PY, str(VOC), *argv],
                          capture_output=True, text=True, encoding="utf-8", env=env)


#: ``<script>`` / ``<style>`` 整块（含 JS 注释与内嵌数据）—— 都不是用户可见文案
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.S | re.I)


def visible_html(html: str) -> str:
    """剥掉脚本与样式，只留用户真正看得到的标记。

    内嵌的 ``EMBEDDED.allPosts`` 数据里当然有 ``subreddit`` 这样的**键名**，
    JS 注释里也可能写「子版块」；这些都不是呈现层的域名泄漏，不能拿来判红。
    """
    return _SCRIPT_OR_STYLE.sub("", html)


# --------------------------------------------------------------------------
# 1. 源画像：术语表语义
# --------------------------------------------------------------------------

class TestSourceProfile(unittest.TestCase):

    def test_reddit_profile_is_the_compat_baseline(self):
        """REDDIT 画像 = 改造前的硬编码文案，逐字校验。"""
        self.assertEqual(REDDIT.channel_noun, "子版块")
        self.assertEqual(REDDIT.channel_section, "子版块分布")
        self.assertEqual(REDDIT.unit_noun, "帖子")
        self.assertEqual(REDDIT.unit_char, "帖")
        self.assertEqual(REDDIT.secondary_noun, "评论")
        self.assertEqual(REDDIT.secondary_speaker, "评论者")
        self.assertEqual(REDDIT.dataset_label, "Reddit RSS")
        self.assertEqual(REDDIT.generator_label, "Reddit 分析技能自动生成")
        self.assertEqual(REDDIT.channel_prefix, "r/")

    def test_unknown_source_does_not_raise(self):
        """未知源要能跑出东西（宁可文案不完美，不可中断分析）。"""
        p = get_profile("some-brand-new-source")
        self.assertEqual(p.source, "some-brand-new-source")
        # 未知源继承 Reddit 词汇（term 回落），但标识是自己的
        self.assertEqual(p.term("unit_noun"), "帖子")

    def test_empty_spec_is_reddit(self):
        self.assertIs(get_profile(None), REDDIT)
        self.assertIs(get_profile(""), REDDIT)

    def test_term_falls_back_but_opt_does_not(self):
        """核心语义：term() 回落、opt() 不回落。"""
        # secondary_noun 在电商源里是空串 —— term() 会错误地回落成「评论」
        self.assertEqual(WAYFAIR.term("secondary_noun"), "评论")   # 回落（危险用法）
        self.assertEqual(WAYFAIR.opt("secondary_noun"), "")        # 正确读取
        # promo_channel_note / channel_prefix 同理
        self.assertEqual(WAYFAIR.opt("channel_prefix"), "")
        self.assertEqual(WAYFAIR.opt("promo_channel_note"), "")

    def test_ecommerce_profiles_have_no_second_layer(self):
        for p in (AMAZON, WAYFAIR):
            self.assertEqual(p.opt("secondary_noun"), "",
                             f"{p.source} 是单层评论源，不该有次级单元")
            self.assertEqual(p.term("unit_noun"), "评论")
            self.assertEqual(p.term("channel_noun"), "站点")
            self.assertTrue(p.has_rating)
            self.assertFalse(p.has_threads)

    def test_optional_terms_registry_is_complete(self):
        """OPTIONAL_TERMS 必须覆盖所有「空值有意义」的字段，否则将来必踩坑。"""
        for key in ("secondary_noun", "promo_channel_note", "channel_prefix"):
            self.assertIn(key, REDDIT.OPTIONAL_TERMS)

    def test_roundtrip_json(self):
        with tempfile.TemporaryDirectory() as td:
            p = WAYFAIR.to_json(Path(td) / "p.json")
            back = get_profile(str(p))
            self.assertEqual(back.source, "wayfair")
            self.assertEqual(back.opt("secondary_noun"), "")
            self.assertTrue(back.has_rating)

    def test_process_scope_rule_differs_by_source(self):
        """人群画像口径说明要随源变化（单层源不能声称有「评论单元」）。"""
        from voc_core.kernel.aggregate_annotations import use_profile, persona_scope_rule

        use_profile(REDDIT)
        r = persona_scope_rule()
        self.assertIn("帖子单元 + 评论单元合并计数", r)

        use_profile(WAYFAIR)
        w = persona_scope_rule()
        self.assertIn("以评论单元计数", w)
        self.assertNotIn("评论单元 + ", w)
        self.assertNotIn("帖子", w)

        use_profile(None)  # 复原，避免影响其它用例


# --------------------------------------------------------------------------
# 2. 列名归一化：新契约 → 内核认识的旧列名
# --------------------------------------------------------------------------

class TestColumnCanonicalization(unittest.TestCase):

    def test_new_contract_columns_fill_legacy_keys(self):
        row = {"unit_id": "u1", "title": "T", "text": "body", "channel": "Wayfair",
               "author": "a", "created_at": "2026-01-01T00:00:00Z", "url": "http://x"}
        out = canonicalize_row(row, "posts")
        self.assertEqual(out["reddit_id"], "u1")
        self.assertEqual(out["summary_text"], "body")
        self.assertEqual(out["subreddit"], "Wayfair")
        self.assertEqual(out["published"], "2026-01-01T00:00:00Z")
        # 原键保留
        self.assertEqual(out["unit_id"], "u1")
        self.assertEqual(out["text"], "body")

    def test_legacy_schema_is_identity_transform(self):
        """关键：已经是旧 schema 的输入必须原样透传（Reddit 无回归的前提）。"""
        row = {"reddit_id": "r1", "summary_text": "x", "subreddit": "s", "title": "t"}
        self.assertEqual(canonicalize_row(row, "posts"), row)

    def test_xlsx_style_header_variants(self):
        """第三方导出的列名常带空格 / 驼峰（Review Title / review-title）。"""
        row = {"Review ID": "r1", "Review Text": "body", "Rating": "5", "ASIN": "B01"}
        out = canonicalize_row(row, "reviews")
        self.assertEqual(out["review_id"], "r1")
        self.assertEqual(out["content"], "body")
        self.assertEqual(out["asin"], "B01")

    def test_role_scoping_avoids_cross_contamination(self):
        """帖子文件不该被补上 review_id 这类电商键。"""
        row = {"unit_id": "u1", "text": "x", "channel": "c"}
        posts = canonicalize_row(row, "posts")
        self.assertNotIn("review_id", posts)
        reviews = canonicalize_row(row, "reviews")
        self.assertIn("review_id", reviews)

    def test_comments_role_maps_thread(self):
        row = {"unit_id": "c1", "thread_id": "p1", "text": "hi", "channel": "Wayfair"}
        out = canonicalize_row(row, "comments")
        self.assertEqual(out["comment_id"], "c1")
        self.assertEqual(out["post_reddit_id"], "p1")
        self.assertEqual(out["body"], "hi")

    def test_existing_value_wins_over_alias(self):
        """目标键已有非空值时不被别名覆盖（overwrite=False 的默认语义）。"""
        row = {"reddit_id": "keep", "unit_id": "other"}
        self.assertEqual(canonicalize_row(row, "posts")["reddit_id"], "keep")
        self.assertEqual(canonicalize_row(row, "posts", overwrite=True)["reddit_id"], "other")

    def test_guess_role(self):
        self.assertEqual(guess_role(["reddit_id", "subreddit"]), "posts")
        self.assertEqual(guess_role(["comment_id", "post_reddit_id"]), "comments")
        self.assertEqual(guess_role(["review_id", "Rating", "Review Text"]), "reviews")

    def test_row_get_reads_by_canonical_name(self):
        self.assertEqual(row_get({"unit_id": "u9"}, "unit_id"), "u9")
        self.assertEqual(row_get({"channel": "站点A"}, "channel"), "站点A")
        self.assertEqual(row_get({}, "unit_id", "?"), "?")

    def test_batch(self):
        rows = [{"unit_id": str(i), "text": "t"} for i in range(3)]
        out = canonicalize_rows(rows, "posts")
        self.assertEqual([r["reddit_id"] for r in out], ["0", "1", "2"])

    def test_bom_prefixed_first_column_is_renamed_not_duplicated(self):
        """Excel 导出的 BOM 会黏在首个列名上；补键不得在末尾再造一个同名列。

        踩过的坑：读到 ``"\\ufeffreddit_id"`` 时补键在末尾追加 ``reddit_id``，
        于是 ``write_csv`` 用 ``list(rows[0].keys())`` 写出了**重复列**，
        tagged/relevant CSV 整体错位。
        """
        row = {"\ufeffreddit_id": "r1", "title": "t", "subreddit": "s"}
        out = canonicalize_row(row, "posts")
        self.assertEqual(list(out.keys()), ["reddit_id", "title", "subreddit"],
                         "键名要改名并保持原列顺序，不能追加")
        self.assertEqual(out["reddit_id"], "r1")
        # 没有 BOM 时保持恒等
        plain = {"reddit_id": "r1", "title": "t"}
        self.assertEqual(canonicalize_row(plain, "posts"), plain)
        self.assertEqual(strip_bom_keys(plain), plain)

    def test_bom_key_does_not_shadow_a_real_key(self):
        """若同时存在 BOM 名与正常名，非空的胜出（不能让空值把真值挤掉）。"""
        out = strip_bom_keys({"\ufeffreddit_id": "", "reddit_id": "keep", "title": "t"})
        self.assertEqual(out["reddit_id"], "keep")


# --------------------------------------------------------------------------
# 3. 内核端到端：换列名 / 换画像，仍能跑对
# --------------------------------------------------------------------------

def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    import csv
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


class TestKernelAcceptsArbitrarySources(unittest.TestCase):
    """用**新契约列名**（unit_id / channel / text）跑真实内核。"""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        d = Path(cls._td.name)
        cls.tagged = d / "tagged.csv"
        cls.stats = d / "stats.json"
        # 故意只用新契约列名，一个 reddit 字样都不给
        _write_csv(cls.tagged, [
            {"unit_id": "u1", "title": "Great chair", "text": "love it",
             "channel": "Wayfair", "author": "a", "created_at": "2026-01-01T00:00:00Z",
             "url": "http://x/1", "voc_bucket": "A_强相关VOC"},
            {"unit_id": "u2", "title": "Broke", "text": "damaged on arrival",
             "channel": "Wayfair", "author": "b", "created_at": "2026-02-01T00:00:00Z",
             "url": "http://x/2", "voc_bucket": "B_可参考"},
        ], ["unit_id", "title", "text", "channel", "author", "created_at", "url", "voc_bucket"])
        cls.stats.write_text(json.dumps({
            "category": "测试品类", "raw_total": 2, "expired_count": 0,
            "after_date_clean": 2, "bucket_A": 1, "bucket_B": 1,
            "bucket_C": 0, "bucket_D": 0, "final_analysis": 2,
        }, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def _render(self, out_name: str, profile: str | None) -> str:
        out = Path(self._td.name) / out_name
        argv = ["--stats", str(self.stats), "--tagged", str(self.tagged),
                "--category", "测试品类", "--out", str(out)]
        if profile:
            argv += ["--profile", profile]
        r = _run_kernel("generate_dashboard.py", argv)
        self.assertEqual(r.returncode, 0, f"内核失败：{r.stderr[-800:]}")
        return out.read_text(encoding="utf-8")

    def test_kernel_accepts_new_column_names(self):
        """新契约列名（unit_id/channel/text）能被内核正确读取。"""
        html = self._render("new_cols.html", None)
        # 数据进了看板（2 条单元），且渠道值出现在数据区
        self.assertIn('"u1"', html)
        self.assertIn("Wayfair", html)

    def test_same_data_two_profiles_two_vocabularies(self):
        """同一份数据，换画像即换术语 —— 这是「通用」的最小证明。"""
        reddit_html = self._render("as_reddit.html", "reddit")
        wayfair_html = self._render("as_wayfair.html", "wayfair")

        self.assertIn("子版块分布", reddit_html)
        self.assertIn("帖子时间分布", reddit_html)
        self.assertIn("Reddit RSS", reddit_html)

        self.assertIn("站点分布", wayfair_html)
        self.assertIn("评论时间分布", wayfair_html)
        self.assertIn("Wayfair PartReviews 导出", wayfair_html)
        # 用户可见区不得残留 Reddit 专属词（JS 注释与内嵌数据的键名不算）
        visible = visible_html(wayfair_html)
        for banned in ("子版块", "Reddit", "Subreddit", "帖子", "原帖", "RSS", "r/"):
            self.assertNotIn(banned, visible, f"wayfair 输出的可见区里残留了「{banned}」")
        # 而 Reddit 侧必须仍然是老文案（不能为了通用把 Reddit 也改掉）
        reddit_visible = visible_html(reddit_html)
        self.assertIn("子版块分布", reddit_visible)
        self.assertIn("Reddit RSS", reddit_visible)


# --------------------------------------------------------------------------
# 4. Reddit 无回归（需要真实产物 + 原版 skill 脚本）
# --------------------------------------------------------------------------

@unittest.skipUnless(
    (DATA / "reddit_posts_tagged.csv").exists() and (SKILL_SCRIPTS / "generate_dashboard.py").exists(),
    "缺少真实产物或原版 skill 脚本，跳过无回归对比",
)
class TestRedditNoRegression(unittest.TestCase):
    """平台版内核 vs 原版 skill 内核，跑同一份真实数据，逐行比对。

    比对前会先**抠掉「画像卡片」那几块** —— Stage 5 把它从「写死两张卡」改成
    「按框架的活跃桶生成」，卡片 div / 内嵌数据 / 渲染函数 / 调用点都换了写法，
    **行数也变了**（两条内嵌数据并成一条）。直接逐行 zip 会让后面所有行整块错位，
    看起来「771 行都变了」，完全掩盖真相。抠掉之后再比才有意义。

    「抠掉之后是否逐字节相等」由 ``tools/stage5_baseline.py`` 的掩码差分单独证明
    （``python tools/stage5_baseline.py --tag x --compare <baseline.json>``）。

    允许的差异只有三类（都记录在此，防止白名单被悄悄放宽）：
    1. 生成时间戳
    2. 一处措辞改进：矩阵副标题「帖粒度」→「帖子粒度」
    3. ``evidenceComments`` 的键顺序（Python set 迭代序，只校验内容等价）
    """

    #: 允许的最大差异行数：3 处时间戳 + 1 处措辞 + 1 行 evidenceComments（见下）
    MAX_DIFF_LINES = 5

    #: 该行的字典迭代顺序不稳定（Python set 迭代序），故做**内容等价**校验而非逐字符
    _EVIDENCE_PREFIX = "  evidenceComments: "

    #: 画像卡片相关区块被掩掉的数量。两边都是 9：
    #: 原版（改造前）= 2 张卡 div + 两条内嵌数据 + 2 个渲染函数 + 2 个调用点 + 3 处旧注释；
    #: 平台版（改造后）= 2 张卡 div + 1 条内嵌数组 + 1 个渲染函数 + 1 个调用点 + 3 处新注释
    #: + 「新增的说明注释」—— 数量相等纯属巧合，这里只为「掩码表没跟产物脱节」把关。
    _MASKED_BLOCKS = {False: 9, True: 9}

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        d = Path(cls._td.name)
        cls.mine = d / "mine.html"
        cls.theirs = d / "skill.html"
        args = [
            "--stats", str(DATA / "reddit_analysis_stats.json"),
            "--tagged", str(DATA / "reddit_posts_tagged.csv"),
            "--relevant", str(DATA / "reddit_posts_relevant.csv"),
            "--comments", str(DATA / "reddit_comments.csv"),
            "--dimensions", str(DATA.parent.parent / "reports" / "reddit_analysis_dimensions.json"),
            "--features-json", str(DATA / "reddit_analysis_features.json"),
            "--category", "Nursery Glider Recliner",
        ]
        _run_kernel("generate_dashboard.py", args + ["--out", str(cls.mine)])
        subprocess.run([PY, str(SKILL_SCRIPTS / "generate_dashboard.py"), *args,
                        "--out", str(cls.theirs)],
                       capture_output=True, text=True,
                       env=dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    @classmethod
    def _masked_lines(cls) -> tuple[list[str], list[str]]:
        """``(平台版行, 原版行)`` —— 都已抠掉画像卡片那几块。"""
        tdir = str(ROOT / "tools")
        if tdir not in sys.path:
            sys.path.insert(0, tdir)
        import importlib

        S = importlib.import_module("stage5_baseline")
        out = []
        for path, framed in ((cls.mine, True), (cls.theirs, False)):
            txt, n = S.mask_persona_regions(path.read_text(encoding="utf-8"), framed=framed)
            # 掩码没命中 = 产物结构跟掩码表对不上，此时「比出来一致」是假的
            assert n == cls._MASKED_BLOCKS[framed], (
                f"画像区块掩码命中 {n} 次（预期 {cls._MASKED_BLOCKS[framed]}）："
                f"掩码表与产物结构已经脱节，比对结论不可信")
            out.append(txt.splitlines())
        return out[0], out[1]

    def test_line_diff_within_whitelist(self):
        a, b = self._masked_lines()
        self.assertEqual(len(a), len(b), "行数变了，说明结构有改动（画像区块已掩掉）")
        diffs = [(i + 1, x, y) for i, (x, y) in enumerate(zip(b, a)) if x != y]
        self.assertLessEqual(
            len(diffs), self.MAX_DIFF_LINES,
            "差异超出白名单：\n" + "\n".join(f"line {i}\n  skill={x[:110]}\n  mine ={y[:110]}"
                                            for i, x, y in diffs[:10]))

    #: 生成时间戳（页眉 / 数据说明表 / 页脚三处，格式一致）
    _TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

    def test_only_timestamp_and_one_wording_change(self):
        a, b = self._masked_lines()
        for i, (x, y) in enumerate(zip(b, a)):
            if x == y:
                continue
            # 白名单 1：只差时间戳
            if self._TS.sub("<TS>", x) == self._TS.sub("<TS>", y):
                continue
            # 白名单 2：措辞改进（帖粒度 → 帖子粒度）
            if "帖粒度交集计数" in x and "帖子粒度交集计数" in y:
                continue
            # 白名单 3：evidenceComments 的键顺序（内容必须一致）
            if x.startswith(self._EVIDENCE_PREFIX) and y.startswith(self._EVIDENCE_PREFIX):
                jx = json.loads(x[len(self._EVIDENCE_PREFIX):].rstrip().rstrip(","))
                jy = json.loads(y[len(self._EVIDENCE_PREFIX):].rstrip().rstrip(","))
                self.assertEqual(jx, jy, f"evidenceComments 内容不一致（line {i+1}）")
                continue
            self.fail(f"出现未预期的差异 line {i+1}:\n  skill={x[:200]}\n  mine ={y[:200]}")


# --------------------------------------------------------------------------
# 5. 清洗分桶脚本（clean_and_bucket ← reddit_analyzer）：无回归 + 换源
# --------------------------------------------------------------------------

_ECOMMERCE_ROWS = [
    {"unit_id": "w1", "title": "", "text": "The glider recliner is comfortable and well "
     "built, highly recommend, worth every penny", "channel": "Wayfair", "author": "a",
     "created_at": "2026-01-05", "url": "https://www.wayfair.com/x/1", "rating": "5"},
    {"unit_id": "w2", "title": "", "text": "Best recliner glider for a nursery, I would "
     "recommend it to anyone, no problem at all", "channel": "Wayfair", "author": "b",
     "created_at": "2026-02-11", "url": "https://www.wayfair.com/x/2", "rating": "4"},
    {"unit_id": "w3", "title": "", "text": "Great recliner, very comfortable, best "
     "alternative to the expensive brands", "channel": "Wayfair", "author": "c",
     "created_at": "2026-03-02", "url": "https://www.wayfair.com/x/3", "rating": "5"},
    {"unit_id": "w4", "title": "", "text": "sponsored coupon giveaway limited time "
     "offer, buy now", "channel": "Wayfair", "author": "d",
     "created_at": "2026-03-09", "url": "https://www.wayfair.com/x/4", "rating": "1"},
]
_ECOMMERCE_FIELDS = ["unit_id", "title", "text", "channel", "author",
                     "created_at", "url", "rating"]


class TestCleanAndBucketEcommerce(unittest.TestCase):
    """电商源（新契约列名 ``unit_id`` / ``channel`` / ``text``）走同一条清洗管线。"""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        cls.d = Path(cls._td.name)
        cls.input = cls.d / "units.csv"
        _write_csv(cls.input, _ECOMMERCE_ROWS, _ECOMMERCE_FIELDS)
        # convert 会把识别结果写在这里；clean 的 --profile auto 据此推断
        (cls.d / "ingest_report.json").write_text(
            json.dumps({"source": "wayfair", "adapter": "wayfair_reviews"},
                       ensure_ascii=False), encoding="utf-8")
        cls.out_dir = cls.d / "analysis"
        cls.report = cls.out_dir / "data_report.md"
        r = _run_cli([
            "clean", "--input", str(cls.input), "--out-dir", str(cls.out_dir),
            "--category", "测试品类", "--report", str(cls.report),
            "--keywords", "recliner,glider,nursery",
            "--brands", "wayfair,pottery barn",
        ])
        assert r.returncode == 0, r.stderr
        cls.report_text = cls.report.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_profile_auto_detected_from_ingest_report(self):
        r = _run_cli([
            "clean", "--input", str(self.input), "--out-dir", str(self.d / "a2"),
            "--category", "测试品类", "--keywords", "recliner,glider",
        ])
        self.assertIn("画像自动推断：wayfair", r.stdout)

    def test_clean_persists_source_for_downstream_steps(self):
        """clean 把源名落在产物目录，后续 dashboard / report 的 --profile auto 才接得上。"""
        p = self.out_dir / "source_profile.json"
        self.assertTrue(p.exists(), "clean 应写出 source_profile.json")
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["source"], "wayfair")

        # 下一步（dashboard）即使只给 --stats，也应能推断出源
        r = _run_cli([
            "dashboard",
            "--stats", str(resolve_artifact(self.out_dir, "stats")),
            "--tagged", str(resolve_artifact(self.out_dir, "tagged")),
            "--relevant", str(resolve_artifact(self.out_dir, "relevant")),
            "--category", "测试品类", "--out", str(self.d / "dash.html"),
        ])
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("画像自动推断：wayfair", r.stdout)
        html = (self.d / "dash.html").read_text(encoding="utf-8")
        self.assertIn("站点分布", html)
        self.assertNotIn("子版块", visible_html(html))
        # DOM id 也走画像：非 Reddit 源不得残留 chart-subreddits / explorer-subreddit
        self.assertIn('id="chart-channels-small"', html)
        self.assertNotIn("subreddit", re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", html,
                                             flags=re.S | re.I))

    def test_report_uses_ecommerce_vocabulary(self):
        t = self.report_text
        self.assertIn("# 测试品类 Wayfair 数据分析报告", t)
        self.assertIn("数据来源：Wayfair PartReviews 导出（按料号）", t)
        self.assertIn("| 采集总量 | 4 | Wayfair 评论去重后的总数 |", t)
        # 表头用「评论」而不是「帖子」
        self.assertIn("| 品牌/关键词 | 提及评论数 | 占比 |", t)
        self.assertIn("| 项目 | 评论数 | 占比 |", t)
        # 渠道叫「站点」，来源栏叫「代表性评论来源链接」
        self.assertIn("## 三、高频站点", t)
        self.assertIn("| Wayfair | 3 |", t)
        self.assertIn("## 附录：代表性评论来源链接（A 级强相关）", t)
        # 局限性说明换成电商口径
        self.assertIn("- PartReviews 导出为单品维度（Supplier part number），非平台全量", t)
        self.assertIn("- 导出内容含低相关样本，定量分析仅基于 A/B 相关候选", t)
        # 没有「关键词」列时不要输出一行空白表格
        self.assertIn("## 四、高频搜索关键词\n\n无有效数据", t)

    def test_no_reddit_vocabulary_leaks(self):
        t = self.report_text
        for banned in ("Reddit", "Subreddit", "子版块", "帖子", "RSS", "r/"):
            self.assertNotIn(banned, t, f"Wayfair 数据报告里残留了「{banned}」")
        # 概况报告是纯 Markdown（没有数据块/脚本），所以上面是「全文判红」而非只判可见区

    def test_new_column_names_feed_the_pipeline(self):
        """tagged/relevant CSV 里必须出现内核规范列名（证明补键生效、下游可用）。"""
        import csv as _csv

        tagged = resolve_artifact(self.out_dir, "tagged")
        with tagged.open(encoding="utf-8-sig") as f:
            rows = list(_csv.DictReader(f))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["reddit_id"] for r in rows))
        self.assertTrue(all(r["subreddit"] == "Wayfair" for r in rows))
        # 列不重复（BOM 首列曾导致 reddit_id 出现两次）
        header = tagged.read_text(encoding="utf-8-sig").splitlines()[0].split(",")
        self.assertEqual(len(header), len(set(header)), f"列名重复：{header}")
        # 分桶结果与概况报告一致
        buckets = sorted(r["voc_bucket"] for r in rows)
        self.assertEqual(len([b for b in buckets if b.startswith(("A", "B"))]), 3)
        self.assertEqual(len([b for b in buckets if b.startswith("D")]), 1)


# --------------------------------------------------------------------------
# 4.5 产物命名（naming）：Reddit 保旧名、其他源用中性名
# --------------------------------------------------------------------------

class TestArtifactNaming(unittest.TestCase):
    """产物**文件名**也参与去域名化 —— 它会印进报告的「数据文件路径」附录。

    起因：Stage 6 闸门在 Wayfair 报告里抓到
    ``清洗后相关评论：out/wayfair/analysis/reddit_posts_relevant.csv``。
    这不是化妆品 —— 那一行用户真看得到，闸门按 error 级失败报了出来。
    """

    def test_auto_keeps_legacy_for_reddit(self):
        """Reddit 必须保旧名：既有脚本、归档、测试全部零改动，这才是真·零回归。"""
        for kind, legacy in LEGACY_NAMES.items():
            self.assertEqual(artifact_name(kind, "auto", is_reddit=True), legacy)

    def test_auto_uses_neutral_for_other_sources(self):
        for kind, neutral in NEUTRAL_NAMES.items():
            got = artifact_name(kind, "auto", is_reddit=False)
            self.assertEqual(got, neutral)
            self.assertNotIn("reddit", got, "非 Reddit 源的文件名不得带 reddit")

    def test_explicit_modes_override_auto(self):
        self.assertEqual(artifact_name("tagged", "legacy", is_reddit=False),
                         LEGACY_NAMES["tagged"])
        self.assertEqual(artifact_name("tagged", "neutral", is_reddit=True),
                         NEUTRAL_NAMES["tagged"])

    def test_resolve_prefers_neutral_then_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self.assertIsNone(resolve_artifact(d, "tagged"))
            (d / LEGACY_NAMES["tagged"]).write_text("x", encoding="utf-8")
            self.assertEqual(resolve_artifact(d, "tagged").name, LEGACY_NAMES["tagged"])
            (d / NEUTRAL_NAMES["tagged"]).write_text("y", encoding="utf-8")
            self.assertEqual(resolve_artifact(d, "tagged").name, NEUTRAL_NAMES["tagged"])

    def test_unknown_kind_and_mode_raise(self):
        with self.assertRaises(KeyError):
            artifact_name("nope")
        with self.assertRaises(ValueError):
            artifact_name("tagged", "sideways")


@unittest.skipUnless(
    (DATA / "reddit_posts.csv").exists() and (SKILL_SCRIPTS / "reddit_analyzer.py").exists(),
    "缺少真实数据或原版 skill 脚本，跳过清洗脚本的无回归对比",
)
class TestCleanAndBucketRedditNoRegression(unittest.TestCase):
    """平台版 clean_and_bucket vs 原版 reddit_analyzer：同一份真实数据。

    **数据文件要求逐字节一致**（tagged / relevant / summary 三个 CSV）；
    概况报告只允许 4 处措辞改进（都是把 Reddit 专属词换成源中立的说法）：

    1. 「Reddit RSS 采集去重后的帖子总数」→「Reddit 采集去重后的帖子总数」
       （与洞察报告 generate_report.py 的写法统一；保留 ``dataset_label`` 已足够）
    2. 「## 三、高频 Subreddit」→「## 三、高频子版块」
    3. 「## 附录：代表性 Reddit 来源（A 级强相关）」→ 增加「链接」二字，
       与洞察报告的 ``link_section`` 统一
    4. 表格列头「Subreddit」→「子版块」
    """

    ALLOWED_REPORT_DIFFS = {
        ("| 采集总量 | 299 | Reddit RSS 采集去重后的帖子总数 |",
         "| 采集总量 | 299 | Reddit 采集去重后的帖子总数 |"),
        ("## 三、高频 Subreddit", "## 三、高频子版块"),
        ("## 附录：代表性 Reddit 来源（A 级强相关）",
         "## 附录：代表性 Reddit 来源链接（A 级强相关）"),
        ("| 标题 | Subreddit | 相关性 | 分数 | 链接 |",
         "| 标题 | 子版块 | 相关性 | 分数 | 链接 |"),
    }

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        d = Path(cls._td.name)
        cls.mine_dir, cls.theirs_dir = d / "mine", d / "theirs"
        data = DATA / "reddit_posts.csv"
        comments = DATA / "reddit_comments.csv"
        common = ["--input", str(data), "--category", "Nursery",
                  "--keywords", "nursery glider recliner",
                  "--brands", "wayfair,pottery barn,daVinci,delta children"]
        if comments.exists():
            common += ["--comments", str(comments)]

        ours = _run_kernel("clean_and_bucket.py", common + [
            "--out-dir", str(cls.mine_dir),
            "--report", str(cls.mine_dir / "data_report.md")])
        assert ours.returncode == 0, ours.stderr

        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
        subprocess.run([PY, str(SKILL_SCRIPTS / "reddit_analyzer.py"), *common,
                        "--out-dir", str(cls.theirs_dir),
                        "--report", str(cls.theirs_dir / "data_report.md")],
                       capture_output=True, text=True, env=env, check=True)

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_data_files_are_byte_identical(self):
        """数据产物必须逐字节一致 —— 这是「Reddit 行为不变」最硬的证据。"""
        for name in ("reddit_posts_tagged.csv", "reddit_posts_relevant.csv",
                     "reddit_analysis_summary.csv"):
            a = (self.theirs_dir / name).read_bytes()
            b = (self.mine_dir / name).read_bytes()
            self.assertEqual(a, b, f"{name} 出现差异（平台版 vs 原版）")

    def test_stats_json_same_except_paths(self):
        a = json.loads((self.theirs_dir / "reddit_analysis_stats.json").read_text(encoding="utf-8"))
        b = json.loads((self.mine_dir / "reddit_analysis_stats.json").read_text(encoding="utf-8"))
        for k in ("tagged_path", "relevant_path", "summary_path", "stats_json_path"):
            a.pop(k, None)
            b.pop(k, None)
        # input_path 相同（同一份输入文件）
        self.assertEqual(a, b)

    def test_report_diff_within_whitelist(self):
        x = (self.theirs_dir / "data_report.md").read_text(encoding="utf-8")
        y = (self.mine_dir / "data_report.md").read_text(encoding="utf-8")
        # 只差输出目录的绝对路径 → 归一化掉
        x = x.replace(str(self.theirs_dir), "<OUT>")
        y = y.replace(str(self.mine_dir), "<OUT>")
        xs, ys = x.splitlines(), y.splitlines()
        self.assertEqual(len(xs), len(ys), "行数变了，说明结构有改动")
        diffs = {(u, v) for u, v in zip(xs, ys) if u != v}
        unexpected = diffs - self.ALLOWED_REPORT_DIFFS
        self.assertFalse(
            unexpected,
            "概况报告出现未预期的差异：\n" + "\n\n".join(
                f"  skill={u}\n  mine ={v}" for u, v in sorted(unexpected)))
        self.assertTrue(diffs, "白名单里的差异一个都没出现，说明对比没真正跑起来")


# --------------------------------------------------------------------------
# 6. 洞察报告（generate_report）：无回归 + 同数据换词汇只换词
# --------------------------------------------------------------------------

_DIMS = ROOT.parent / "reports" / "reddit_analysis_dimensions.json"
_FEAT = DATA / "reddit_analysis_features.json"


@unittest.skipUnless(
    _DIMS.exists() and _FEAT.exists() and (SKILL_SCRIPTS / "generate_report.py").exists(),
    "缺少 dimensions/features 或原版 skill 脚本，跳过洞察报告的对比",
)
class TestInsightReportNoRegression(unittest.TestCase):
    """平台版 generate_report vs 原版：真实 dimensions.json + 真实数据。

    允许的差异只有三类，都记录在此：

    1. **评论证据来源行（本版是修 bug，不是改口径）**：原版 ``load_comment_index``
       用 ``encoding="utf-8"`` 读评论 CSV，首列名的 UTF-8 BOM 让
       ``r.get("comment_id")`` 永远取不到值 → 索引恒为空 → 评论证据只能打印裸 id。
       本版走 ``canonicalize_rows`` 补键，索引生效，「评论 t1_xxx」变成可点击的
       评论锚点链接。这是把原版**已经写好但被 BOM 废掉的**功能点亮。
    2. 「**高频 Subreddit**」→「**高频子版块**」（与看板的 ``channel_section`` 用词统一）
    3. 「清洗后相关**帖**」→「清洗后相关**帖子**」（报告里的计数单元统一用全称）
    4. 标签集默认路径「tags/reddit_tagset.json」→「tags/tagset.json」（去掉域名前缀）

    5. **生成时间戳**（分钟精度）：属于环境差异，两端渲染先后跨分钟边界时字面值必然不同。
       必须显式归一化 —— 否则该用例会以约 1/60 的概率随机飘红，而这层噪声恰好会掩盖真差异。
    """

    ALLOWED_EXACT = {
        ("**高频 Subreddit**：", "**高频子版块**："),
        ("- 清洗后相关帖：`data\\reddit_analysis\\reddit_posts_relevant.csv`",
         "- 清洗后相关帖子：`data\\reddit_analysis\\reddit_posts_relevant.csv`"),
    }
    _TAGSET = ("- 标签集：`tags/reddit_tagset.json`", "- 标签集：`tags/tagset.json`")
    _SRC_SKILL = "> — 来源：t1_"
    _SRC_MINE = "> — 来源：[评论 t1_"

    #: 生成时间戳（报告里是分钟精度 ``%Y-%m-%d %H:%M``，容忍可选的秒）
    _TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?")

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        d = Path(cls._td.name)
        cls.mine, cls.theirs = d / "mine.md", d / "skill.md"
        cls.args = [
            "--dimensions", str(_DIMS), "--features-json", str(_FEAT),
            "--stats", str(DATA / "reddit_analysis_stats.json"),
            "--relevant", str(DATA / "reddit_posts_relevant.csv"),
            "--category", "Nursery Glider Recliner",
        ]
        if (DATA / "reddit_comments.csv").exists():
            cls.args += ["--comments", str(DATA / "reddit_comments.csv")]

        r = _run_kernel("generate_report.py", cls.args + ["--out", str(cls.mine)])
        assert r.returncode == 0, r.stderr
        env = dict(os.environ, PYTHONHASHSEED="0", PYTHONIOENCODING="utf-8")
        subprocess.run([PY, str(SKILL_SCRIPTS / "generate_report.py"), *cls.args,
                        "--out", str(cls.theirs)],
                       capture_output=True, text=True, env=env, check=True)

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_only_documented_diffs(self):
        xs = self.theirs.read_text(encoding="utf-8").splitlines()
        ys = self.mine.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(xs), len(ys), "行数变了，说明结构有改动")
        unexpected, n_comment = [], 0
        for i, (u, v) in enumerate(zip(xs, ys)):
            if u == v:
                continue
            # 允许的差异 5：只差生成时间戳（环境噪声，先归一化再比）
            if self._TS.sub("<TS>", u) == self._TS.sub("<TS>", v):
                continue
            if u.startswith(self._SRC_SKILL) and v.startswith(self._SRC_MINE):
                n_comment += 1
                continue
            if (u, v) in self.ALLOWED_EXACT or (u, v) == self._TAGSET:
                continue
            unexpected.append(f"line {i + 1}\n  skill={u[:160]}\n  mine ={v[:160]}")
        self.assertFalse(unexpected, "洞察报告出现未预期的差异：\n" + "\n".join(unexpected[:10]))
        self.assertGreater(n_comment, 0,
                           "评论来源链接没生效 —— 说明 canonicalize 的评论索引断了")

    def test_comment_permalink_counts_match(self):
        """两侧的评论证据行数必须一致（只是链接形态不同）。"""
        a = sum(1 for ln in self.theirs.read_text(encoding="utf-8").splitlines()
                if ln.startswith(self._SRC_SKILL))
        b = sum(1 for ln in self.mine.read_text(encoding="utf-8").splitlines()
                if ln.startswith(self._SRC_MINE))
        self.assertEqual(a, b)


@unittest.skipUnless(_DIMS.exists() and _FEAT.exists(), "缺少 dimensions/features，跳过换词汇对比")
class TestInsightReportVocabularySwitch(unittest.TestCase):
    """同一份数据（同一 dimensions.json / 同一 stats），只换画像 —— 词汇跟着换。"""

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        d = Path(cls._td.name)
        base = [
            "--dimensions", str(_DIMS), "--features-json", str(_FEAT),
            "--stats", str(DATA / "reddit_analysis_stats.json"),
            "--relevant", str(DATA / "reddit_posts_relevant.csv"),
            "--category", "Nursery Glider Recliner",
        ]
        if (DATA / "reddit_comments.csv").exists():
            base += ["--comments", str(DATA / "reddit_comments.csv")]
        cls.rd, cls.wf = d / "rd.md", d / "wf.md"
        for prof, out in ((None, cls.rd), ("wayfair", cls.wf)):
            argv = base + ["--out", str(out)]
            if prof:
                argv += ["--profile", prof]
            r = _run_kernel("generate_report.py", argv)
            assert r.returncode == 0, r.stderr
        cls.rd_text = cls.rd.read_text(encoding="utf-8")
        cls.wf_text = cls.wf.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def test_same_numbers_two_vocabularies(self):
        # 标题与数据来源按画像切换
        self.assertTrue(self.rd_text.startswith("# Reddit 用户洞察分析报告"))
        self.assertTrue(self.wf_text.startswith("# Wayfair 用户洞察分析报告"))
        self.assertIn("**数据来源**：Wayfair（经采集与清洗后的 A+B 级评论）", self.wf_text)
        # 表格/小节用「评论」，不再用「帖」
        self.assertIn("| 采集总量 | 299 | Wayfair 评论去重后的总数 |", self.wf_text)
        self.assertIn("**高频站点**：", self.wf_text)
        # 渠道与计数单元措辞不得残留 Reddit 说法（URL 里的 reddit.com 是数据，不算）
        for banned in ("Reddit", "Subreddit", "子版块", "原帖"):
            self.assertNotIn(banned, self.wf_text, f"Wayfair 洞察报告里残留了「{banned}」")
        self.assertIn("**高频子版块**：", self.rd_text)

    def test_numbers_are_identical(self):
        """只换词汇，不换数字。

        验收标准里的「图表与数字完全一致」在这里的严格表述是：**Wayfair 版出现的
        每一个数字，都必须在 Reddit 版里以同样的次数出现**（反向不成立是应该的 ——
        单层源本来就没有「评论单元 7662」这一行，Reddit 版多出它是正确行为）。
        """
        from collections import Counter

        ts = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?")  # 生成时间戳，两次渲染必然差秒
        num = lambda t: Counter(re.findall(r"\d+(?:\.\d+)?", ts.sub("<TS>", t)))  # noqa: E731
        rd, wf = num(self.rd_text), num(self.wf_text)
        invented = wf - rd
        self.assertFalse(invented, f"换画像后凭空多出/改变了数字：{dict(invented)}")
        # 共有的数字必须占绝大多数（防止两边都几乎为空导致上面的检查空转）
        shared = sum((wf & rd).values())
        self.assertGreater(shared, 100)
        # 唯一允许 Reddit 版多出来的，是单层源没有的「次级单元」计数
        self.assertNotIn("**计数单元**：评论 **154** 个 + 评论 **", self.wf_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
