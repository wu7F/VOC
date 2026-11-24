# -*- coding: utf-8 -*-
"""Pure helper for mapping primary aftersale reason categories to conservative subcategories.

Read-only, no I/O; deterministic literal keyword rules only.
"""

from __future__ import annotations

import re

_UNKNOWN = "其他（未细分）"

_SENT_SPLIT = re.compile(r"[.?!;。！？；\n]+")

_NEG_DAMAGE = re.compile(
    r"(?:\bnot\s+(?:damaged|broken|cracked|scratched|torn|bent|warped|rusted)\b|"
    r"\bno\s+(?:damage|damages|cracks?|holes?|scratches?|tears?|rust)\b|"
    r"\bwithout\s+(?:any\s+)?(?:damage|cracks?|holes?|scratches?|tears?|rust|breakage)\b|"
    r"\b(?:if|whether|should|may|might|could)\s+(?:be\s+)?(?:damaged|cracked|broken|torn)\b|"
    r"(?:是否破损|如有破损|若有破损|如果破损|没有(?:破洞|开裂|破损|损坏)|未破损|无破损|没有损坏))",
    re.IGNORECASE,
)


def _split_clauses(text: str) -> list[str]:
    """Split text into clause chunks to avoid cross-sentence pattern spans."""
    return [c.strip() for c in _SENT_SPLIT.split(text) if c.strip()]


def _any_match(patterns: list[re.Pattern[str]], clauses: list[str]) -> bool:
    """Return True if any pattern matches any clause. Patterns are precompiled explicit regexes."""
    for clause in clauses:
        for pat in patterns:
            if pat.search(clause):
                return True
    return False


def reason_subcategory(category: str, text: str) -> str:
    """Return a single conservative subcategory label for the given primary category.

    Precedence: packaging > fabric hole > crack > scratch > transit > rust > deform.
    This is additive only and never changes the primary category.
    """
    t = (text or "").strip()
    if not t:
        return _UNKNOWN

    clauses = _split_clauses(t)

    # Helper to filter clauses that contain explicit damage negation/question wording.
    def _damage_clauses() -> list[str]:
        return [c for c in clauses if not _NEG_DAMAGE.search(c)]

    # ---------- 破损损坏 ----------
    if category == "破损损坏":
        dc = _damage_clauses()
        if not dc:
            return _UNKNOWN

        packaging = [
            re.compile(r"\b(?:packaging|package|box|carton)\s+(?:was|is|has\s+been|arrived)\s+(?:badly\s+)?(?:damaged|torn|crushed|broken)\b", re.I),
            re.compile(r"\b(?:damaged|torn|crushed|broken)\s+(?:outer\s+)?(?:box|package|carton)\b", re.I),
            re.compile(r"包装(?:破损|损坏|破了|烂了|压坏|挤坏)"),
        ]
        if _any_match(packaging, dc):
            return "包装破损"

        fabric_hole = [
            re.compile(r"\b(?:fabric|cloth|textile|material|面料)\b[\s\S]{0,30}?\b(?:hole|holes|tear|rip|tears|rips)\b", re.I),
            re.compile(r"\b(?:hole|holes|tear|rip|tears|rips)\b[\s\S]{0,30}?\b(?:fabric|cloth|textile|material|面料)\b", re.I),
            re.compile(r"(?:面料|布面)(?:有|出现|存在)?(?:破洞|撕裂|破了|烂了)"),
            re.compile(r"(?:破洞|撕裂)(?:在|出现在)?(?:面料|布面)"),
        ]
        if _any_match(fabric_hole, dc):
            return "面料破洞"

        transit_damage = [
            re.compile(r"\b(?:damaged|broken|crushed|smashed|bent)\s+(?:in|during)\s+(?:transit|shipping|delivery|transport)\b", re.I),
            re.compile(r"\b(?:shipping|transit|delivery|transport)\s+(?:damage|damaged)\b", re.I),
            re.compile(r"\b(?:damaged|broken)\s+by\s+(?:carrier|logistics|shipping)\b", re.I),
            re.compile(r"运输途中(?:受损|损坏|破损|压坏)|物流(?:受损|损坏|破损|压坏)|快递(?:受损|损坏|破损|压坏)"),
        ]

        crack_broken = [
            re.compile(r"\b(?:crack|split|fractur(?:e|ed|es|ing)|break|broke|broken)\b", re.I),
            re.compile(r"\b(?:cracks?|cracked|fractures?|fractured|splits?|splitted|breaking|broke|broken|broke|breakage|断裂|开裂|裂开|碎了|断了)\b", re.I),
            re.compile(r"(?:断裂|开裂|裂开|碎了|断了)"),
        ]
        if _any_match(crack_broken, dc):
            return "断裂开裂"

        scratch_dent = [
            re.compile(r"\b(?:scratch(?:ed|es)?|scuff(?:ed|s)?|dent(?:ed|s)?|ding(?:ed|s)?)\b", re.I),
            re.compile(r"(?:划痕|刮伤|刮花|凹陷|凹痕|磕碰|碰伤)"),
        ]
        if _any_match(scratch_dent, dc):
            return "划痕凹陷"

        if _any_match(transit_damage, dc):
            return "运输受损"

        rust_peel = [
            re.compile(r"\b(?:rust(?:ed|ing|y)?|corro(?:ded|sion)?)\b", re.I),
            re.compile(r"\b(?:peeling|peeled|flaking)\s*(?:paint|coating|finish)\b", re.I),
            re.compile(r"(?:生锈|锈蚀|掉漆|油漆脱落|漆面脱落|脱漆)"),
        ]
        if _any_match(rust_peel, dc):
            return "生锈掉漆"

        deform = [
            re.compile(r"\b(?:deform(?:ed)?|warp(?:ed|ing)?|bent|twisted|misshapen)\b", re.I),
            re.compile(r"(?:变形|弯曲|走形|扭曲)"),
        ]
        if _any_match(deform, dc):
            return "产品变形"

        return _UNKNOWN

    # ---------- 尺寸规格 ----------
    if category == "尺寸规格":
        small_patterns = [
            re.compile(r"\b(?:too\s+(?:small|tight|short|narrow|snug))\b", re.I),
            re.compile(r"(?:偏小|太小|小了|太短|太紧|太窄|尺码小|尺寸小)"),
        ]
        if _any_match(small_patterns, clauses):
            return "尺寸偏小"

        large_patterns = [
            re.compile(r"\b(?:too\s+(?:large|big|loose|long|wide|roomy))\b", re.I),
            re.compile(r"(?:偏大|太大|大了|太长|太松|太宽|尺码大|尺寸大)"),
        ]
        if _any_match(large_patterns, clauses):
            return "尺寸偏大"

        return _UNKNOWN

    # ---------- 缺件漏发 ----------
    if category == "缺件漏发":
        missing_parts = [
            re.compile(r"\b(?:missing|absent|omitted|not\s+included|did\s+not\s+include)\b[\s\S]{0,30}?\b(?:screws?|bolts?|nuts?|washers?|hardware|parts?|components?|accessories|fittings?|tools?|fixings?)\b", re.I),
            re.compile(r"\b(?:screws?|bolts?|nuts?|washers?|hardware|parts?|components?|accessories|fittings?|tools?|fixings?)\b[\s\S]{0,30}?\b(?:missing|absent|omitted|not\s+included)\b", re.I),
            re.compile(r"(?:缺少|缺失|漏发|没有|没给)(?:配件|螺丝|螺栓|螺母|垫片|零件|组件|工具|附件)"),
            re.compile(r"(?:配件|螺丝|螺栓|螺母|垫片|零件|组件|工具|附件)(?:缺少|缺失|漏发)"),
        ]
        if _any_match(missing_parts, clauses):
            return "缺少配件"

        not_received = [
            re.compile(r"\b(?:never\s+received|not\s+received|did\s+not\s+receive|haven'?t\s+received|hasn'?t\s+received|package\s+lost|parcel\s+lost|order\s+lost|missing\s+(?:package|parcel|order|delivery|shipment))\b", re.I),
            re.compile(r"(?:包裹|快递|订单|货物|货品|商品)(?:未收到|没收到|没有收到|丢失|遗失了?)"),
            re.compile(r"(?:未收到|没收到|没有收到)(?:包裹|快递|订单)"),
        ]
        if _any_match(not_received, clauses):
            return "包裹未收到"

        missing_item = [
            re.compile(r"\b(?:missing|absent|omitted|not\s+included|did\s+not\s+include)\b[\s\S]{0,30}?\b(?:item|product|piece|unit|article|goods?)\b", re.I),
            re.compile(r"\b(?:item|product|piece|unit|article|goods?)\b[\s\S]{0,30}?\b(?:missing|absent|omitted|not\s+included)\b", re.I),
            re.compile(r"(?:缺少|缺失|漏发|没有|没给)(?:商品|物品|产品|货品|货物)"),
            re.compile(r"(?:商品|物品|产品|货品|货物)(?:缺少|缺失|漏发)"),
        ]
        if _any_match(missing_item, clauses):
            return "缺少商品"

        return _UNKNOWN

    # ---------- 功能故障 ----------
    if category == "功能故障":
        not_working = [
            re.compile(r"\b(?:not\s+(?:working|functioning|operating)|does\s+not\s+(?:work|function|operate)|doesn'?t\s+(?:work|function|operate)|won'?t\s+(?:work|function|operate)|malfunction(?:s|ing|ed)?|defective|faulty|stopped\s+working|died|dead\s+on\s+arrival)\b", re.I),
            re.compile(r"\b(?:cannot|can'?t|unable\s+to)\s+(?:use|operate|turn\s+on|start|power\s+on)\b", re.I),
            re.compile(r"(?:无法(?:使用|工作|运行|启动|开启|开机)|不工作|失灵|故障|坏了|不能(?:用|工作|运行)|打不开)"),
        ]
        if _any_match(not_working, clauses):
            return "无法工作"

        unstable = [
            re.compile(r"\b(?:unstable|wobbly|shaky|loose|wobbles?|shakes?)\b", re.I),
            re.compile(r"\b(?:not\s+(?:stable|steady|secure)|falls?\s+(?:over|off|apart))\b", re.I),
            re.compile(r"(?:不稳|晃动|摇晃|松动|摇摇晃晃|松动不稳)"),
        ]
        if _any_match(unstable, clauses):
            return "松动不稳"

        open_close = [
            re.compile(r"\b(?:not\s+(?:closing|locking|latching|opening)|doesn'?t\s+(?:close|lock|latch|open)|can'?t\s+(?:close|lock|latch|open)|will\s+not\s+(?:close|lock|latch|open)|stuck\s+(?:open|closed))\b", re.I),
            re.compile(r"(?:无法(?:关闭|锁住|锁上|合上|打开|开启)|关不上|锁不上|合不上|打不开|卡住)"),
        ]
        if _any_match(open_close, clauses):
            return "开合锁定异常"

        return _UNKNOWN

    # ---------- 物流配送 ----------
    if category == "物流配送":
        delay = [
            re.compile(r"\b(?:late|delayed|took\s+(?:too\s+)?long|slow\s+delivery|delayed\s+delivery|arrived\s+late)\b", re.I),
            re.compile(r"(?:配送延迟|延迟|迟到|太慢|很慢|物流慢|快递慢)"),
        ]
        if _any_match(delay, clauses):
            return "配送延迟"

        not_delivered = [
            re.compile(r"\b(?:not\s+(?:delivered|received|arrived)|never\s+(?:delivered|received|arrived)|did\s+not\s+(?:arrive|receive|get)|haven'?t\s+(?:received|gotten))\b", re.I),
            re.compile(r"(?:未送达|没送达|未收到|没收到|没有收到|还没到)"),
        ]
        if _any_match(not_delivered, clauses):
            return "未送达"

        return _UNKNOWN

    # ---------- 安装使用 ----------
    if category == "安装使用":
        manual = [
            re.compile(r"\b(?:missing|absent|omitted|not\s+included|no|without)\b[\s\S]{0,30}?\b(?:manual|instructions?|guide|说明书|指南|教程|手册)\b", re.I),
            re.compile(r"\b(?:manual|instructions?|guide|说明书|指南|教程|手册)\b[\s\S]{0,30}?\b(?:missing|absent|omitted|unclear|confusing|not\s+helpful|useless)\b", re.I),
            re.compile(r"\b(?:unclear|confusing|incomprehensible|illegible)\b[\s\S]{0,30}?\b(?:manual|instructions?|guide|说明书|指南|教程|手册)\b", re.I),
            re.compile(r"(?:说明书|指南|教程|手册)(?:缺失|没有|不清楚|不清晰|看不懂|模糊)"),
            re.compile(r"(?:缺失|没有|不清楚|不清晰|看不懂)(?:说明书|指南|教程|手册)"),
        ]
        if _any_match(manual, clauses):
            return "说明书缺失或不清"

        assembly = [
            re.compile(r"\b(?:cannot|can'?t|unable\s+to|difficult\s+to|struggling\s+to|could\s+not)\b[\s\S]{0,30}?\b(?:assemble|install|put\s+together|set\s+up|assembly|installation)\b", re.I),
            re.compile(r"\b(?:assembly|installation)\b[\s\S]{0,30}?\b(?:difficult|hard|tough|impossible|problematic|confusing)\b", re.I),
            re.compile(r"(?:无法|不能|很难|难以|困难|费劲)(?:安装|组装|装配)"),
            re.compile(r"(?:安装|组装|装配)(?:困难|很难|麻烦|复杂|无法|不能)"),
        ]
        if _any_match(assembly, clauses):
            return "安装困难"

        return _UNKNOWN

    # ---------- 要求退货 ----------
    if category == "要求退货":
        refund = [
            re.compile(r"\b(?:want|request|demand|need|please|asking\s+for|looking\s+for)\b[\s\S]{0,30}?\b(?:refund|money\s+back)\b", re.I),
            re.compile(r"(?:我要|要求|申请|希望|请)(?:退款|退钱|返还货款)"),
        ]
        if _any_match(refund, clauses):
            return "退款诉求"

        return_patterns = [
            re.compile(r"\b(?:want|request|demand|need|please|asking\s+for|looking\s+for)\b[\s\S]{0,30}?\b(?:return|send\s+back)\b", re.I),
            re.compile(r"(?:我要|要求|申请|希望|请)(?:退货|退回|寄回|返还商品)"),
        ]
        if _any_match(return_patterns, clauses):
            return "退货诉求"

        return _UNKNOWN

    # ---------- 要求换货 ----------
    if category == "要求换货":
        exchange = [
            re.compile(r"\b(?:want|request|demand|need|please|asking\s+for|looking\s+for)\b[\s\S]{0,30}?\b(?:exchange|swap)\b", re.I),
            re.compile(r"(?:我要|要求|申请|希望|请)(?:换货|更换|替换|调换)"),
        ]
        if _any_match(exchange, clauses):
            return "换货诉求"

        replacement = [
            re.compile(r"\b(?:want|request|demand|need|please|send)\b[\s\S]{0,30}?\b(?:replacement|new\s+one|another\s+one)\b", re.I),
            re.compile(r"(?:我要|要求|申请|希望|请)(?:补发|补寄|重新发|重发|再发|发个新的)"),
        ]
        if _any_match(replacement, clauses):
            return "补发替换诉求"

        return _UNKNOWN

    # ---------- 其他问题 ----------
    if category == "其他问题":
        other_c = [c for c in clauses if not re.compile(r"(?:no\s+stains|not\s+dirty|no\s+bad\s+smell|no\s+strong\s+odor|not\s+uncomfortable|no\s+color\s+difference|没有污渍|没有异味|没有色差)", re.I).search(c) and not _NEG_DAMAGE.search(c)]
        if not other_c:
            return _UNKNOWN

        color = [
            re.compile(r"\b(?:wrong\s+(?:color|colour|shade)|color\s+is\s+(?:different|wrong|off|not\s+as\s+(?:described|expected|pictured)|darker|lighter))\b", re.I),
            re.compile(r"\b(?:color|colour)\b[\s\S]{0,30}?\b(?:different|wrong|off|not\s+match|doesn'?t\s+match)\b", re.I),
            re.compile(r"(?:颜色|色彩|色差)(?:不对|有色差|不同|不一样|错了|不符|不一致)"),
            re.compile(r"(?:色差|偏色|颜色不准)"),
        ]
        if _any_match(color, other_c):
            return "颜色色差"

        odor = [
            re.compile(r"\b(?:strong\s+(?:odor|smell|scent)|bad\s+(?:odor|smell|scent)|smells?\s+(?:bad|strong|terrible|awful|horrible|foul|chemical|toxic))\b", re.I),
            re.compile(r"\b(?:foul|rancid|pungent|noxious)\s+(?:odor|smell|scent)\b", re.I),
            re.compile(r"(?:异味|臭味|难闻|刺鼻|有刺激性气味|气味大)"),
        ]
        if _any_match(odor, other_c):
            return "异味"

        stain = [
            re.compile(r"\b(?:stain(?:ed|s)?|dirty|filthy|soiled|smudged?)\b", re.I),
            re.compile(r"\b(?:has\s+(?:stains?|dirt))\b", re.I),
            re.compile(r"(?:污渍|脏了|有渍|有污点|污点|弄脏|沾上)"),
        ]
        if _any_match(stain, other_c):
            return "污渍脏污"

        comfort = [
            re.compile(r"\b(?:uncomfortable|not\s+comfortable|too\s+(?:hard|soft|firm|stiff|rough))\b", re.I),
            re.compile(r"(?:不舒适|不舒服|舒适度(?:差|不好)|舒适性差|太硬|太软|硌人|磨脚)"),
        ]
        if _any_match(comfort, other_c):
            return "舒适度"

        fabric_hole_other = [
            re.compile(r"\b(?:fabric|cloth|textile|material|面料)\b[\s\S]{0,30}?\b(?:hole|holes|tear|rip|tears|rips)\b", re.I),
            re.compile(r"\b(?:hole|holes|tear|rip|tears|rips)\b[\s\S]{0,30}?\b(?:fabric|cloth|textile|material|面料)\b", re.I),
            re.compile(r"(?:面料|布面)(?:有|出现|存在)?(?:破洞|撕裂|破了|烂了)"),
        ]
        if _any_match(fabric_hole_other, other_c):
            return "面料破洞"

        wrong_item = [
            re.compile(r"\b(?:wrong\s+(?:item|product|article|goods?|thing)|received\s+(?:wrong|incorrect)\s+(?:item|product))\b", re.I),
            re.compile(r"\b(?:incorrect\s+(?:item|product|article|goods?))\b", re.I),
            re.compile(r"(?:发错商品|寄错商品|错发商品|发错货|发错了|货不对板)"),
        ]
        if _any_match(wrong_item, other_c):
            return "发错商品"

        return _UNKNOWN

    return _UNKNOWN
