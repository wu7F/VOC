#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voc_core 单元测试（标准库 unittest，无第三方依赖）。

覆盖三块：
1. ``schema``    —— Unit 规范化 / 校验 / 序列化往返 / 三种 legacy 形态
2. ``io_utils``  —— 列名归一化、混合编码解码、值归一化
3. ``adapters``  —— 识别不冲突、归一化产出正确形态

运行::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# 允许直接从项目根运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voc_core import Unit, summarize  # noqa: E402
from voc_core.adapters import detect_best, get  # noqa: E402
from voc_core.io_utils import (  # noqa: E402
    epoch_to_iso,
    norm_key,
    pick,
    read_text_auto,
    to_bool,
    to_int,
)
from voc_core.schema import (  # noqa: E402
    SOURCE_AMAZON,
    SOURCE_REDDIT,
    SOURCE_WAYFAIR,
    TYPE_COMMENT,
    TYPE_POST,
    TYPE_REVIEW,
)


class TestSchema(unittest.TestCase):
    def test_normalize_strips_prefixes(self):
        u = Unit(
            unit_id="t1_abc",
            source="reddit",
            source_type="comment",
            text="hello   world",
            author="u/Someone",
            channel="r/NewParents",
            thread_id="t3_xyz",
            parent_unit_id="t1_parent",
        ).normalize()
        self.assertEqual(u.author, "Someone")
        self.assertEqual(u.channel, "NewParents")
        self.assertEqual(u.thread_id, "xyz")
        self.assertEqual(u.parent_unit_id, "parent")
        self.assertEqual(u.text, "hello world")

    def test_normalize_backfills_thread_id_for_root(self):
        u = Unit(unit_id="p1", source="reddit", source_type=TYPE_POST, title="hi").normalize()
        self.assertEqual(u.thread_id, "p1")
        self.assertTrue(u.is_thread_root)

    def test_validate_collects_errors(self):
        bad = Unit(unit_id="", source="myspace", source_type="gossip")
        errs = bad.validate()
        self.assertTrue(any("unit_id" in e for e in errs))
        self.assertTrue(any("未知 source" in e for e in errs))
        self.assertTrue(any("未知 source_type" in e for e in errs))
        self.assertTrue(any("无内容" in e for e in errs))

    def test_validate_rating_bounds(self):
        u = Unit(unit_id="r1", source="amazon", source_type=TYPE_REVIEW, text="x", rating=9)
        self.assertTrue(any("rating 越界" in e for e in u.validate()))

    def test_row_roundtrip(self):
        u = Unit(
            unit_id="r1", source=SOURCE_AMAZON, source_type=TYPE_REVIEW,
            text="great", title="t", rating=5, verified=True, helpful_votes=12,
            product_id="B001", extra={"variant": "Gray"},
        )
        back = Unit.from_row(u.to_row())
        self.assertEqual(back.unit_id, "r1")
        self.assertEqual(back.rating, 5)
        self.assertIs(back.verified, True)
        self.assertEqual(back.helpful_votes, 12)
        self.assertEqual(back.extra, {"variant": "Gray"})

    def test_from_row_tolerates_garbage(self):
        back = Unit.from_row({"unit_id": "x", "source": "amazon", "source_type": "review",
                              "rating": "五颗星!", "verified": "maybe", "extra": "{broken"})
        self.assertIsNone(back.rating)
        self.assertIsNone(back.verified)
        self.assertEqual(back.extra, {})

    def test_legacy_row_reddit_post(self):
        u = Unit(unit_id="abc", source=SOURCE_REDDIT, source_type=TYPE_POST,
                 title="T", text="B", channel="NAS", url="https://www.reddit.com/x")
        row = u.legacy_row()
        self.assertEqual(row["reddit_id"], "abc")
        self.assertEqual(row["subreddit"], "NAS")
        self.assertEqual(row["summary_text"], "B")

    def test_legacy_row_reddit_comment(self):
        u = Unit(unit_id="c1", source=SOURCE_REDDIT, source_type=TYPE_COMMENT,
                 text="B", thread_id="p1", channel="NAS", title="T")
        row = u.legacy_row()
        self.assertEqual(row["comment_id"], "c1")
        self.assertEqual(row["post_reddit_id"], "p1")
        self.assertEqual(row["is_root_comment"], "True")

    def test_legacy_row_amazon_review(self):
        u = Unit(unit_id="r1", source=SOURCE_AMAZON, source_type=TYPE_REVIEW,
                 text="C", title="T", rating=4, product_id="B001", verified=False)
        row = u.legacy_row()
        self.assertEqual(row["review_id"], "r1")
        self.assertEqual(row["rating"], "4")
        self.assertEqual(row["asin"], "B001")
        self.assertEqual(row["verified"], "false")

    def test_summarize(self):
        units = [
            Unit(unit_id="a", source=SOURCE_REDDIT, source_type=TYPE_POST, text="x",
                 thread_id="a", created_at="2026-01-01T00:00:00Z", lang="en"),
            Unit(unit_id="b", source=SOURCE_AMAZON, source_type=TYPE_REVIEW, text="y",
                 thread_id="B001", rating=5),
        ]
        s = summarize(units)
        self.assertEqual(s["total_units"], 2)
        self.assertEqual(s["threads"], 2)
        self.assertEqual(s["by_source"], {"reddit": 1, "amazon": 1})
        self.assertEqual(s["rating_distribution"], {"5": 1})
        self.assertEqual(s["dated_units"], 1)
        self.assertAlmostEqual(s["undated_ratio"], 0.5)


class TestIoUtils(unittest.TestCase):
    def test_norm_key_unifies_separators(self):
        # 带分隔符的三种写法归一到同一形态
        for variant in ("Review Title", "review-title", "review_title", "REVIEW_TITLE"):
            self.assertEqual(norm_key(variant), "review_title", variant)
        # 无分隔符的驼峰写法仅小写化（无法还原词边界，属已知取舍）
        self.assertEqual(norm_key("ReviewTitle"), "reviewtitle")

    def test_norm_key_strips_bom(self):
        self.assertEqual(norm_key("\ufeffreddit_id"), "reddit_id")

    def test_pick_matches_any_separator_style(self):
        row = {"Review Title": "Nice", "Review Text": "Body", "Rating": "5"}
        self.assertEqual(pick(row, ["review_title"]), "Nice")
        self.assertEqual(pick(row, ["review_text", "body"]), "Body")
        self.assertEqual(pick(row, ["rating"]), "5")

    def test_pick_marks_original_keys(self):
        row = {"Review Title": "Nice", "extra_col": "v"}
        used: set[str] = set()
        pick(row, ["review_title"], mark_used=used)
        self.assertIn("Review Title", used)

    def test_pick_respects_priority(self):
        row = {"a": "", "b": "second"}
        self.assertEqual(pick(row, ["a", "b"]), "second")

    def test_to_int(self):
        self.assertEqual(to_int("5.0"), 5)
        self.assertEqual(to_int("1,234"), 1234)
        self.assertEqual(to_int("5 星"), 5)
        self.assertIsNone(to_int("N/A"))
        self.assertIsNone(to_int(""))

    def test_to_bool(self):
        self.assertIs(to_bool("True"), True)
        self.assertIs(to_bool("是"), True)
        self.assertIs(to_bool("VP"), True)
        self.assertIs(to_bool("no"), False)
        self.assertIsNone(to_bool("maybe"))

    def test_epoch_to_iso(self):
        self.assertEqual(epoch_to_iso("1700000000"), "2023-11-14T22:13:20Z")
        self.assertEqual(epoch_to_iso(1700000000000), "2023-11-14T22:13:20Z")
        self.assertEqual(epoch_to_iso("2026-01-01"), "2026-01-01")  # 非数字原样返回

    def test_read_text_auto_handles_bom_and_gbk(self):
        with tempfile.TemporaryDirectory() as td:
            bom_file = Path(td) / "bom.csv"
            bom_file.write_bytes("标题,正文\n你好,世界\n".encode("utf-8-sig"))
            text = read_text_auto(bom_file)
            self.assertNotIn("\ufeff", text)
            self.assertIn("你好", text)

            gbk_file = Path(td) / "gbk.csv"
            gbk_file.write_bytes("标题,正文\n沙发,很舒服\n".encode("gbk"))
            self.assertIn("沙发", read_text_auto(gbk_file))

    def test_read_text_auto_mixed_encoding_picks_fewer_replacements(self):
        """混合编码：UTF-8 正文 + GBK 弯引号（A1 AF）不得整篇变成乱码。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "mixed.csv"
            body = "标题,正文\nChair,it’s great\n".encode("utf-8")
            body = body.replace("’".encode("utf-8"), b"\xa1\xaf")  # 塞入 GBK 字节
            p.write_bytes(body)
            text = read_text_auto(p)
            self.assertIn("Chair", text)
            self.assertIn("great", text)


class TestAdapters(unittest.TestCase):
    def _csv(self, rows: str) -> Path:
        td = tempfile.mkdtemp()
        p = Path(td) / "data.csv"
        p.write_text(rows, encoding="utf-8-sig")
        return p

    def test_registry_lookup_by_adapter_id_and_source(self):
        self.assertEqual(get("amazon_reviews").source, SOURCE_AMAZON)
        self.assertEqual(get("amazon").source, SOURCE_AMAZON)  # 源唯一时可省略
        self.assertEqual(get("wayfair_reviews").source, SOURCE_WAYFAIR)
        with self.assertRaises(KeyError):
            get("reddit")  # 帖子和评论两个适配器，有歧义

    def test_detect_amazon_by_asin_column(self):
        p = self._csv("Review ID,ASIN,Rating,Review Title,Review Text\nr1,B001,5,Good,Nice one\n")
        adapter, scores = detect_best(p)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.adapter_id, "amazon_reviews")
        self.assertGreater(scores["amazon_reviews"], 0.5)

    def test_detect_reddit_comments_by_postId(self):
        p = self._csv("id,postId,body,author,date,parentId,type\n"
                      "t1_a,t3_p,hello,user,2026-01-01,t3_p,comment\n")
        adapter, _ = detect_best(p)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.adapter_id, "reddit_comments")

    def test_detect_reddit_posts_by_subreddit(self):
        p = self._csv("id,title,selftext,subreddit,author,created_utc\n"
                      "abc,My NAS build,details here,homelab,bob,1700000000\n")
        adapter, _ = detect_best(p)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.adapter_id, "reddit_posts")

    def test_detect_returns_none_for_ambiguous_generic_export(self):
        """只有通用列名（无平台特征）时不应瞎猜，应让用户显式指定。"""
        p = self._csv("Rating,Review Text,Title\n5,good,ok\n")
        adapter, scores = detect_best(p)
        self.assertIsNone(adapter)
        self.assertTrue(all(v < 0.3 for v in scores.values()))

    def test_amazon_normalize(self):
        p = self._csv("Review ID,ASIN,Rating,Review Title,Review Text,Reviewer,Verified Purchase,Helpful Votes\n"
                      "r1,B001,5,Great,Works well,alice,Yes,7\n"
                      "r2,B002,2,Bad,Broke fast,bob,No,1\n")
        units = get("amazon_reviews").normalize(p)
        self.assertEqual(len(units), 2)
        u = units[0]
        self.assertEqual(u.source, SOURCE_AMAZON)
        self.assertEqual(u.source_type, TYPE_REVIEW)
        self.assertEqual(u.rating, 5)
        self.assertIs(u.verified, True)
        self.assertEqual(u.helpful_votes, 7)
        self.assertEqual(u.product_id, "B001")
        self.assertEqual(u.thread_id, "B001")
        self.assertIn("amazon.com", u.url)

    def test_amazon_rating_string_forms(self):
        p = self._csv("ASIN,Review Text,Rating\nB1,ok,5.0 out of 5 stars\nB1,meh,2星\n")
        units = get("amazon_reviews").normalize(p)
        self.assertEqual([u.rating for u in units], [5, 2])

    def test_amazon_dedupes_identical_rows(self):
        row = "B1,5,Great,Works well\n"
        p = self._csv("ASIN,Rating,Review Title,Review Text\n" + row + row)
        self.assertEqual(len(get("amazon_reviews").normalize(p)), 1)

    def test_amazon_keeps_rows_without_rating(self):
        """缺星级不该丢数据 —— Wayfair/亚马逊的部分导出确实没有星级列。"""
        p = self._csv("ASIN,Review Title,Review Text\nB1,Nice,Really nice chair\n")
        units = get("amazon_reviews").normalize(p)
        self.assertEqual(len(units), 1)
        self.assertIsNone(units[0].rating)

    def test_wayfair_normalize_with_space_separated_headers(self):
        p = self._csv("Review ID,SKU,Overall Rating,Review Title,Review Text,"
                      "Reviewer Nickname,Submission Time,Verified Purchaser\n"
                      "w1,W123,4,Love it,Great quality,jen,2026-02-03,Yes\n")
        units = get("wayfair_reviews").normalize(p)
        self.assertEqual(len(units), 1)
        u = units[0]
        self.assertEqual(u.source, SOURCE_WAYFAIR)
        self.assertEqual(u.rating, 4)
        self.assertEqual(u.product_id, "W123")
        self.assertIs(u.verified, True)
        self.assertEqual(u.author, "jen")
        self.assertEqual(u.url, "")  # 不臆造 Wayfair 链接

    def test_reddit_posts_normalize_and_merge_duplicates(self):
        p = self._csv(
            "id,title,selftext,subreddit,author,created_utc,query\n"
            "abc,Best NAS?,looking for advice,homelab,bob,1700000000,nas\n"
            "abc,Best NAS?,looking for advice,homelab,bob,1700000000,backup\n"
        )
        adapter = get("reddit_posts")
        units = adapter.normalize(p)
        self.assertEqual(len(units), 1)          # 同 id 合并
        u = units[0]
        self.assertEqual(u.source_type, TYPE_POST)
        self.assertEqual(u.channel, "homelab")
        self.assertEqual(u.created_at, "2023-11-14T22:13:20Z")
        self.assertIn("nas", u.extra.get("matched_queries", ""))
        self.assertIn("backup", u.extra.get("matched_queries", ""))

    def test_reddit_posts_recovers_subreddit_from_url(self):
        p = self._csv("id,title,url\np1,Hi,/r/NewParents/comments/p1/hi/\n")
        units = get("reddit_posts").normalize(p)
        self.assertEqual(units[0].channel, "NewParents")

    def test_reddit_comments_normalize(self):
        p = self._csv(
            "id,postId,body,author,date,parentId,type\n"
            "t1_c1,t3_p1,Nice one,alice,1700000000,t3_p1,comment\n"
            "t1_c2,t3_p1,Agreed,bob,1700000001,t1_c1,comment\n"
            "t3_p1,,I am the post,carol,1700000002,,post\n"
        )
        units = get("reddit_comments").normalize(p)
        self.assertEqual(len(units), 2)          # type=post 的行被排除
        self.assertEqual(units[0].unit_id, "c1")
        self.assertEqual(units[0].thread_id, "p1")
        self.assertEqual(units[0].parent_unit_id, "")   # 根评论
        self.assertEqual(units[1].parent_unit_id, "c1")  # 回复评论
        self.assertEqual(units[0].extra["is_root_comment"], "True")
        self.assertEqual(units[1].extra["is_root_comment"], "False")

    def test_all_units_validate_clean(self):
        p = self._csv("ASIN,Rating,Review Text,Review Title\nB1,5,ok,fine\n")
        for u in get("amazon_reviews").normalize(p):
            self.assertEqual(u.validate(), [], f"{u.unit_id} 校验未通过")


class TestWayfairRealSchema(unittest.TestCase):
    """Wayfair 官方 ``PartReviews`` 导出的真实结构（2026-10 实测校准）。

    样本文件 ``0424电动 评论.xlsx`` 的列名是
    ``Supplier part number / Launch date / Product name / Class name /
    Review date / Rating / Review text / Store name / Annotations``，
    与亚马逊、以及此前按通用电商列名写的别名表**都对不上**。这里把那次
    校准固化成回归用例 —— 别名表一旦被人改错，这些用例会立刻失败。
    """

    HEADER = ("Supplier part number,Launch date,Product name,Class name,"
              "Review date,Rating,Review text,Store name,Annotations")

    def _csv(self, rows: str, name: str = "export.csv") -> Path:
        # 刻意用中性文件名：把"靠列名/取值识别"与"靠文件名识别"两条路径分开测，
        # 否则 fixture 名里带 wayfair 会干扰判分。
        td = tempfile.mkdtemp()
        p = Path(td) / name
        p.write_text(rows, encoding="utf-8-sig")
        return p

    def _real_file(self, extra_row: str = "") -> Path:
        base = [
            'SR-YJDXR240424-BE,2025-09-12,IBRA1740 - Recliner Chair,Recliners,'
            '2025-11-27,5,Easy to set up and is very comfortable,Wayfair,'
            '"Material - Comfort, Quality/Price"',
            'SR-YJDXR240424-BE,2025-09-12,IBRA1740 - Recliner Chair,Recliners,'
            '2025-12-08,5,I was a little nervous purchasing this recliner,Wayfair,'
            '"Time - Delay, Assembly - Instructions"',
            'SR-YJDXR240424-BE,2025-09-12,IBRA1740 - Recliner Chair,Recliners,'
            '2025-12-18,4,The color is a bit more yellow than I thought,Wayfair,'
            'Color - Brightness',
        ]
        if extra_row:
            base.append(extra_row)
        return self._csv(self.HEADER + "\n" + "\n".join(base) + "\n")

    # -- 识别 -------------------------------------------------------------

    def test_official_columns_identify_wayfair_without_filename(self):
        """文件名不含 'wayfair' 也要认得出来 —— 用户的导出常叫『0424电动 评论.xlsx』。"""
        p = self._real_file()
        adapter, scores = detect_best(p)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.adapter_id, "wayfair_reviews")
        self.assertGreaterEqual(scores["wayfair_reviews"], 0.9)
        # 关键：不得被亚马逊抢走
        self.assertLess(scores["amazon_reviews"], 0.3)

    def test_detect_by_store_value_when_columns_renamed(self):
        """列名被工具改写成通用名时，靠渠道列的取值（恒为 Wayfair）定性。"""
        p = self._csv("Review Date,Rating,Review Text,Store name\n"
                      "2025-11-27,5,Nice chair,Wayfair\n")
        adapter, _ = detect_best(p)
        self.assertIsNotNone(adapter)
        self.assertEqual(adapter.adapter_id, "wayfair_reviews")

    def test_generic_wayfair_columns_do_not_capture_amazon(self):
        """反向验证：亚马逊列结构的文件不该被 Wayfair 适配器吞掉。"""
        p = self._csv("Review ID,ASIN,Rating,Review Title,Review Text,Country\n"
                      "r1,B001,5,Nice,Great chair,US\n")
        adapter, scores = detect_best(p)
        self.assertEqual(adapter.adapter_id, "amazon_reviews")
        self.assertLess(scores["wayfair_reviews"], 0.3)

    def test_filename_alone_is_a_hint_not_a_verdict(self):
        """文件名含 wayfair 会加分，但列结构完全对不上时亚马逊仍应胜出。"""
        p = self._csv("Review ID,ASIN,Rating,Review Title,Review Text\n"
                      "r1,B001,5,Nice,Great chair\n", name="wayfair_export.csv")
        adapter, scores = detect_best(p)
        self.assertEqual(adapter.adapter_id, "amazon_reviews")
        self.assertGreater(scores["amazon_reviews"], scores["wayfair_reviews"])

    # -- 归一化 -----------------------------------------------------------

    def test_official_columns_map_to_unit_fields(self):
        units = get("wayfair_reviews").normalize(self._real_file())
        self.assertEqual(len(units), 3)
        u = units[0]
        self.assertEqual(u.source, SOURCE_WAYFAIR)
        self.assertEqual(u.source_type, TYPE_REVIEW)
        # 料号是商品标识 —— 缺了它 thread_id 会退化成"每条评论一个讨论串"
        self.assertEqual(u.product_id, "SR-YJDXR240424-BE")
        self.assertEqual(u.thread_id, "SR-YJDXR240424-BE")
        self.assertEqual(u.channel, "Wayfair")
        self.assertIn("IBRA1740", u.product_name)
        self.assertEqual(u.title, "")          # 该导出无标题列
        self.assertEqual(u.rating, 5)
        self.assertEqual(u.url, "")            # 不臆造 Wayfair 链接
        self.assertEqual(u.validate(), [])

    def test_review_date_normalized_to_iso(self):
        """Wayfair 给的是 'YYYY-MM-DD'，需补时间部分以统一下游解析口径。"""
        units = get("wayfair_reviews").normalize(self._real_file())
        self.assertEqual(units[0].created_at, "2025-11-27T00:00:00Z")
        self.assertEqual(units[2].created_at, "2025-12-18T00:00:00Z")

    def test_extra_keeps_launch_date_and_class_name(self):
        u = get("wayfair_reviews").normalize(self._real_file())[0]
        self.assertEqual(u.extra["Launch date"], "2025-09-12")
        self.assertEqual(u.extra["Class name"], "Recliners")

    def test_platform_annotations_structured(self):
        """Wayfair 自带的 Annotations 列拆成列表留存（先验，不作结论）。"""
        units = get("wayfair_reviews").normalize(self._real_file())
        self.assertEqual(
            units[0].extra["platform_annotations"],
            ["Material - Comfort", "Quality/Price"],
        )
        self.assertEqual(
            units[1].extra["platform_annotations"],
            ["Time - Delay", "Assembly - Instructions"],
        )

    def test_legacy_row_shape_matches_downstream_kernel(self):
        u = get("wayfair_reviews").normalize(self._real_file())[0]
        row = u.legacy_row()
        self.assertEqual(row["asin"], "SR-YJDXR240424-BE")   # product_id 落 asin 列
        self.assertEqual(row["country"], "Wayfair")           # channel 落 country 列
        self.assertEqual(row["rating"], "5")
        self.assertEqual(row["source"], "wayfair")

    # -- 台账（不静默丢数据） ---------------------------------------------

    def test_rating_only_rows_counted_not_silently_dropped(self):
        """只打星、不写字的评论：不进内容分析，但必须被计数并计入星级分布。

        实测该样本 97 行里有 2 行如此 —— 静默丢弃会让星级分布与平台后台对不上。
        """
        p = self._real_file(
            'SR-YJDXR240424-BE,2025-09-12,IBRA1740 - Recliner Chair,Recliners,'
            '2025-11-20,5,,Wayfair,'
        )
        adapter = get("wayfair_reviews")
        units = adapter.normalize(p)
        self.assertEqual(len(units), 3)                    # 仍只有 3 条可分析
        st = adapter.last_stats
        self.assertEqual(st["rows_total"], 4)
        self.assertEqual(st["kept"], 3)
        self.assertEqual(st["skipped_no_text"], 1)
        self.assertEqual(st["rating_only_distribution"], {"5": 1})

    def test_fully_empty_row_counted_separately(self):
        p = self._real_file(
            'SR-YJDXR240424-BE,2025-09-12,IBRA1740 - Recliner Chair,Recliners,'
            '2025-11-20,,,Wayfair,'
        )
        adapter = get("wayfair_reviews")
        adapter.normalize(p)
        self.assertEqual(adapter.last_stats["skipped_empty"], 1)
        self.assertEqual(adapter.last_stats["skipped_no_text"], 0)


class TestDateParsing(unittest.TestCase):
    """电商导出日期格式的规范化（只做无歧义转换）。"""

    def setUp(self):
        self.parse = get("wayfair_reviews").parse_date

    def test_iso_date_gets_time_part(self):
        self.assertEqual(self.parse("2025-11-27"), "2025-11-27T00:00:00Z")
        self.assertEqual(self.parse("2025/11/27"), "2025-11-27T00:00:00Z")
        self.assertEqual(self.parse("2025.11.27"), "2025-11-27T00:00:00Z")

    def test_iso_datetime_normalized(self):
        self.assertEqual(self.parse("2025-11-27 14:30"), "2025-11-27T14:30:00Z")
        self.assertEqual(self.parse("2025-11-27T14:30:05"), "2025-11-27T14:30:05Z")

    def test_chinese_date(self):
        self.assertEqual(self.parse("2025年11月27日"), "2025-11-27T00:00:00Z")

    def test_epoch_seconds_and_millis(self):
        self.assertEqual(self.parse("1700000000"), "2023-11-14T22:13:20Z")
        self.assertEqual(self.parse("1700000000000"), "2023-11-14T22:13:20Z")

    def test_ambiguous_us_format_left_untouched(self):
        """11/27/2025 无法区分月/日，猜错会让时间整体错位 —— 原样保留。"""
        self.assertEqual(self.parse("11/27/2025"), "11/27/2025")

    def test_empty(self):
        self.assertEqual(self.parse(""), "")
        self.assertEqual(self.parse(None), "")


class TestSplitTerms(unittest.TestCase):
    """关键词/品牌词表切分：必须兼容全角逗号等中文标点。

    事故背景（2026-10-12 job 20261012-153454-analysis）：用户用中文输入法
    打出全角逗号「，」，旧逻辑只按半角 "," 切 → 整串被当成一个词 →
    kw_hits 全 0 → 209 条全部落 D 桶（A+B=0）。
    """

    def test_fullwidth_comma(self):
        from voc_core.kernel.clean_and_bucket import split_terms
        self.assertEqual(
            split_terms("nursery glider recliner，nursery recliner，recliner"),
            ["nursery glider recliner", "nursery recliner", "recliner"],
        )

    def test_mixed_separators(self):
        from voc_core.kernel.clean_and_bucket import split_terms
        self.assertEqual(
            split_terms("a，b;c；d、e\nf\t g , h"),
            ["a", "b", "c", "d", "e", "f", "g", "h"],
        )

    def test_empty_and_blank(self):
        from voc_core.kernel.clean_and_bucket import split_terms
        self.assertEqual(split_terms(""), [])
        self.assertEqual(split_terms("，；、"), [])
        self.assertEqual(split_terms(None), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

