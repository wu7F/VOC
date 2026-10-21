# -*- coding: utf-8 -*-
"""三平台数据适配层。

把不同来源的数据统一归一化为平台内部的「讨论单元」模型：

    {
      "unit_id": "wf_0001",
      "title": "……",            # 展示用标题（Reddit = 帖子标题；电商 = 评论文首句）
      "body":  "……",            # 正文（Reddit = 帖子正文；电商 = 评论剩余文本）
      "comments": [{"comment_id","author","body"}],   # Reddit 有；电商为空
      "meta": {
        "platform": "Amazon" | "Wayfair" | "Reddit",
        "product": "…", "sku": "…", "category": "…",
        "rating": 5, "date": "2026-01-01", "url": "…",
        "brands": ["…"], "annotations": ["Material - Comfort", ...]
      }
    }

三个平台共用同一套下游分析管线，因此新增平台只需新增一个适配函数。
"""

from __future__ import annotations

import csv
import io
import json
import re
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

# ---------------------------------------------------------------------------
# 读表
# ---------------------------------------------------------------------------

# 单文件读取行数上限（防止误传超大表导致内存溢出；超出时会在来源说明中显式标注）
MAX_TABLE_ROWS = 200_000
_HEADER_SCAN_ROWS = 15


def _best_header(rows: list[list]) -> int:
    """在前若干行中挑「非空单元格最多」的一行作为表头行。"""
    best_i, best_n = 0, -1
    for i, row in enumerate(rows):
        n = sum(1 for c in row if c is not None and str(c).strip())
        if n > best_n:
            best_n, best_i = n, i
    return best_i


def _find_header_row(rows: list[list]) -> int:
    """定位表头行。

    台账类表格常见结构：前几行是**说明行**（如「考核：每日跟进…」这种长句），
    之后才是真正的表头行。判据：
      · 非空格数 >= 2
      · 且**过半**的非空单元格是短词（<=10 字）—— 说明行长句会被排除
    都不满足时退回「非空最多」。
    """
    def _vals(r):
        return [str(c).strip() for c in r if c is not None and str(c).strip()]

    for i, r in enumerate(rows):
        vals = _vals(r)
        if len(vals) < 2:
            continue
        short = sum(1 for v in vals if len(v) <= 10)
        if short * 2 > len(vals):
            return i
    return _best_header(rows)


def _make_cols(hdr: list) -> list[str]:
    """生成去重后的列名。"""
    seen: dict[str, int] = {}
    cols: list[str] = []
    for i, h in enumerate(hdr):
        name = str(h).strip() if h is not None else ""
        name = name or f"col{i}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        cols.append(name)
    return cols


def _row_to_dict(row: list, cols: list[str]) -> dict | None:
    if not any(c is not None and str(c).strip() for c in row):
        return None
    d = {}
    for k, v in zip(cols, row):
        d[k] = "" if v is None else str(v).strip()
    return d


def _pick_sheet(wb, names: list[str]) -> str:
    """多 sheet 时，按「表头行非空列数」挑选最像数据表的 sheet。"""
    best_name, best_score = names[0], -1
    for sn in names:
        ws = wb[sn]
        buf = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= _HEADER_SCAN_ROWS:
                break
            buf.append(list(row))
        if not buf:
            continue
        score = sum(1 for c in buf[_find_header_row(buf)] if c is not None and str(c).strip())
        if score > best_score:
            best_score, best_name = score, sn
    return best_name


def _read_xlsx(path: Path) -> tuple[list[dict], str]:
    """读取 xlsx：自动挑选数据表 sheet、定位表头行，并**流式读完全表**。

    注意：此处必须逐行流式读取整张表。早先实现为「采样后只读前 N 行」，
    会在超大文件上**静默丢弃绝大部分数据**（7,932 行的文件只读到 400 行），
    是必须避免的失败模式。
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    names = list(wb.sheetnames)
    if not names:
        wb.close()
        raise ValueError(f"{path.name} 中没有任何工作表")

    if len(names) == 1:
        chosen = names[0]
    else:
        chosen = _pick_sheet(wb, names)
        wb.close()
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)

    ws = wb[chosen]
    it = ws.iter_rows(values_only=True)

    # 先缓存前若干行用于定位表头，其余全部流式读取
    head_buf: list[list] = []
    for _ in range(_HEADER_SCAN_ROWS):
        try:
            head_buf.append(list(next(it)))
        except StopIteration:
            break
    if not head_buf:
        wb.close()
        raise ValueError(f"{path.name}#{chosen} 中没有数据")

    hi = _find_header_row(head_buf)
    cols = _make_cols(head_buf[hi])
    if len(cols) < 2:
        wb.close()
        raise ValueError(f"{path.name}#{chosen} 未能识别出有效表头（非空列数 < 2）")

    rows: list[dict] = []
    truncated = False
    for raw in head_buf[hi + 1:]:
        d = _row_to_dict(raw, cols)
        if d:
            rows.append(d)
    for raw in it:
        if len(rows) >= MAX_TABLE_ROWS:
            truncated = True
            break
        d = _row_to_dict(list(raw), cols)
        if d:
            rows.append(d)
    wb.close()

    src = f"{path.name}#{chosen}"
    if truncated:
        src += f"（行数超过上限，仅读取前 {MAX_TABLE_ROWS} 行）"
    return rows, src


def _sheet_rows(ws, sheet_name: str) -> list[dict]:
    """流式读取单个 sheet：定位表头行 + 读全表，返回行字典列表。"""
    it = ws.iter_rows(values_only=True)

    head_buf: list[list] = []
    for _ in range(_HEADER_SCAN_ROWS):
        try:
            head_buf.append(list(next(it)))
        except StopIteration:
            break
    if not head_buf:
        return []
    hi = _find_header_row(head_buf)
    cols = _make_cols(head_buf[hi])
    if len(cols) < 2:
        return []

    rows: list[dict] = []
    for raw in head_buf[hi + 1:]:
        d = _row_to_dict(raw, cols)
        if d:
            rows.append(d)
    for raw in it:
        if len(rows) >= MAX_TABLE_ROWS:
            break
        d = _row_to_dict(list(raw), cols)
        if d:
            rows.append(d)
    return rows


def _read_xlsx_multi(path: Path) -> tuple[list[dict], str]:
    """读取 xlsx 的**全部业务 sheet** 并合并（售后 VOC 台账用）。

    - 跳过 WPS 保留表（WpsReserved_*）和空表
    - 各 sheet 表头不同也没关系：行是 dict，列名取并集后各自保留自己的键
    - 表头行自动定位（说明行/空行在前也能找对）
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    names = [n for n in wb.sheetnames if not n.lower().startswith("wpsreserved")]
    merged: list[dict] = []
    used: list[str] = []
    for sn in names:
        try:
            rows = _sheet_rows(wb[sn], sn)
        except Exception:
            continue
        if rows:
            merged.extend(rows)
            used.append(f"{path.name}#{sn}({len(rows)}行)")
    wb.close()
    if not merged:
        raise ValueError(f"{path.name} 中没有读到任何数据行")
    return merged, " + ".join(used)


def find_header_row(rows: list[list]) -> int:
    """定位表头行（公开入口，供飞书表格读取复用）。"""
    return _find_header_row(rows)


def make_cols(hdr: list) -> list[str]:
    """生成去重后的列名（公开入口）。"""
    return _make_cols(hdr)


def row_to_dict(row: list, cols: list[str]) -> dict | None:
    """行 → dict（公开入口）。"""
    return _row_to_dict(row, cols)


def read_table(path: str | Path, multi_sheet: bool = False) -> tuple[list[dict], str]:
    """统一读表入口，返回 (行列表, 来源描述)。

    multi_sheet=True 时（售后 VOC 用）：xlsx 的**所有业务 sheet** 都会读进来并
    合并成一份行列表，各 sheet 表头取并集（列名不同的 sheet 各自保留自己的键）。
    WPS 生成的保留表（WpsReserved_*）会被跳过。
    """
    p = Path(path)
    suf = p.suffix.lower()
    if suf in (".xlsx", ".xlsm", ".xls"):
        if multi_sheet:
            return _read_xlsx_multi(p)
        return _read_xlsx(p)
    if suf in (".jsonl", ".ndjson"):
        rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rows, p.name
    if suf == ".json":
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            for k in ("data", "posts", "results", "items", "rows"):
                if isinstance(data.get(k), list):
                    return data[k], p.name
            return [data], p.name
        return data, p.name
    # CSV / TSV（容忍 BOM）
    return _read_csv(p)


def _read_csv(path: Path) -> tuple[list[dict], str]:
    """读取 CSV/TSV。

    与 xlsx 路径保持一致的行为：
      · 跳过全空行
      · 首行若不是有效表头（如前面有说明行/空行），自动定位真正的表头行
    """
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    suf = path.suffix.lower()
    delim = "\t" if (suf == ".tsv" or text.count("\t") > text.count(",")) else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return [], path.name

    def _nonnull(r):
        return sum(1 for c in r if c is not None and str(c).strip())

    hi = _find_header_row(rows[:_HEADER_SCAN_ROWS])
    if _nonnull(rows[hi]) < 2:
        hi = 0
    cols = _make_cols(rows[hi])
    out: list[dict] = []
    for raw in rows[hi + 1:]:
        d = _row_to_dict(raw, cols)
        if d:
            out.append(d)
    return out, path.name


# ---------------------------------------------------------------------------
# 列名解析
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def resolve_columns(rows: list[dict]) -> dict[str, str]:
    """把实际列名解析到语义字段。返回 {语义字段: 实际列名}。"""
    if not rows:
        return {}
    cols = list(rows[0].keys())
    norm = {_norm(c): c for c in cols}

    ALIAS = {
        "id": ["redditid", "commentid", "id", "postid", "submissionid", "supplierpartnumber",
               "asin", "sku", "productid", "partnumber", "itemid", "productsku",
               "listingid", "parentasin", "reviewid"],
        "product": ["productname", "producttitle", "itemname", "product",
                    "itemtitle", "title", "listingtitle", "posttitle"],
        "category": ["classname", "category", "productcategory", "class",
                     "subcategory", "categoryname"],
        "date": ["reviewdate", "date", "reviewdateutc", "created", "createdat",
                 "reviewdatetime", "published", "postdate", "submitdate"],
        "rating": ["rating", "starrating", "overallrating", "stars",
                   "reviewrating", "averagerating", "score"],
        "text": ["reviewtext", "review", "reviewbody", "body", "content",
                 "comment", "text", "reviewcontent", "description",
                 "selftext", "summarytext", "postbody"],
        "store": ["storename", "store", "platform", "marketplace", "channel",
                  "site", "source", "retailer", "subreddit"],
        "annotations": ["annotations", "tags", "aspects", "topics",
                        "categories", "labels"],
        "author": ["author", "reviewer", "nickname", "username", "customername",
                   "reviewername", "user", "customer"],
        "url": ["url", "link", "permalink", "producturl", "reviewurl",
                "commentsurl", "productlink", "feedurl"],
        "helpful": ["helpfulvotes", "helpful", "foundhelpful", "votes"],
    }
    out = {}
    for field, cands in ALIAS.items():
        for c in cands:
            if c in norm:
                out[field] = norm[c]
                break
    return out


def detect_platform(rows: list[dict], cols: dict[str, str]) -> str:
    """从数据内容推断平台。"""
    if cols.get("store"):
        vals = [str(r.get(cols["store"], "")).lower() for r in rows[:500]]
        joined = " ".join(vals)
        if "wayfair" in joined:
            return "Wayfair"
        if "amazon" in joined:
            return "Amazon"
        # 出现 subreddit 特征
        if any(v and not v.isspace() for v in vals) and _looks_like_reddit(rows, cols):
            return "Reddit"
    if _looks_like_reddit(rows, cols):
        return "Reddit"
    return ""


def _looks_like_reddit(rows: list[dict], cols: dict[str, str]) -> bool:
    if not rows:
        return False
    keys = " ".join(_norm(k) for k in rows[0].keys())
    if any(k in keys for k in ("subreddit", "redditid", "postredditid", "commentid")):
        return True
    # URL 指向 reddit
    ucol = cols.get("url")
    if ucol:
        n = sum(1 for r in rows[:200] if "reddit.com" in str(r.get(ucol, "")).lower())
        if n > max(1, len(rows[:200]) * 0.3):
            return True
    return False


# ---------------------------------------------------------------------------
# 品牌识别
# ---------------------------------------------------------------------------

def parse_brand_list(raw: str | list) -> list[str]:
    """拆分品牌清单。

    注意：分隔符只认 `,` `;` 与换行 —— **不认 `|`**，因为 `|` 被保留用于
    合并同一品牌的多种拼写（如 `La-Z-Boy|Lazy Boy|La Z Boy`）。
    """
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    if not raw:
        return []
    parts = re.split(r"[,;\n]", str(raw))
    return [p.strip() for p in parts if p.strip()]


def detect_brands(text: str, brands: list[str]) -> list[str]:
    """在文本中检测品牌（词边界匹配，避免 Monte 命中 Monterey）。"""
    if not text or not brands:
        return []
    low = text.lower()
    hits = []
    for b in brands:
        key = b.strip().lower()
        if not key:
            continue
        if re.fullmatch(r"[a-z0-9][a-z0-9 .'&-]*", key):
            if re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", low):
                hits.append(b)
        elif key in low:
            hits.append(b)
    return hits


def merge_brand_aliases(brands: list[str]) -> dict[str, str]:
    """把 `A|B|C` 形式的别名组合并为「规范名 -> 命中词列表」。

    例如 "La-Z-Boy|Lazy Boy|La Z Boy" → {"La-Z-Boy": ["La-Z-Boy","Lazy Boy","La Z Boy"]}
    """
    canon: dict[str, list[str]] = {}
    for b in brands:
        if "|" in b:
            aliases = [x.strip() for x in b.split("|") if x.strip()]
            if aliases:
                canon[aliases[0]] = aliases
        else:
            canon[b.strip()] = [b.strip()]
    return canon


# ---------------------------------------------------------------------------
# 适配：电商评论（Amazon / Wayfair）
# ---------------------------------------------------------------------------

def adapt_ecommerce(rows: list[dict], cols: dict[str, str], platform: str,
                    brand_map: dict[str, list[str]] | None = None,
                    category: str = "") -> list[dict]:
    """把电商评论导出适配为讨论单元（1 条评论 = 1 个单元）。"""
    brand_map = brand_map or {}
    units = []
    counter = 0
    for r in rows:
        text = str(r.get(cols.get("text", ""), "") or "").strip()
        product = str(r.get(cols.get("product", ""), "") or "").strip()
        if not text and not product:
            continue
        # 评分清理（"5.0 out of 5 stars" / "5" / "4,5"）
        raw_rating = str(r.get(cols.get("rating", ""), "") or "")
        m = re.search(r"(\d+(?:[.,]\d+)?)", raw_rating)
        rating = None
        if m:
            try:
                rating = float(m.group(1).replace(",", "."))
            except ValueError:
                rating = None
        if rating is not None and rating > 5:
            rating = round(rating / 2, 1)  # 10 分制 → 5 分制

        ann_raw = str(r.get(cols.get("annotations", ""), "") or "")
        ann = [a.strip() for a in re.split(r"[,;|]", ann_raw) if a.strip()]

        sku = str(r.get(cols.get("id", ""), "") or "").strip()
        # 标题取评论文首句（无独立标题字段），正文为其余部分，避免重复计入
        first, rest = _split_headline(text)
        haystack = " ".join([product, text])
        brands = []
        for canon, aliases in brand_map.items():
            for a in aliases:
                if re.search(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])",
                             haystack.lower()) or a.lower() in product.lower():
                    brands.append(canon)
                    break
        counter += 1
        units.append({
            "unit_id": f"ec{counter:05d}",
            "title": first or (product[:80] if product else f"评论 {counter}"),
            "body": rest,
            "comments": [],
            "meta": {
                "platform": platform,
                "product": product,
                "sku": sku,
                "category": str(r.get(cols.get("category", ""), "") or category),
                "rating": rating,
                "date": _norm_date(str(r.get(cols.get("date", ""), "") or "")),
                "url": str(r.get(cols.get("url", ""), "") or ""),
                "author": str(r.get(cols.get("author", ""), "") or ""),
                "brands": sorted(set(brands)),
                "annotations": ann,
                "source_row": counter,
            },
        })
    return units


def _split_headline(text: str) -> tuple[str, str]:
    """把评论正文切成「首句标题 + 剩余正文」。"""
    text = (text or "").strip()
    if not text:
        return "", ""
    m = re.search(r"(.{12,140}?[.!?。！？])\s", text)
    if m:
        head = m.group(1).strip()
        rest = text[m.end():].strip()
        return head, rest
    if len(text) <= 140:
        return text, ""
    return text[:120].rstrip() + "…", text[120:].strip()


def _norm_date(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # 2025-11-27 / 11/27/2025 / 2025/11/27
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s[:10]


# ---------------------------------------------------------------------------
# 适配：Reddit（帖子 + 评论）
# ---------------------------------------------------------------------------

def _split_reddit_files(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """把一份混合文件拆成 (帖子, 评论)。

    Reddit 评论导出常见结构：一行一条记录，带 type=post/comment 字段。
    """
    if not rows:
        return [], []
    keys = {_norm(k) for k in rows[0].keys()}
    if "type" not in keys and "postredditid" not in keys:
        return rows, []
    tcol = next((k for k in rows[0] if _norm(k) == "type"), None)
    if not tcol:
        return rows, []
    posts, comments = [], []
    for r in rows:
        t = str(r.get(tcol, "")).strip().lower()
        if t == "post":
            posts.append(r)
        elif t == "comment":
            comments.append(r)
    return posts, comments


def has_type_column(rows: list[dict]) -> bool:
    """判断是否含 `type` 列（Reddit 混合导出的标志：一行一记录，用 type 区分帖子/评论）。"""
    if not rows:
        return False
    return any(_norm(k) == "type" for k in rows[0].keys())


def looks_like_comments(rows: list[dict]) -> bool:
    """判断一份表是否为**纯评论表**。

    注意：必须先排除「含 type 列的混合导出」—— 混合文件里帖子行同样带有
    `parentId` 字段，若先按 parentId 判定，会把整个混合文件误判成纯评论文件，
    导致帖子行全部丢失、最终解析不出任何讨论单元。
    """
    if not rows:
        return False
    if has_type_column(rows):
        return False
    keys = {_norm(k) for k in rows[0].keys()}
    return bool(keys & {"postredditid", "commentid", "parentcommentid",
                        "linkid", "submissionid"})


def adapt_reddit_multifile(files_rows: list[tuple[str, list[dict]]],
                           comment_limit: int = 20,
                           brand_map: dict[str, list[str]] | None = None) -> list[dict]:
    """支持三种上传形态：

    1. 单文件混合导出（含 `type` 列，type=post/comment）；
    2. 「帖子文件 + 评论文件」分文件上传；
    3. 只有帖子（无评论）。
    """
    posts, comments = [], []
    for _src, rows in files_rows:
        if has_type_column(rows):
            # 混合导出：先按 type 拆分
            p, c = _split_reddit_files(rows)
            posts.extend(p)
            comments.extend(c)
        elif looks_like_comments(rows):
            comments.extend(rows)
        else:
            posts.extend(rows)
    return adapt_reddit(posts, comments, comment_limit=comment_limit, brand_map=brand_map)


def adapt_reddit(posts: list[dict], comments: list[dict],
                 comment_limit: int = 20,
                 brand_map: dict[str, list[str]] | None = None) -> list[dict]:
    """把 Reddit 帖子 + 评论适配为讨论单元（1 帖 + 前 N 条评论 = 1 个单元）。"""
    brand_map = brand_map or {}
    if not posts:
        return []
    pcols = resolve_columns(posts)
    ccols = resolve_columns(comments) if comments else {}

    # 评论按帖子分组
    by_post: dict[str, list[dict]] = {}
    if comments and ccols.get("id"):
        pid_col = None
        for k in comments[0].keys():
            if _norm(k) in ("postredditid", "postid", "parentpostid", "linkid",
                            "submissionid", "threadid"):
                pid_col = k
                break
        if pid_col is None:
            pid_col = ccols.get("id")
        for c in comments:
            pid = str(c.get(pid_col, "") or "").strip()
            by_post.setdefault(pid, []).append(c)
        # 按日期升序，取前 N 条
        for k in by_post:
            by_post[k].sort(key=lambda x: str(x.get(ccols.get("date", ""), "")))

    units = []
    has_title_col = bool(pcols.get("product"))
    for i, p in enumerate(posts):
        pid = str(p.get(pcols.get("id", ""), "") or f"p{i}").strip()
        if has_title_col:
            title = str(p.get(pcols.get("product", ""), "") or "").strip()
            body = str(p.get(pcols.get("text", ""), "") or "").strip()
        else:
            # 该导出格式没有独立标题列：帖子行的文本列承载的就是标题
            # （勿同时写入正文，否则同一句话会被重复计入标注）
            title = str(p.get(pcols.get("text", ""), "") or "").strip()
            body = ""
        if not title and not body:
            continue
        crows = by_post.get(pid, [])[:comment_limit] if comment_limit > 0 else []
        cunits = []
        for j, c in enumerate(crows):
            cbody = str(c.get(ccols.get("text", ""), "") or "").strip()
            if not cbody:
                continue
            cunits.append({
                "comment_id": str(c.get(ccols.get("id", ""), "") or f"{pid}c{j}"),
                "author": str(c.get(ccols.get("author", ""), "") or ""),
                "body": cbody,
            })
        sub = str(p.get(pcols.get("store", ""), "") or "").strip()
        url = str(p.get(pcols.get("url", ""), "") or "").strip()
        haystack = " ".join([title, body] + [c["body"] for c in cunits])
        brands = []
        for canon, aliases in brand_map.items():
            for a in aliases:
                if re.search(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])",
                             haystack.lower()):
                    brands.append(canon)
                    break
        units.append({
            "unit_id": f"rd_{pid}",
            "title": title,
            "body": body,
            "comments": cunits,
            "meta": {
                "platform": "Reddit",
                "subreddit": sub,
                "product": "",
                "sku": pid,
                "category": str(p.get(pcols.get("category", ""), "") or ""),
                "rating": None,
                "date": _norm_date(str(p.get(pcols.get("date", ""), "") or "")),
                "url": url,
                "author": str(p.get(pcols.get("author", ""), "") or ""),
                "brands": sorted(set(brands)),
                "annotations": [],
                "comment_total": len(by_post.get(pid, [])),
            },
        })
    return units


def adapt_auto(rows: list[dict], platform: str, *,
               brand_map: dict[str, list[str]] | None = None,
               comment_limit: int = 20, category: str = "") -> list[dict]:
    """自动识别数据形态并适配。"""
    cols = resolve_columns(rows)
    plat = platform or detect_platform(rows, cols) or "Amazon"
    if plat == "Reddit":
        posts, comments = _split_reddit_files(rows)
        return adapt_reddit(posts, comments, comment_limit=comment_limit,
                            brand_map=brand_map)
    return adapt_ecommerce(rows, cols, plat, brand_map=brand_map, category=category)
