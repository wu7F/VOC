# -*- coding: utf-8 -*-
"""临时探针：consumer 框架的文案渲染是否与改造前的硬编码逐字节相同。跑完即删。"""
from voc_core.analysis_frame import load_frame

CTX = dict(
    scope="帖子单元 + 评论单元合并计数",
    unit="帖子", secondary="评论", secondary_speaker="评论者",
    unit_pair="帖子 / 评论", merged="帖子 + 评论", unit_char="帖",
    n=7287, share="（占全部单元的 93.2%）", note="",
)

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

f = load_frame(None)
print("框架:", f.name, "|", f.label)
print("维度:", f.dim_ids())
print("画像映射:", f.persona_dims())
print("轴:", f.axis.dim, "| 桶:", [b.id for b in f.axis.buckets],
      "| 进矩阵:", f.axis.matrix_bucket, "| 分布键:", f.axis.distribution_key)

bad = 0
got = f.axis.render_texts(**CTX)
# 报告矩阵标题里的 ${note} 由代码按「有无次级单元」选版后传入 —— 这里模拟真实用法
got2 = f.axis.render_texts(**{**CTX, "note": got["matrix_note"]})
for k, want in EXPECT_TEXTS.items():
    g = got2.get(k)
    if g == want:
        print(f"  = texts.{k}")
    else:
        bad += 1
        print(f"  ! texts.{k}\n    期望: {want!r}\n    实际: {g!r}")

gotd = f.axis.render_dashboard(**CTX)
for k, want in EXPECT_DASH.items():
    g = gotd.get(k)
    if g == want:
        print(f"  = dashboard.{k}")
    else:
        bad += 1
        print(f"  ! dashboard.{k}\n    期望: {want!r}\n    实际: {g!r}")

# 桶级看板文案
for b in f.axis.buckets:
    if b.dashboard:
        print(f"  · 桶 {b.id}: {b.dashboard.get('card_title')!r} / "
              f"{b.dashboard.get('core_finding')!r} / persona_label={b.persona_label!r}")

print("\n不一致条数:", bad)
raise SystemExit(1 if bad else 0)
