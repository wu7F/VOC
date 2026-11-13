"""Amazon Team totals from exact counts and explicitly mapped account identities."""
from __future__ import annotations

from . import shop_filter
from .aftersale_rates import combine_rates

TEAM_MEMBERS = (
    ("Team 1", ("xingshan_US", "KornerHK_US", "southwest_US")),
    ("Team 2", ("Wekas_US", "kaifengzhejie_US")),
    ("Team 3", ("maoyitong_US", "AmazonMJSKY_US_US", "RUIBAO_US_US")),
)
_ACCOUNTS = {shop_filter.norm(a): a for _, members in TEAM_MEMBERS for a in members}


def account_aliases(account_map: dict) -> dict:
    names = {k: {v} for k, v in _ACCOUNTS.items()}
    for name, accounts in account_map.items():
        values = accounts if isinstance(accounts, list) else [accounts]
        names.setdefault(shop_filter.norm(name), set()).update(
            _ACCOUNTS.get(shop_filter.norm(a), str(a).strip()) for a in values if a)
    return names


def canonical_account(name: str, aliases: dict):
    """Never fuzzy-match names or choose the first id of an ambiguous mapping."""
    values = aliases.get(shop_filter.norm(name), set())
    return next(iter(values)) if len(values) == 1 and next(iter(values)) in _ACCOUNTS.values() else None


def resolver(account_map: dict):
    aliases = account_aliases(account_map)
    return lambda name: canonical_account(name, aliases)


def shop_is_allowed(name: str, whitelist: set[str] | None, aliases: dict) -> bool:
    if whitelist is None or shop_filter.norm(name) in whitelist:
        return True
    account = canonical_account(name, aliases)
    return bool(account and any(canonical_account(label, aliases) == account for label in whitelist))


def feishu_source_for_shop(shop: str, month: str, sales: dict, aliases: dict) -> str:
    """Join one proven account/month source; unknown labels retain exact matching."""
    account = canonical_account(shop, aliases)
    if not account:
        return "" if len(aliases.get(shop_filter.norm(shop), set())) > 1 else shop_filter.norm(shop)
    candidates = [source for source in sales.get("accounts", [])
                  if canonical_account(sales.get("shop_names", {}).get(source, source), aliases) == account
                  and month in sales.get("qty", {}).get(source, {})]
    return candidates[0] if len(candidates) == 1 else ""


def feishu_mapping_warning(sales: dict, aliases: dict, event_shops: list[str],
                           whitelist: set[str] | None) -> str:
    """Distinguish mapping ambiguity from ordinary missing dates or quantities."""
    issues = set()
    coverage = {}
    for source in sales.get("accounts", []):
        label = sales.get("shop_names", {}).get(source, source)
        if not shop_is_allowed(label, whitelist, aliases):
            continue
        if len(aliases.get(shop_filter.norm(label), set())) > 1:
            issues.add(label + " 对应多个账号")
        account = canonical_account(label, aliases)
        if account:
            for month in sales.get("qty", {}).get(source, {}):
                coverage.setdefault((account, month), []).append(source)
    issues.update(account + "/" + month + " 有多个销量店名"
                  for (account, month), sources in coverage.items() if len(sources) > 1)
    issues.update(shop + " 对应多个账号" for shop in event_shops
                  if len(aliases.get(shop_filter.norm(shop), set())) > 1)
    return "飞书店铺与账号来源映射不唯一：" + "；".join(sorted(issues)) + "。" if issues else ""


def _groups():
    return [{"team": team, "members": list(members), "records": 0, "aftersale": 0,
             "rate_numerator": 0, "sales_qty": None, "rate": None,
             "rate_basis": "none", "missing_shops": [], "excluded_shops": [],
             "rate_warning": "", "rate_denominators": [], "shops": []}
            for team, members in TEAM_MEMBERS]


def build_teams(aftersale: dict, *, sales: dict, account_map: dict,
                events: list[dict], use_feishu: bool) -> list[dict]:
    """Keep registrations separate from eligible events and complete denominators."""
    if aftersale.get("platform") != "AmazonCs":
        return []
    resolve = resolver(account_map)
    aliases = account_aliases(account_map)
    detail = aftersale["mode"] == "detail"
    basis = "detail" if detail else "feishu_qty" if use_feishu else "eccang_qty"
    complete = detail or aftersale.get("sales", {}).get("complete") is True
    wl = aftersale.get("shop_whitelist") or {}
    allowed = {resolve(n) for n in wl.get("shops", [])} if wl.get("enabled") else None
    excluded = {resolve(i["shop"]) for i in wl.get("excluded_shops", [])}
    registrations = {a: [] for a in _ACCOUNTS.values()}
    unmatched = set()
    for shop in aftersale.get("shops", []):
        account = resolve(shop["shop"])
        if account:
            registrations[account].append(shop)
        else:
            unmatched.add(shop["shop"])
    event_buckets = {a: [] for a in _ACCOUNTS.values()}
    uncertain = set()
    report_months = sorted({e["month"] for e in events if e.get("month")})
    for event in events:
        account = resolve(event["shop"])
        exact_name = shop_filter.norm(event.get("exact_account", ""))
        exact = _ACCOUNTS.get(exact_name)
        # Do not move counts from a label bucket without a validated reconciliation.
        if exact_name and exact != account:
            uncertain.update(a for a in (account, exact) if a)
            unmatched.add(event.get("raw_shop", event["shop"]))
        elif account:
            event_buckets[account].append(event)
        else:
            unmatched.add(event.get("raw_shop", event["shop"]))
            uncertain.update(a for a in aliases.get(shop_filter.norm(event["shop"]), set())
                             if a in _ACCOUNTS.values())
    quantities = {a: [] for a in _ACCOUNTS.values()}
    source_accounts = set(sales.get("accounts") or [])
    for key, months in sales.get("qty", {}).items():
        if key not in source_accounts:
            continue
        name = sales.get("shop_names", {}).get(key, key) if use_feishu else key
        account = resolve(name)
        if account:
            quantities[account].append((name, months))
        else:
            unmatched.add(name)
            uncertain.update(a for a in aliases.get(shop_filter.norm(name), set())
                             if a in _ACCOUNTS.values())
    groups = _groups()
    for group in groups:
        group["rate_basis"] = basis
        issues = []
        for account in group["members"]:
            rows, qrows, evs = registrations[account], quantities[account], event_buckets[account]
            member = {"account": account,
                      "shop_names": sorted({r["shop"] for r in rows} | {q[0] for q in qrows}),
                      "records": sum(r.get("records", 0) for r in rows),
                      "aftersale": sum(r.get("aftersale", 0) for r in rows),
                      "rate_numerator": 0, "sales_qty": None, "rate": None,
                      "rate_denominators": [], "status": "ok", "rate_warning": ""}
            if allowed is not None and (account not in allowed or account in excluded):
                member.update(status="excluded", rate_warning="店铺被当前名单排除，Team 覆盖不完整。")
                group["excluded_shops"].append(account)
            elif detail:
                if not rows:
                    member.update(status="missing", rate_warning="订单明细中未找到该店铺。")
                else:
                    member.update(rate_numerator=member["aftersale"], sales_qty=member["records"])
            elif not complete:
                member.update(status="unavailable", rate_warning="销量分母尚未完整读取或校验。")
            elif not qrows:
                member.update(status="missing", rate_warning="飞书销量表缺少该店铺。" if use_feishu else "销量缓存缺少该账号。")
            else:
                month_sources = {}
                for _, source_months in qrows:
                    for month, qty in source_months.items():
                        month_sources.setdefault(month, []).append(qty)
                required = sorted(month_sources) if use_feishu else report_months
                missing = [m for m in required if m not in month_sources]
                overlapping = [m for m in required if len(month_sources.get(m, [])) > 1]
                # Different confirmed labels may describe separate months. Only
                # one verified source may supply each account/month denominator.
                months = {m: quantities[0] for m, quantities in month_sources.items()
                          if m in required and len(quantities) == 1}
                member["sales_qty"] = sum(months.values())
                member["rate_denominators"] = [
                    {"account": account, "month": month, "qty": qty,
                     **({"source_id": sales.get("source_id", "")} if use_feishu else {})}
                    for month, qty in sorted(months.items())]
                valid = [e for e in evs if e.get("month") and months.get(e["month"], 0) > 0]
                member["rate_numerator"] = len(valid)
                if overlapping:
                    member.update(status="unavailable", rate_warning="同账号同月存在多个销量店名："
                                  + "、".join(overlapping) + "，需确认分区是否重复。")
                elif not required or missing:
                    member.update(status="unavailable", rate_warning="未找到本次售后的可验证月份范围。" if not required else
                                  "销量缺少本次月份：" + "、".join(missing))
                elif len(valid) != len(evs):
                    member.update(status="unavailable", rate_warning="售后记录缺日期或同店同月无正销量，暂不计算 Team 率。")
                elif member["sales_qty"] == 0:
                    member.update(status="zero_sales", rate_warning="该店铺销量为 0。")
            if account in uncertain and member["status"] != "excluded":
                member.update(status="unavailable", rate_warning="店铺名与订单账号不一致或配置映射不唯一，需确认后计算。")
            if member["status"] == "missing":
                group["missing_shops"].append(account)
            if member["status"] not in ("ok", "zero_sales"):
                issues.append(account + "：" + member["rate_warning"])
                member["sales_qty"] = None
            elif member["sales_qty"]:
                member["rate"] = round(member["rate_numerator"] / member["sales_qty"], 4)
            group["shops"].append(member)
            for key in ("records", "aftersale", "rate_numerator"):
                group[key] += member[key]
            group["rate_denominators"].extend(member["rate_denominators"])
        if issues:
            group["rate_warning"] = "；".join(issues)
        else:
            group["sales_qty"] = sum(m["sales_qty"] for m in group["shops"])
            if group["sales_qty"]:
                group["rate"] = round(group["rate_numerator"] / group["sales_qty"], 4)
            else:
                group["rate_warning"] = "组内销量合计为 0，暂不计算售后率。"
        group["unmatched_shops"] = sorted(unmatched)
    return groups


def combine_team_reports(reports: list[dict]) -> list[dict]:
    """Merge exact disjoint snapshots. Old reports keep counts unknown, not zero."""
    reports = [r for r in reports if r.get("platform") == "AmazonCs"]
    if not reports:
        return []
    output = _groups()
    for group in output:
        parts, warnings, unmatched = [], [], set()
        old = False
        members = {a: [] for a in group["members"]}
        for report in reports:
            part = next((g for g in report.get("teams", []) if g.get("team") == group["team"]), None)
            if not part:
                old = True
                continue
            for key in ("records", "aftersale", "rate_numerator"):
                group[key] += part[key]
            for key in ("missing_shops", "excluded_shops"):
                group[key].extend(part[key])
            unmatched.update(part.get("unmatched_shops", []))
            if part.get("sales_qty") is None:
                warnings.append(part.get("rate_warning") or "报告缺少完整 Team 分母。")
            parts.append({"basis": part["rate_basis"], "numerator": part["rate_numerator"],
                          "denominator": part.get("sales_qty"), "denominators": part.get("rate_denominators", [])})
            for member in part["shops"]:
                members[member["account"]].append(member)
        group["unmatched_shops"] = sorted(unmatched)
        for key in ("missing_shops", "excluded_shops"):
            group[key] = sorted(set(group[key]))
        bases = {p["basis"] for p in parts}
        group["rate_basis"] = next(iter(bases)) if len(bases) == 1 else "none"
        combined = None
        if old:
            warnings.insert(0, "历史报告缺少 Team 分子分母明细，请重新分析。")
        elif not warnings:
            if any(p["basis"] not in ("detail", "feishu_qty", "eccang_qty") or
                   (p["basis"] != "detail" and
                    (sum(d["qty"] for d in p["denominators"]) != p["denominator"] or
                     (p["basis"] == "feishu_qty" and any(not d.get("source_id") for d in p["denominators"]))))
                   for p in parts):
                warnings.append("报告的 Team 分母明细不完整，请重新分析。")
            else:
                seen, overlap = set(), False
                for part in parts:
                    for d in part["denominators"]:
                        identity = (d["account"], d["month"])
                        overlap |= identity in seen
                        seen.add(identity)
                combined = combine_rates(parts)
                if overlap:
                    combined["rate_warning"] = "报告包含重叠账号月份，无法确认售后事件是否重复，暂不计算合并率。"
                if combined["rate_warning"]:
                    warnings.append(combined["rate_warning"])
        if not warnings and combined is not None:
            group["sales_qty"] = sum(p["denominator"] for p in parts)
            group["rate"] = combined["rate"]
            group["rate_denominators"] = [d for p in parts for d in p["denominators"]]
        group["rate_warning"] = "；".join(dict.fromkeys(warnings))
        for account, rows in members.items():
            member = {"account": account, "shop_names": sorted({n for r in rows for n in r["shop_names"]}),
                      "records": sum(r["records"] for r in rows), "aftersale": sum(r["aftersale"] for r in rows),
                      "rate_numerator": sum(r["rate_numerator"] for r in rows),
                      "sales_qty": None, "rate": None, "status": "unavailable",
                      "rate_warning": group["rate_warning"], "rate_denominators": []}
            if warnings:
                member["status"] = next((r["status"] for r in rows if r["status"] != "ok"), "unavailable")
            elif rows:
                member["sales_qty"] = sum(r["sales_qty"] for r in rows)
                member["status"] = "ok" if member["sales_qty"] else "zero_sales"
                member["rate"] = round(member["rate_numerator"] / member["sales_qty"], 4) if member["sales_qty"] else None
                member["rate_denominators"] = [d for r in rows for d in r["rate_denominators"]]
            group["shops"].append(member)
        if old:
            for key in ("records", "aftersale", "rate_numerator"):
                group[key] = None
                for member in group["shops"]:
                    member[key] = None
        if not warnings and group["sales_qty"] == 0:
            group["rate_warning"] = "组内销量合计为 0，暂不计算售后率。"
    return output
