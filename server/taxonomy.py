# -*- coding: utf-8 -*-
"""VOC 通用维度词典（离线引擎的知识底座）。

平台内置一套**跨品类通用**的 VOC 维度体系，共 7 个维度：

    purchase_status       购买状态（已购 / 未购 / 未知）—— 互斥
    user_personas         人群画像
    usage_scenarios       使用场景
    pre_purchase_concerns 购买前关注点
    post_purchase_experience 购买后体验
    features              功能体验（产品属性方面）
    unmet_needs           未满足需求

每个标签由若干正则模式定义（大小写不敏感）。模式命中的**原句**会被逐字抽为证据，
因此证据天然可溯源，不存在编造。

词典同时支持英文（主，适配 Amazon/Wayfair/Reddit 英文语料）与中文（辅）。
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# 情感词典（用于判定证据句的情感倾向）
# ---------------------------------------------------------------------------

POSITIVE_WORDS = [
    "love", "loves", "loved", "great", "excellent", "amazing", "awesome",
    "perfect", "comfortable", "comfy", "sturdy", "solid", "durable", "quiet",
    "beautiful", "gorgeous", "happy", "satisfied", "recommend", "worth",
    "easy", "smooth", "helpful", "responsive", "wonderful", "fantastic",
    "best", "good", "nice", "well made", "well-made", "high quality",
    "high-quality", "favorite", "pleased", "impressed", "works great",
    "no issues", "no complaints", "would buy again", " Worth ",
    "喜欢", "满意", "很好", "不错", "舒服", "结实", "安静", "推荐", "值",
]

NEGATIVE_WORDS = [
    "hate", "hated", "bad", "terrible", "awful", "horrible", "poor", "cheap",
    "cheaply", "flimsy", "broke", "broken", "defective", "wobble", "wobbles",
    "wobbly", "squeak", "squeaks", "squeaky", "noisy", "uncomfortable",
    "hurts", "hurt", "painful", "stiff", "smell", "smells", "smelly", "odor",
    "stink", "stinks", "return", "returned", "refund", "disappointed",
    "disappointing", "frustrating", "annoying", "difficult", "hard to",
    "fell apart", "falling apart", "peeling", "scratched", "damaged",
    "missing", "wrong", "delay", "delayed", "late", "waste of money",
    "not worth", "regret", "problem", "problems", "issue", "issues",
    "complaint", "complaints", "overpriced", "unsafe", "dangerous",
    "差", "糟糕", "不好", "退货", "失望", "难", "噪音", "气味", "晃动", "坏",
]

NEGATORS = [
    "not", "no", "never", "isn't", "aren't", "wasn't", "weren't", "don't",
    "doesn't", "didn't", "can't", "cannot", "won't", "wouldn't", "hardly",
    "barely", "without", "n't",
]

# 购买状态模式（互斥判定，purchased 优先）
PURCHASED_PATTERNS = [
    r"\bi\s+(bought|purchased|ordered|got|received|own|have)\b",
    r"\bwe\s+(bought|purchased|ordered|got|received|own|have)\b",
    r"\b(mine|my own)\b",
    r"\b(i've|i have)\s+(had|owned|been using|used)\b",
    r"\b(after|for)\s+\d+\s+(months?|years?|weeks?)\b",
    r"\bpulled the trigger\b",
    r"\barrived\b.*\b(assembled|set up|put together)\b",
    r"我(买|买了|已购|用了|入手|下单)",
]

NOT_PURCHASED_PATTERNS = [
    r"\b(looking for|in the market for|shopping for)\b",
    r"\bshould i\b",
    r"\b(any|anyone)\s+(recommend|recs?|suggestions?)\b",
    r"\brecommend(ations?)?\b",
    r"\b(still|trying to)\s+(deciding|decide|choose|choosing)\b",
    r"\bwhich one\b",
    r"\bworth it\b",
    r"\bconsidering\b",
    r"\bthinking (about|of)\s+(buying|getting)\b",
    r"\bhelp me (choose|pick|decide)\b",
    r"\banyone (else )?have\b",
    r"求推荐|想买|纠结|该不该买|哪个好|值得吗",
]


def _tag(key: str, label_cn: str, label_en: str, patterns: list[str],
         desc: str = "", negative_hint: bool = False) -> dict:
    return {
        "key": key,
        "label_cn": label_cn,
        "label_en": label_en,
        "patterns": patterns,
        "desc": desc,
        "negative_hint": negative_hint,
    }


# ---------------------------------------------------------------------------
# 维度定义
# ---------------------------------------------------------------------------

PERSONAS = [
    _tag("new_parent", "新手父母", "New parent", [
        r"\b(first[- ]time|new|expecting)\s+(mom|mum|mother|dad|father|parent)\b",
        r"\b(first[- ]time\s+)?(mom|dad)s?\b",
        r"\bnewborn\b", r"\bexpecting\b", r"\bpregnan\w*\b",
        r"\bbaby is (due|coming)\b", r"\bfirst baby\b",
    ], "首次成为父母或即将迎来新生儿"),
    _tag("postpartum_recovery", "产后恢复期", "Postpartum recovery", [
        r"\bc[- ]?section\b", r"\bcesarean\b", r"\bpostpartum\b",
        r"\brecovery\b.*\b(after|from)\b.*\b(birth|delivery|baby)\b",
        r"\b(still )?healing\b", r"\bstitches\b",
    ], "剖腹产 / 产后恢复期的身体限制"),
    _tag("breastfeeding", "母乳喂养父母", "Breastfeeding parent", [
        r"\bbreastfeed\w*\b", r"\bnursing\b.*\b(mom|mother|baby)\b",
        r"\bnurse (the )?baby\b", r"\bpumping\b", r"\blatching\b",
    ], "以母乳喂养为主的照护者"),
    _tag("multi_child", "多孩家庭", "Multi-child family", [
        r"\bsecond (baby|child|kid)\b", r"\bmy (two|2|three|3) (kids|children)\b",
        r"\bolder (kid|child|one)\b", r"\bhand[ -]?me[ -]?down\b",
        r"\btoddler\b.*\bbaby\b",
    ], "有多个孩子、关注复用与耐用"),
    _tag("small_space", "小户型/公寓", "Small space dweller", [
        r"\bsmall (apartment|space|room|house|studio)\b", r"\bstudio\b",
        r"\bcondo\b", r"\btight space\b", r"\bno space\b",
        r"\bnursery is (small|tiny)\b", r"\bsquare feet\b",
    ], "居住空间有限，关注尺寸与收纳"),
    _tag("tall_user", "高个用户", "Tall user", [
        r"\bi'?m \d'?\s?\d{1,2}\b", r"\b(6|7)\s?('|\s?foot|\s?ft)\b",
        r"\bi'?m tall\b", r"\btall\b.*\b(husband|partner|fit)\b",
    ], "身高较高，关注头枕/腿托适配"),
    _tag("short_user", "娇小用户", "Petite user", [
        r"\bi'?m 5'?\s?[0-3]\b", r"\bi'?m short\b", r"\bpetite\b",
        r"\b5'[0-3]\b", r"\b(short|small)\b.*\bframe\b",
    ], "身高较矮，关注脚能否落地"),
    _tag("plus_size_user", "大体型用户", "Plus-size user", [
        r"\bplus[- ]?size\b", r"\b\d{3}\s?(lb|lbs|pounds)\b",
        r"\bheavier\b", r"\bcurvy\b", r"\bbigger (person|build|guy)\b",
        r"\bweight limit\b",
    ], "体重或体型较大，关注承重与宽度"),
    _tag("budget_conscious", "价格敏感型", "Budget conscious", [
        r"\bbudget\b", r"\bcheap\b", r"\baffordable\b", r"\bunder \$?\d+\b",
        r"\bsave money\b", r"\bprice range\b", r"\bcost effective\b",
    ], "以价格为主要决策约束"),
    _tag("research_heavy", "重度研究者", "Heavy researcher", [
        r"\bi'?ve been (researching|reading|looking)\b",
        r"\bread (all the )?reviews\b", r"\bcompared\b.*\b(models?|options?)\b",
        r"\bwent down a rabbit hole\b", r"\bspreadsheet\b",
    ], "决策前大量比较与资料检索"),
]


SCENARIOS = [
    _tag("night_feeding", "夜间哺乳", "Night feeding", [
        r"\bnight[- ]?(feed|nurs|nursing)\w*\b", r"\b(3|2|4)\s?am\b",
        r"\bmiddle of the night\b", r"\bnighttime\b.*\b(feed|nurse)\b",
        r"\bup all night\b",
    ], "半夜起身喂养的场景"),
    _tag("nap_sleeping", "哄睡/小睡", "Nap & soothing", [
        r"\bnap(s|ping)?\b", r"\bslept in (it|this)\b", r"\bfall asleep\b",
        r"\brock(ing)? (the )?(baby|him|her) to sleep\b", r"\bsoothe\b",
    ], "哄睡与在椅子上小睡"),
    _tag("feeding_session", "日常喂养", "Feeding session", [
        r"\bfeed(ing)?\b.*\b(baby|bottle)\b", r"\bbottle[- ]?feed\w*\b",
        r"\bformula\b",
    ], "日常喂奶/喂养场景"),
    _tag("leisure_reading", "休闲阅读/观影", "Leisure & TV", [
        r"\bread(ing)?\b", r"\bwatch(ing)? (tv|movies)\b", r"\bknit\w*\b",
        r"\bcrochet\w*\b", r"\bvideo games?\b", r"\blounge\b",
    ], "阅读、观影等休闲久坐"),
    _tag("living_room_use", "客厅使用", "Living room use", [
        r"\bliving room\b", r"\bden\b", r"\bmain room\b",
    ], "放置在客厅/公共区域"),
    _tag("nursery_use", "婴儿房使用", "Nursery use", [
        r"\bnursery\b", r"\bgender neutral\b.*\broom\b",
    ], "放置在婴儿房"),
    _tag("long_sitting", "长时间久坐", "Long sitting", [
        r"\b(hours?|all day|all night)\b.*\b(sit|sitting|in it)\b",
        r"\bsit(ting)? for hours\b", r"\blong (periods|sessions)\b",
    ], "单次长时间连续使用"),
    _tag("small_space_use", "小空间场景", "Tight space use", [
        r"\bcorner\b", r"\bagainst the wall\b", r"\bsmall room\b",
        r"\bfootprint\b",
    ], "需要贴合墙角或受限空间"),
]


CONCERNS = [
    _tag("size_fit_concern", "尺寸与适配顾虑", "Size & fit concern", [
        r"\bwill it fit\b", r"\bsize\b.*\b(concern|worried|issue)\b",
        r"\bdimension", r"\btoo (big|tall|wide|small)\b",
        r"\bmeasure", r"\bfit through the door\b",
    ], "担心尺寸不合、放不下或进出困难"),
    _tag("try_before_buy", "想先试坐", "Try before buy", [
        r"\btry (it )?(before|in store)\b", r"\bshowroom\b",
        r"\bfloor model\b", r"\bsat in it at\b", r"\bin store\b",
    ], "希望线下体验后再决定"),
    _tag("price_value_concern", "价格与性价比", "Price & value", [
        r"\bworth (it|the)\b", r"\bexpensive\b", r"\bprice\b",
        r"\bvalue for (the )?money\b", r"\boverpriced\b", r"\bfor the price\b",
    ], "权衡价格与价值"),
    _tag("durability_concern", "耐用性顾虑", "Durability concern", [
        r"\bwill (it )?last\b", r"\bhold up\b", r"\bdurab\w+\b",
        r"\bhow long.*last\b", r"\blongevity\b",
    ], "担心用不久、易损耗"),
    _tag("assembly_concern", "安装难度顾虑", "Assembly concern", [
        r"\bassembl\w+\b", r"\bput (it )?together\b", r"\binstructions\b",
        r"\bhardware\b",
    ], "关注自行安装的难度"),
    _tag("fabric_color_concern", "面料与颜色", "Fabric & color", [
        r"\bfabric\b", r"\bupholster\w*\b", r"\bswatch\w*\b",
        r"\bcolor\b.*\b(match|off|different)\b", r"\bmatching decor\b",
    ], "担心实物颜色/面料与图片不符"),
    _tag("delivery_concern", "物流与到货", "Delivery concern", [
        r"\bdeliver(y|ed)?\b", r"\bship(ping|ped)?\b", r"\barrive\b",
        r"\bwait time\b", r"\bback[- ]?order\b",
    ], "关注发货时效与物流体验"),
    _tag("safety_concern", "安全合规顾虑", "Safety concern", [
        r"\bsafe\w*\b", r"\bcertif\w+\b", r"\btoxic\b", r"\bchemicals?\b",
        r"\bflammab\w+\b", r"\bcpsc\b",
    ], "担心材质安全与认证"),
]


EXPERIENCES = [
    _tag("comfort_experience", "舒适度体验", "Comfort", [
        r"\bcomfort\w*\b", r"\bcomfy\b", r"\bcosy\b", r"\bcozy\b",
        r"\bsoft\b", r"\bcushion\w*\b", r"\bpadd\w+\b",
    ], "坐感与支撑的实际感受"),
    _tag("assembly_experience", "安装体验", "Assembly", [
        r"\bassembl\w+\b", r"\bput (it )?together\b", r"\bset up\b",
        r"\binstructions\b", r"\bhardware\b", r"\bdrill\b",
    ], "开箱组装的过程体验"),
    _tag("quality_experience", "做工与质量", "Build quality", [
        r"\bquality\b", r"\bwell (made|built)\b", r"\bcheap(ly)? (made|built)\b",
        r"\bsturd\w+\b", r"\bsolid\b", r"\bflimsy\b", r"\bwobbl\w+\b",
        r"\bdurab\w+\b",
    ], "用料与结构的实际感受"),
    _tag("odor_experience", "气味问题", "Odor", [
        r"\bsmell\w*\b", r"\bodou?r\b", r"\bstink\w*\b", r"\bchemical smell\b",
        r"\boff[ -]?gas\w*\b", r"\bventilat\w+\b",
    ], "新品的异味及其消散过程"),
    _tag("noise_experience", "噪音与晃动", "Noise & wobble", [
        r"\bsqueak\w*\b", r"\bcreak\w*\b", r"\bnois\w+\b", r"\brattle?\w*\b",
        r"\bwobbl\w+\b",
    ], "使用中的异响与晃动"),
    _tag("mechanism_experience", "后仰与机械结构", "Recline mechanism", [
        r"\breclin\w+\b", r"\bmechanism\b", r"\blever\b", r"\bfootrest\b",
        r"\bglid\w+\b", r"\bswivel\w*\b", r"\brock(ing|s)?\b",
    ], "后仰、滑行、旋转等机构体验"),
    _tag("size_fit_experience", "尺寸适配结果", "Fit outcome", [
        r"\bfit (me|us|perfectly|well)\b", r"\bfits\b.*\b(room|space|corner)\b",
        r"\btoo (tall|short|big|small|wide)\b", r"\bfoot\b.*\b(floor|ground)\b",
        r"\bhead\b.*\b(rest|clearance)\b",
    ], "实际使用中的合身度与摆放结果"),
    _tag("delivery_experience", "物流与包装", "Delivery & packaging", [
        r"\bpackag\w+\b", r"\bbox\b.*\b(damage|arrive)\b", r"\bdeliver\w*\b",
        r"\bship\w*\b", r"\bdamaged\b", r"\bmissing (parts|pieces|hardware)\b",
    ], "到货、包装与完好度"),
    _tag("service_experience", "客服与售后", "Service & support", [
        r"\bcustomer (service|support)\b", r"\bwarranty\b", r"\breturn\w*\b",
        r"\brefund\w*\b", r"\bexchang\w+\b", r"\bcommunicat\w+\b",
        r"\bresolut\w+\b",
    ], "售后沟通与保修退换"),
    _tag("price_value_experience", "价格感受", "Price perception", [
        r"\bworth (it|the)\b", r"\bfor the price\b", r"\bvalue\b",
        r"\boverpriced\b", r"\bbargain\b", r"\bspent\b",
    ], "成交价与实际获得感的对比"),
]


# 功能体验（产品属性方面）—— 平台与报告共用
FEATURES = [
    _tag("comfort_padding", "坐感与填充", "Comfort & padding", [
        r"\bcomfort\w*\b", r"\bcomfy\b", r"\bcushion\w*\b", r"\bpad(ding)?\b",
        r"\bplush\b", r"\bsoft\b", r"\bfirm\b",
    ]),
    _tag("lumbar_head_support", "腰背与头颈支撑", "Lumbar & head support", [
        r"\blumbar\b", r"\bheadrest\b", r"\bhead rest\b", r"\bneck support\b",
        r"\bpillow\b.*\b(back|lumbar|head)\b", r"\bback support\b",
    ]),
    _tag("recline_function", "后仰功能", "Recline function", [
        r"\breclin\w+\b", r"\bfootrest\b", r"\bpush back\b", r"\blever\b",
        r"\bmanual (recline|push)\b", r"\belectric (recline|button)\b",
        r"\bpower reclin\w+\b",
    ]),
    _tag("glide_rock_swivel", "滑行/摇动/旋转", "Glide, rock & swivel", [
        r"\bglid\w+\b", r"\brock(er|ing|s)?\b", r"\bswivel\w*\b",
        r"\b360\b",
    ]),
    _tag("size_dimensions", "尺寸与占用空间", "Size & footprint", [
        r"\bsize\b", r"\bdimension\w*\b", r"\bfootprint\b",
        r"\bwide|width\b", r"\bfits?\b.*\b(room|corner|space)\b",
        r"\bsmall space\b",
    ]),
    _tag("build_durability", "结构与耐用性", "Build & durability", [
        r"\bdurab\w+\b", r"\bsturdy\b", r"\bsolid\b", r"\bflimsy\b",
        r"\bwobbl\w+\b", r"\bhold up\b", r"\bwood(en)?\b", r"\bframe\b",
    ]),
    _tag("assembly_ease", "安装便捷性", "Assembly", [
        r"\bassembl\w+\b", r"\bput (it )?together\b", r"\binstruction\w*\b",
        r"\bset ?up\b", r"\bhardware\b", r"\bdrill\b",
    ]),
    _tag("material_fabric", "面料与材质", "Material & fabric", [
        r"\bfabric\b", r"\bupholster\w*\b", r"\bleather\b", r"\bcorduroy\b",
        r"\bvelvet\b", r"\btexture\b", r"\bstitch\w*\b", r"\bmaterial\b",
    ]),
    _tag("appearance_color", "外观与颜色", "Appearance & color", [
        r"\bcolor\b", r"\bcolou?r\w*\b", r"\bappearance\b", r"\blook\w* (great|nice|good)\b",
        r"\bbrightness\b", r"\bdecor\b", r"\bstyle\b",
    ]),
    _tag("price_value", "价格与性价比", "Price & value", [
        r"\bprice\b", r"\bworth (it|the)\b", r"\bvalue\b", r"\bcost\b",
        r"\bexpensive\b", r"\bcheap\b", r"\bbudget\b", r"\$\d+",
    ]),
    _tag("delivery_packaging", "物流与包装", "Delivery & packaging", [
        r"\bdeliver\w*\b", r"\bship\w*\b", r"\bpackag\w+\b", r"\bbox\b",
        r"\bdamage\w*\b", r"\bmissing (parts|pieces|hardware)\b", r"\barrive\w*\b",
    ]),
    _tag("odor_smell", "气味", "Odor", [
        r"\bsmell\w*\b", r"\bodou?r\b", r"\bstink\w*\b", r"\bchemical\b",
        r"\boff[ -]?gas\w*\b",
    ]),
    _tag("noise_squeak", "异响与噪音", "Noise & squeak", [
        r"\bsqueak\w*\b", r"\bcreak\w*\b", r"\bnois\w+\b", r"\brattle?\w*\b",
        r"\bquiet\b",
    ]),
    _tag("ease_of_cleaning", "清洁便利性", "Ease of cleaning", [
        r"\bclean\w*\b", r"\bwash\w*\b", r"\bspill\w*\b", r"\bstain\w*\b",
        r"\bvacuum\w*\b", r"\bwipe\w*\b",
    ]),
    _tag("weight_mobility", "重量与移动", "Weight & mobility", [
        r"\bweigh\w*\b", r"\bheavy\b", r"\blight\w*\b", r"\bmov(e|ing) it\b",
        r"\bcarry\b",
    ]),
    _tag("service_warranty", "客服与保修", "Service & warranty", [
        r"\bcustomer (service|support)\b", r"\bwarrant\w+\b", r"\breturn\w*\b",
        r"\brefund\w*\b", r"\bexchang\w+\b", r"\bcommunicat\w+\b",
    ]),
    _tag("safety_certification", "安全与认证", "Safety & certification", [
        r"\bcertif\w+\b", r"\bsafety\b", r"\btest\w*\b.*\b(standard|compliance)\b",
        r"\bcpsc\b", r"\bcarb\b", r"\bnon[- ]?toxic\b",
    ]),
]


UNMET_NEEDS = [
    _tag("usb_charging", "需要充电/USB 接口", "USB charging", [
        r"\busb\b", r"\bcharging (port|station)\b", r"\bphone charger\b",
        r"\bpower outlet\b", r"\bplug\b.*\b(near|reach)\b",
    ]),
    _tag("washable_cover", "需要可拆洗椅套", "Washable cover", [
        r"\bremovable cover\b", r"\bmachine washable\b", r"\bwashable\b",
        r"\bslipcover\b", r"\bcover\b.*\b(wash|remove|clean)\b",
    ]),
    _tag("adjustable_support", "需要可调腰托/头枕", "Adjustable support", [
        r"\badjustable (lumbar|head|pillow)\b", r"\bwish\b.*\b(adjust|lumbar|headrest)\b",
        r"\bwould be nice\b.*\b(adjust)\b", r"\bno lumbar\b", r"\blocking\b",
    ]),
    _tag("quieter_mechanism", "需要更静音的机构", "Quieter mechanism", [
        r"\bwish\b.*\b(quiet|silent)\b", r"\bso (loud|noisy)\b",
        r"\bneed\b.*\bquieter\b", r"\bgrease\b",
    ]),
    _tag("better_instructions", "需要更清晰的说明书", "Better instructions", [
        r"\binstruction\w*\b.*\b(poor|unclear|confusing|bad)\b",
        r"\bno instructions\b", r"\bvideo (guide|tutorial)\b",
        r"\bwish\b.*\binstruction\w*\b",
    ]),
    _tag("wider_seat", "需要更宽/更高的座位", "Wider or taller seat", [
        r"\bwish\b.*\b(wider|bigger|taller|larger)\b", r"\btoo narrow\b",
        r"\bwider seat\b", r"\bmore room\b",
    ]),
    _tag("cup_holder", "需要杯架等便利件", "Cup holder & extras", [
        r"\bcup holder\b", r"\bdrink holder\b", r"\bside pocket\b",
        r"\bstorage pocket\b",
    ]),
    _tag("higher_weight_capacity", "需要更高承重", "Higher weight capacity", [
        r"\bweight (limit|capacity)\b", r"\bhold\b.*\b\d{3}\s?(lb|lbs)\b",
        r"\bmax(imum)? weight\b",
    ]),
    _tag("longer_warranty", "需要更长保修", "Longer warranty", [
        r"\bwarrant\w+\b.*\b(short|only|too)\b", r"\bwish\b.*\bwarrant\w+\b",
        r"\bextended warranty\b",
    ]),
]


DIMENSIONS: dict[str, dict] = {
    "purchase_status": {
        "label_cn": "购买状态",
        "label_en": "Purchase status",
        "kind": "mutex",
        "tags": {
            "purchased": {
                "key": "purchased", "label_cn": "已购买人群", "label_en": "Purchased",
                "patterns": PURCHASED_PATTERNS,
                "desc": "单元中出现明确的第一人称已购买/已持有表述",
            },
            "not_purchased": {
                "key": "not_purchased", "label_cn": "未购买人群", "label_en": "Not purchased",
                "patterns": NOT_PURCHASED_PATTERNS,
                "desc": "单元处于选购/调研阶段，尚未购买",
            },
        },
    },
    "user_personas": {"label_cn": "人群画像", "label_en": "User persona", "tags": PERSONAS},
    "usage_scenarios": {"label_cn": "使用场景", "label_en": "Usage scenario", "tags": SCENARIOS},
    "pre_purchase_concerns": {"label_cn": "购买前关注点", "label_en": "Pre-purchase concern", "tags": CONCERNS},
    "post_purchase_experience": {"label_cn": "购买后体验", "label_en": "Post-purchase experience", "tags": EXPERIENCES},
    "features": {"label_cn": "功能体验", "label_en": "Feature & experience", "tags": FEATURES},
    "unmet_needs": {"label_cn": "未满足需求", "label_en": "Unmet need", "tags": UNMET_NEEDS},
}

# 体验类维度（证据多含情感评价）
EXPERIENCE_DIMS = {"post_purchase_experience", "features", "unmet_needs"}


def _as_tag_dict(tags) -> dict:
    """把标签列表统一转为 `tag_key -> tag_def` 的字典形式。"""
    if isinstance(tags, dict):
        return tags
    return {t["key"]: t for t in tags}


# 归一化：所有维度的 tags 统一为字典，便于按 key 索引
for _d, _s in DIMENSIONS.items():
    _s["tags"] = _as_tag_dict(_s["tags"])


# ---------------------------------------------------------------------------
# 电商平台自带标注体系 → 本平台功能标签 的映射
# Amazon / Wayfair 的评论导出常带 Annotations 列，是平台侧的高质量打标结果，
# 直接映射可显著提升 features 维度准确度（优于纯词典）。
# ---------------------------------------------------------------------------

ANNOTATION_MAP = {
    "material - comfort": "comfort_padding",
    "material - texture": "material_fabric",
    "material": "material_fabric",
    "quality/price": "price_value",
    "quality/price - price/quality relation": "price_value",
    "quality/price - stability/flimsiness": "build_durability",
    "assembly - difficulty": "assembly_ease",
    "assembly - instructions": "assembly_ease",
    "assembly": "assembly_ease",
    "usability": "ease_of_cleaning",
    "usability - features": "recline_function",
    "size": "size_dimensions",
    "size - physical fit": "size_dimensions",
    "color": "appearance_color",
    "color - brightness/tone": "appearance_color",
    "color - matching decor": "appearance_color",
    "time - delay (delivery date vs. scheduled date)": "delivery_packaging",
    "carrier/da - product handling": "delivery_packaging",
    "defect/damage - damage": "build_durability",
    "missing parts": "delivery_packaging",
    "communication": "service_warranty",
    "communication - problem resolution": "service_warranty",
}

# 平台标注中「负面倾向明显」的条目（用于情感先验）
ANNOTATION_NEGATIVE = {
    "assembly - difficulty", "quality/price - stability/flimsiness",
    "defect/damage - damage", "missing parts", "time - delay (delivery date vs. scheduled date)",
    "carrier/da - product handling",
}


def all_tags() -> dict[str, dict]:
    """展平 `tag_key -> {dim, tag_def}`。"""
    flat = {}
    for dim, spec in DIMENSIONS.items():
        for key, t in spec["tags"].items():
            flat[key] = {"dim": dim, **t}
    return flat


# ---------------------------------------------------------------------------
# TagIndex：让聚合层与「标签从哪来」解耦
#
# 离线引擎用内置 taxonomy；LLM 引擎会为每个品类现生成标签集（标签键不同）。
# 聚合层只依赖这一层抽象，因此两种引擎共用同一套聚合与前端契约。
# ---------------------------------------------------------------------------

PURCHASE_KEYS = ("purchased", "not_purchased")


class TagIndex:
    """标签索引：`tag_key -> {dim, label_cn, label_en, desc}` + 维度中文名。"""

    def __init__(self, tags: dict[str, dict], dim_labels: dict[str, str]):
        self.tags = tags
        self.dim_labels = dim_labels

    def get(self, key: str) -> dict | None:
        return self.tags.get(key)

    def dim_of(self, key: str) -> str:
        t = self.tags.get(key)
        return t["dim"] if t else ""

    def label(self, key: str) -> str:
        t = self.tags.get(key)
        return t["label_cn"] if t else key

    def dim_label(self, dim: str) -> str:
        return self.dim_labels.get(dim, dim)

    def keys_of(self, dim: str) -> list[str]:
        return [k for k, t in self.tags.items() if t["dim"] == dim]


def index_from_taxonomy() -> TagIndex:
    """内置词典引擎的标签索引。"""
    tags = {}
    for key, t in all_tags().items():
        tags[key] = {
            "dim": t["dim"],
            "label_cn": t["label_cn"],
            "label_en": t.get("label_en", ""),
            "desc": t.get("desc", ""),
        }
    dim_labels = {d: s["label_cn"] for d, s in DIMENSIONS.items()}
    return TagIndex(tags, dim_labels)


def index_from_tagset(tagset: dict) -> TagIndex:
    """从「标签集 JSON」（离线或 LLM 产出）构建标签索引。

    结构约定：
        {"dimensions": {"<dim>": {"label_cn": ..., "tags": {"<key>": {"label_cn": ...}}}}}
    """
    tags: dict[str, dict] = {}
    dim_labels: dict[str, str] = {}
    for dim, spec in (tagset.get("dimensions") or {}).items():
        dim_labels[dim] = spec.get("label_cn") or DIMENSIONS.get(dim, {}).get("label_cn", dim)
        for key, t in (spec.get("tags") or {}).items():
            tags[key] = {
                "dim": dim,
                "label_cn": t.get("label_cn") or key,
                "label_en": t.get("label_en", ""),
                "desc": t.get("thoughts") or t.get("desc", ""),
            }
    # 兜底：标签集若缺某个标准维度，用内置词典补齐维度名
    for d, s in DIMENSIONS.items():
        dim_labels.setdefault(d, s["label_cn"])
    return TagIndex(tags, dim_labels)
