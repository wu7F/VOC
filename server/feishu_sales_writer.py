# -*- coding: utf-8 -*-
"""VOC 专用销量工作表：完整读取，按店铺/日期分区覆盖，失败后可原样重跑。

只写明确指定的 Sheets 工作表 A:D，不排序或移动既有行。服务的所有进程
共用文件锁；目标工作表应专供 VOC，人工并发编辑不具有远端事务保护。
"""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import os
from pathlib import Path
import re
from urllib.parse import quote

from . import feishu
from .sales_lock import try_lock

HEADERS = ["店铺", "SKU", "日期", "销量件数"]
READ_ROWS = 1000
WRITE_ROWS = 1000  # 低于官方每次 5000 行限制。
MAX_ROWS = 500000  # 四列最多 2,000,000 个单元格。


def _text(value, label: str, limit: int = 256) -> str:
    if value is None or isinstance(value, (bool, list, dict)):
        raise feishu.FeishuError("销量表%s必须是非空文本。" % label)
    result = str(value).strip()
    if not result or len(result) > limit or result.startswith("="):
        raise feishu.FeishuError("销量表%s为空、过长或含公式。" % label)
    return result


def normalize_day(value) -> str:
    """兼容 Sheets 日期文本和 Excel/Sheets 日期序号，不使用本机时区。"""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value, "日期", 128)
    try:
        if re.fullmatch(r"\d+(?:\.\d+)?", text):
            serial = Decimal(text)
            # 日期序号从 1899-12-30 起；排除常见的误填年份/毫秒时间戳。
            if serial < 30000 or serial >= 100000:
                raise ValueError
            return (date(1899, 12, 30) + timedelta(days=int(serial))).isoformat()
        iso = text.replace("/", "-")
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", iso):
            y, m, d = map(int, iso.split("-"))
            return date(y, m, d).isoformat()
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, OverflowError, InvalidOperation):
        raise feishu.FeishuError("销量表日期无效，请使用 YYYY-MM-DD。") from None


def normalize_row(row: dict) -> dict:
    if not isinstance(row, dict) or any(h not in row for h in HEADERS):
        raise feishu.FeishuError("销量表必须包含：店铺、SKU、日期、销量件数。")
    raw = row["销量件数"]
    try:
        if isinstance(raw, (bool, dict, list)) or raw is None or not str(raw).strip():
            raise ValueError
        n = Decimal(str(raw).strip())
        if not n.is_finite() or n < 0 or n != n.to_integral_value() or n > 10**12:
            raise ValueError
        qty = int(n)
    except (ValueError, InvalidOperation):
        raise feishu.FeishuError("销量件数必须是非负整数。") from None
    return {"店铺": _text(row["店铺"], "店铺"),
            "SKU": _text(row["SKU"], "SKU").upper(),
            "日期": normalize_day(row["日期"]), "销量件数": qty}


def key_for(row: dict) -> tuple[str, str, str]:
    return (row["店铺"].casefold(), row["SKU"].upper(), row["日期"])


def _blank(row: list) -> bool:
    return all(v is None or v == "" for v in row)


def _pending_path(data_dir, url: str) -> Path:
    info = feishu.parse_feishu_url(url)
    key = "%s/%s" % (info.get("token", ""), info.get("sheet_id", ""))
    return Path(data_dir) / ("feishu_sales_pending_" + sha256(key.encode()).hexdigest()[:20])


def has_pending(data_dir, *, url: str) -> bool:
    return _pending_path(data_dir, url).exists()


def _same_cells(left: list, right: list) -> bool:
    if _blank(left) or _blank(right):
        return _blank(left) and _blank(right)
    if left == HEADERS or right == HEADERS:
        return left == right
    return normalize_row(dict(zip(HEADERS, left))) == normalize_row(dict(zip(HEADERS, right)))


def _target(data_dir, url: str) -> tuple[str, str, str, dict]:
    info = feishu.parse_feishu_url(url)
    # 飞书 wiki 链接没有稳定工作表 ID，不能猜测第一张后再写。
    if info["kind"] != "sheets" or not re.fullmatch(r"[A-Za-z0-9]+", info.get("sheet_id", "")):
        raise feishu.FeishuError("销量表请使用带 sheet=工作表ID 的电子表格链接。")
    cfg = feishu.load_config(data_dir)
    auth = feishu.get_tenant_token(cfg.get("app_id", ""), cfg.get("app_secret", ""))
    spreadsheet, sheet_id = info["token"], info["sheet_id"]
    sheets = feishu._sheets_of(auth, spreadsheet)
    matches = [s for s in sheets if s.get("sheet_id") == sheet_id]
    if len(matches) != 1 or matches[0].get("resource_type", "sheet") != "sheet":
        raise feishu.FeishuError("销量表指定的工作表不存在或不是电子表格。")
    meta = matches[0]
    props = meta.get("grid_properties") or {}
    if (type(props.get("row_count")) is not int or props["row_count"] < 1
            or type(props.get("column_count")) is not int or props["column_count"] < 4
            or props["row_count"] > MAX_ROWS):
        raise feishu.FeishuError("销量表网格信息不完整或超出支持范围。")
    # 不能覆盖合并单元格，否则四个业务字段会错位。
    if meta.get("merges"):
        raise feishu.FeishuError("销量工作表不能包含合并单元格。")
    return auth, spreadsheet, sheet_id, props


def _read(auth: str, spreadsheet: str, sheet_id: str, row_count: int) -> list[list]:
    result: list[list] = []
    revision = None
    for start in range(1, row_count + 1, READ_ROWS):
        end = min(start + READ_ROWS - 1, row_count)
        rng = quote("%s!A%d:D%d" % (sheet_id, start, end), safe="")
        data = feishu._http_json(
            "%s/sheets/v2/spreadsheets/%s/values/%s?valueRenderOption=ToString"
            "&dateTimeRenderOption=FormattedString" % (feishu._OPEN, spreadsheet, rng),
            headers={"Authorization": "Bearer " + auth}).get("data") or {}
        vr = data.get("valueRange")
        if not isinstance(vr, dict) or not isinstance(vr.get("values", []), list):
            raise feishu.FeishuError("销量表读取响应不完整。")
        current_revision = data.get("revision", vr.get("revision"))
        if current_revision is None:
            raise feishu.FeishuError("销量表读取缺少版本号，无法确认完整快照。")
        if revision is not None and current_revision != revision:
            raise feishu.FeishuError("读取时销量表被修改，请重试。")
        revision = current_revision
        values = vr.get("values") or []
        if len(values) > end - start + 1 or any(not isinstance(r, list) or len(r) > 4 for r in values):
            raise feishu.FeishuError("销量表读取范围不匹配。")
        result.extend([list(r) + [""] * (4 - len(r)) for r in values])
        result.extend([["", "", "", ""] for _ in range(end - start + 1 - len(values))])
    while result and _blank(result[-1]):
        result.pop()
    return result


def _read_sheet_unlocked(data_dir, url: str) -> list[list]:
    if has_pending(data_dir, url=url):
        raise feishu.FeishuError("上次销量写表未确认完成，请先重跑易仓写表。")
    auth, spreadsheet, sheet_id, props = _target(data_dir, url)
    return _read(auth, spreadsheet, sheet_id, props["row_count"])


def read_sheet(data_dir, *, url: str, locked: bool = False) -> list[list]:
    """完整读取 A:D；拒绝与同服务的写入交错，不截取默认首批数据。"""
    if locked:
        # VOC 读取器已持同一锁，且会在发布聚合缓存后才释放。
        return _read_sheet_unlocked(data_dir, url)
    with try_lock(data_dir, "feishu-sales-write") as acquired:
        if not acquired:
            raise feishu.FeishuError("销量表正在写入，请稍后重试读取。")
        return _read_sheet_unlocked(data_dir, url)


def _replacement_days(replace_days) -> set[tuple[str, str]]:
    days = set()
    for item in replace_days or []:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise feishu.FeishuError("覆盖日期必须是店铺、日期对。")
        days.add((_text(item[0], "店铺").casefold(), normalize_day(item[1])))
    return days


def _plan(values: list[list], incoming: list[dict], replace_days=None) -> tuple[dict[int, list], int]:
    desired: dict[tuple, dict] = {}
    partitions = _replacement_days(replace_days)
    for raw in incoming:
        row = normalize_row(raw)
        key = key_for(row)
        if key in desired:
            raise feishu.FeishuError("待写销量存在重复的店铺、SKU、日期，请先汇总。")
        desired[key] = row
        partitions.add((key[0], key[2]))
    if values and [str(v).strip() for v in values[0]] != HEADERS:
        raise feishu.FeishuError("销量表第一行必须依次为：店铺、SKU、日期、销量件数。")

    existing: dict[tuple, list[int]] = {}
    blank_rows = deque()
    for number, raw in enumerate(values[1:], 2):
        if _blank(raw):
            blank_rows.append(number)
            continue
        try:
            row = normalize_row(dict(zip(HEADERS, raw)))
        except feishu.FeishuError as e:
            raise feishu.FeishuError("销量表第 %d 行无效：%s" % (number, e)) from None
        key = key_for(row)
        existing.setdefault(key, []).append(number)
    for key, numbers in existing.items():
        if len(numbers) > 1 and (key[0], key[2]) not in partitions:
            raise feishu.FeishuError("销量表有不属于本次覆盖日期的重复行，请先核对。")

    changes = {1: HEADERS} if not values else {}
    next_row = max(len(values) + 1, 2)
    for key, row in desired.items():
        numbers = existing.get(key) or []
        if numbers:
            number = numbers[0]
        elif blank_rows:
            number = blank_rows.popleft()
        else:
            number = next_row
            next_row += 1
        cells = [row[h] for h in HEADERS]
        # 原值可能是数字文本；规范化为数字后也写一次，后续重跑会跳过。
        if number > len(values) or not _same_cells(values[number - 1], cells):
            changes[number] = cells
    for key, numbers in existing.items():
        if (key[0], key[2]) in partitions:
            keep = numbers[:1] if key in desired else []
            for number in numbers:
                if number not in keep:
                    changes[number] = ["", "", "", ""]
    return changes, max(next_row - 1, len(values), 1)


def _batches(changes: dict[int, list]):
    """相邻行合并；空白旧行最后清理，避免先清后写期间丢失原记录。"""
    for clearing in (False, True):
        batch: list[list] = []
        start = previous = 0
        for number in sorted(changes):
            cells = changes[number]
            if _blank(cells) != clearing:
                continue
            if batch and (number != previous + 1 or len(batch) >= WRITE_ROWS):
                yield start, batch
                batch = []
            if not batch:
                start = number
            batch.append(cells)
            previous = number
        if batch:
            yield start, batch


def write_rows(data_dir, rows: list[dict], *, url: str, replace_days=None) -> dict:
    """用输入替换其全部 (店铺, 日期) 分区；其它日期原位保留。

    所有输入、现有行都先校验。HTTP 超时/失败立即抛出；不自动追加重试。
    下次从服务端当前数据重新规划，可修复部分成功和响应丢失，无重复追加。
    空输入只确保表头，不清空历史；显式 replace_days 可清空已撤销的日期。
    """
    with try_lock(data_dir, "feishu-sales-write") as acquired:
        if not acquired:
            raise feishu.FeishuError("销量表已有写入任务，请稍后重试。")
        auth, spreadsheet, sheet_id, props = _target(data_dir, url)
        values = _read(auth, spreadsheet, sheet_id, props["row_count"])
        changes, required = _plan(values, rows, replace_days)
        if required * props["column_count"] > 2000000:
            raise feishu.FeishuError("销量工作表将超出两百万单元格限制。")
        headers = {"Authorization": "Bearer " + auth}
        pending = _pending_path(data_dir, url)
        expected = [list(r) for r in values]
        expected.extend([["", "", "", ""] for _ in range(required - len(expected))])
        for number, cells in changes.items():
            expected[number - 1] = cells
        while expected and _blank(expected[-1]):
            expected.pop()
        if changes:
            from . import feishu_sales
            feishu_sales.invalidate(data_dir, "销量表正在写入")
            pending.parent.mkdir(parents=True, exist_ok=True)
            # 写前持久标记；崩溃/超时后不能把不完整远端表再次启用为分母。
            with pending.open("w", encoding="utf-8") as mark:
                mark.write("pending\n")
                mark.flush()
                os.fsync(mark.fileno())
        current = props["row_count"]
        while current < required:
            count = min(5000, required - current)
            data = feishu._http_json(
                "%s/sheets/v2/spreadsheets/%s/dimension_range" % (feishu._OPEN, spreadsheet),
                method="POST", headers=headers,
                body={"dimension": {"sheetId": sheet_id, "majorDimension": "ROWS", "length": count}}
            ).get("data") or {}
            if data.get("addCount") != count:
                raise feishu.FeishuError("销量表扩容结果不完整，请重跑核对。")
            current += count
        for start, batch in _batches(changes):
            data = feishu._http_json(
                "%s/sheets/v2/spreadsheets/%s/values" % (feishu._OPEN, spreadsheet),
                method="PUT", headers=headers,
                body={"valueRange": {"range": "%s!A%d:D%d" % (sheet_id, start, start + len(batch) - 1),
                                     "values": batch}}
            ).get("data") or {}
            if data.get("updatedRows") != len(batch) or data.get("updatedCells") != len(batch) * 4:
                raise feishu.FeishuError("销量表写入回执不完整，请重跑核对。")
        actual = _read(auth, spreadsheet, sheet_id, current)
        if len(actual) != len(expected) or any(not _same_cells(a, b) for a, b in zip(actual, expected)):
            raise feishu.FeishuError("销量表写后核对不一致，请重跑核对。")
        pending.unlink(missing_ok=True)
        return {"ok": True, "rows": len(rows), "changed_rows": len(changes),
                "replaced_days": len(_replacement_days(replace_days) |
                                     {(key_for(normalize_row(r))[0], normalize_row(r)["日期"]) for r in rows})}
