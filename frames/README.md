# `frames/` —— 分析框架（"分析什么"的配置）

Stage 5 把「分析什么」从代码里的 8 个键变成了这一份 YAML。
**换框架不用改一行代码**，换的是这里的一个文件。

| 文件 | 是什么 |
|---|---|
| `consumer.yaml` | 默认框架（消费品，按购买状态分桶）。**不带 `--frame` 时用它**，行为与平台化之前逐字节相同 |
| `b2b_role.yaml` | B2B / 组织采购框架（按决策角色分桶，**没有购买状态轴**） |

代码侧：`voc_core/analysis_frame.py`（装载 / 校验 / 渲染）、`voc_core/miniyaml.py`（零依赖 YAML 子集）、
`voc_core/classifiers.py`（内置分类器定义）、`voc_core/frame_prompts.py`（提示词分片）。
契约测试：`tests/test_frame.py`（21 项）。

---

## 一、`--frame` 与 `--profile` 是两件事，别混

| 参数 | 回答的问题 | 决定 | 值域 |
|---|---|---|---|
| `--profile` | **数据从哪来** | 用户可见文案（"帖子 / 子版块" 还是 "评论 / 站点"）与能力开关 | `voc_core/source_profile.py` 里的源画像 |
| `--frame` | **分析什么** | 维度清单、分桶轴、看板模块开关 | 本目录的 `<name>.yaml` |

同一份亚马逊评论，可以按购买状态分析，也可以按决策角色分析 ——
所以它们是**两个参数**，不是同一个参数的两个取值。两者正交，可以自由组合。

> 路线图里这一层的原名是 `profiles/<行业>.yaml`；落地时改叫 `frames/<框架名>.yaml`。
> 原因就是上面这张表：`--profile` 已经被源画像占用了，两个都叫 profile 必然出事。
> 目录也刻意分开 —— `frames/` vs `voc_core/source_profile.py`。

---

## 二、怎么用

```bash
python voc.py frames                       # 列出内置框架 + 每个的维度数与轴
python voc.py frames b2b_role              # 看某个框架的详细信息
python voc.py frames consumer --yaml       # 打印框架（与 frames/*.yaml 同构，可直接存盘复用）

# 换框架跑分析：--frame 只在第一步给一次
python voc.py tags       ... --frame b2b_role                 # ① 标签设计（框架元数据从这里写进产物）
python voc.py annotate   ...                                  # ② 标注（从 tagset.json 读框架）
python voc.py aggregate  ...                                  # ③ 聚合（从产物读框架）
python voc.py report     ...                                  # ④ 报告
python voc.py dashboard  ...                                  # ⑤ 看板
python voc.py validate   ...                                  # ⑥ 闸门
```

**产物自描述（关键设计）**：框架信息只在 `tags` 这一步给一次，
`design_tags` 把它写进产物（`tagset.analysis_frame` / `dimensions.analysis_frame`），
下游全部从上游产物读。所以下游不需要再挂 `--frame`，也不存在"忘了传导致不一致"。

`--frame` 也仍然被下游接受（用于读**旧产物**：那些没有 `analysis_frame` 块、改造前生成的产物），
另外可以用环境变量 `VOC_FRAME=b2b_role` 设默认值。优先级：
**产物里的 `analysis_frame` > `--frame` > `VOC_FRAME` > 内置 `consumer`**。

`--frame` 的取值可以是：内置名（`consumer`）、文件路径（`frames/b2b_role.yaml`）、
或把产物里那段 `analysis_frame` 原样存成 yaml 直接喂回来。

---

## 三、键参考

### 顶层

| 键 | 必填 | 说明 |
|---|---|---|
| `name` | ✅ | 框架名（`voc.py frames <name>` 用的就是它） |
| `label` / `description` | 推荐 | 人读的标题与一句话说明 |
| `sample_tags_per_dim` | | 提示词里"每维度约 N 个标签"的 N（默认 3） |
| `dimensions` | ✅ | 维度清单，见下 |
| `segmentation_axis` | | 分桶轴，见下。**不写 = 这个框架没有分桶轴**（如硬件配件只关心兼容机型） |
| `modules` | | 看板模块开关：`auto` = 按数据可用性自动开关；`true` = 总是开；`false` = 强制隐藏 |
| `category_legend` | | 标签 `category` → 中文说明（`user` → 人群画像…），注入提示词 |
| `prompt` | | 提示词各段落的文案（`purpose_line` / `dim_block_header` / `summary_line` / `persona_requirements` …） |
| `notes` | | 纯注释，不参与逻辑 |

**未知键一律报错**（`FrameError`）—— 手滑把 `dimensions` 写成 `dimension` 而静默忽略，
症状是"我明明配了但没生效"，比直接报错难查一个数量级。

### `dimensions[]`

| 键 | 说明 |
|---|---|
| `id` | 维度键，即产出 JSON 里的键名（`usage_scenarios`…） |
| `label` | 中文名（报告 / 看板里显示） |
| `category` | 必须落在 `category_legend` 里（`user` / `scene` / `pre_purchase` / `post_purchase` / `feature`） |
| `kind` | `generative`（默认，标签值由 LLM 归纳）/ `classifier`（脚本注入固定分类器） |
| `persona_bucket` | 非空 = 这个维度是某个桶的人群画像（值 = 桶 id） |
| `analysis_hint` | 注入该维度各标签 `thoughts` 的【判定前置】前缀 |
| `experience` | `true` = 参与情感 / 满意度统计 |
| `deprecated` | 旧标签集里可能有，一律忽略、不产出（如 `user_personas_unknown`） |
| `optional` | 校验时允许缺失 |
| `prompt_line` | 该维度在 `design_tags` 提示词里的说明行，可含 `${dim_id}` / `${dim_label}` / `${dim_category}` |

### `segmentation_axis`

| 键 | 说明 |
|---|---|
| `dim` | 轴落在哪个维度键上（`purchase_status` / `decision_role`） |
| `label` | 轴的中文名（`购买状态` / `决策角色`）—— 报告看板里所有 `${axis}` 都渲染成它 |
| `classifier` | 引用 `voc_core/classifiers.py` 里的**内置分类器**（`purchase_status` / `decision_role`） |
| `distribution_key` | 产物里的分布键（`purchase_status_distribution` / `decision_role_distribution`） |
| `unit_counts_key` | 产物里全桶计数的键 |
| `matrix_bucket` | 哪个桶进「画像 × 功能」矩阵（consumer = `purchased`） |
| `unknown_bucket` | 排除桶的 id（`unknown` / `unclear`） |
| `buckets[]` | 桶定义，见下 |
| `prompt_axis_question` | 标注规则里"先判断发言者___"的填空（`是否已购买该品类产品`） |
| `prompt` | 轴相关的提示词段落（`annotate_axis_rule` / `deprecated_persona_note`） |
| `texts` | 聚合 / 报告文案，见下 |
| `dashboard` | 看板文案，见下 |

### `buckets[]`

| 键 | 说明 |
|---|---|
| `id` | 桶 id（`purchased` / `non_purchased` / `unknown`） |
| `label_cn` | 完整名，分布图与表格里用（「已购买者」） |
| `short_label` | 判定句式里用（「已购」） |
| `judge_label` | 分布副标题的「先判定 A / B / C」串里用（「已购买」） |
| `persona_short` | 画像小结里用（「购买者」） |
| `persona_label` | 该桶对应画像维度的中文名 —— **加载时自动从维度反查，通常不用写** |
| `persona_condition` | 标注规则里那句话的条件（「已购买」「尚未购买」） |
| `tags` | **这个桶收纳哪些分类器标签**（`[ps_purchased]` / `[dr_decision_maker]`）—— 显式声明，不靠猜 |
| `excluded` | `true` = 不进画像桶、不进分布图，只计数并披露 |
| `dashboard` | 该桶的看板卡片文案（`card_title` / `card_subtitle` / `core_finding`） |

> 同一个桶在不同句子里有多种叫法，**全部显式配置**，而不是代码里裁字符串
> （裁剪脆弱且不可配：「已购买者」去掉「购买」两个字得不到「已购」）。

### `texts` / `dashboard` 的文案与变量

值里可以写 `${var}`（`string.Template` 语法，**严格模式**：变量写错立即报错，不会静默留空）。
可用变量由 `Axis._base_ctx()` 统一提供：

| 变量 | 值（consumer 例） |
|---|---|
| `${axis}` / `${axis_dim}` | `购买状态` / `purchase_status` |
| `${bucket_list}` / `${bucket_full_list}` | `已购 / 未购` / `已购买者 / 未购买者` |
| `${judge_list}` | `已购买 / 未购买` |
| `${persona_list}` | `购买者 / 未购买者` |
| `${bucket0}` `${bucket1}` … | 第 N 个**非排除**桶的短名（`已购` / `未购`） |
| `${bucket0_full}` … | 第 N 个非排除桶的完整名 |
| `${persona0}` / `${persona0_short}` … | 该桶对应画像维度的中文名 / 简称 |
| `${excluded}` / `${excluded_full}` | 排除桶的短名 / 完整名（`无法判断`） |
| `${matrix_bucket}` / `${matrix_bucket_full}` | 进矩阵那个桶的判定短名 / 完整名（`已购`） |
| `${other_buckets}` | 除进矩阵那个桶外的非排除桶（`未购`；三桶框架下是 `影响者 / 使用者`） |
| `${framework_items}` | `dashboard.framework_items` 的逗号列表 → 箭头串 |
| 其余 | `string.Template` 的调用点上下文（`${unit}` `${secondary}` `${category}` `${n}` `${share}` …） |

两个命名空间**刻意不互注**：`render_texts()` 只渲染 `texts` 组、`render_dashboard()` 只渲染 `dashboard` 组。
因为 `matrix_note` 在两组里**同名不同值** —— 报告里说「仅已购**帖**入矩阵」（简称），
看板里说「仅已购**帖子**入矩阵」（全称）。互相注入会静默覆盖，症状是看板副标题里少一个字。

---

## 四、**不变量**：闸门逻辑与框架无关

闸门逻辑一字未改，它属于**分类器**而不是框架：

> 一个单元恰好命中该轴**一个**标签才生效；命中 0 个或 ≥2 个都判 `unknown`（排除）。

变的只是「标签 → 桶」这张映射表，而且它由 `buckets[].tags` **显式声明**，不靠猜。
所以下面这 6 条对任何框架都必须成立（`tools/stage5_baseline.py --compare` 会自动断言）：

1. 排除桶不出现在分布里
2. 分布条目 = 活跃桶（不多不少）
3. 全桶计数仍披露（排除桶不静默消失）
4. 排除计数自洽（`excluded_unknown_units` == 排除桶 total）
5. 总量自洽（分布之和 + 排除量 = 单元总数）
6. 画像维度只挂活跃桶

**产物契约键名不随框架改**：`excluded_unknown_units` / `unit_counts` 这些键在换框架后**不改名**
—— 换的是内容，不是形状，下游与闸门不必知道有几个桶。

---

## 五、写一个新框架

1. `cp frames/consumer.yaml frames/<新名>.yaml`，改 `name` / `label` / `description`。
2. 改 `dimensions`：留下要用的维度，删掉不要的；每个画像维度挂好 `persona_bucket`。
3. 改 `segmentation_axis`：
   * `dim` / `label` / `distribution_key` / `unit_counts_key` 换成新轴的名字；
   * `buckets[]` 逐个改 `id` / 四种叫法 / `tags`；不用的桶删掉，排除桶标 `excluded: true`；
   * 检查 `texts` / `dashboard` 里所有涉及桶数的句子 —— **桶数从 2 变 3 时，
     写死 `${bucket1}` 的句子就不对了**，该用 `${other_buckets}` 就用它。
4. `python voc.py frames <新名>` 看装载是否通过（跨字段校验会在这里拦下来）。
5. 跑一遍 `--frame <新名>` 全链路，然后 `python tools/_probe_frame_switch.py <产物目录> --old-axis <旧轴名>`
   验收：换轴生效 / 无旧轴残留 / 空模块自动隐藏 / 下游不报错。

如果新轴需要一套**新的分类器**（不是改桶，而是改"怎么判"），
在 `voc_core/classifiers.py` 的 `CLASSIFIERS` 里加一个条目（标签定义 + 标签→桶的顺序），
再在框架 YAML 的 `segmentation_axis.classifier` 里引用它。

---

## 六、零回归约定（改这个目录前先读）

* **不带 `--frame` 时加载内置 `consumer`，渲染结果与平台化之前逐字节相同。**
  这一点由 `tests/test_frame.py` 的文案等价性用例（`EXPECT_TEXTS` 12 条 + `EXPECT_DASH` 7 条逐字节）守着。
* 改 `consumer.yaml` 的文案 = 改用户可见输出。改之前先跑
  `python tools/stage5_baseline.py --tag <标签> --compare out/verify/stage5/baseline/baseline.json`，
  它做**掩码差分**（把预期会变的区块整块抠掉，剩余内容必须逐字节一致）——
  行级 `diff` 在「两行合并成一行」这类改动上会骗人。
* `frames/*.yaml` 的提示词影响 `design_tags` 的最终提示词。改完跑
  `python tools/stage5_prompt_baseline.py` 比对 golden（`tests/fixtures/prompts_golden/`）。

### 这份文件用的 YAML 是**子集**，不是 YAML

`voc_core/miniyaml.py` 只支持：注释、`键: 值`、行内列表 `[a, b]`、行内映射 `{x: 1}`、
列表内映射、字面块标量 `|`。
**不支持并明确报错**：锚点/别名（`&` / `*`）、多文档（`---`）、`!` 标签、tab 缩进、
重复键、同层缩进不齐、`-` 的层级跳跃。

刻意保守的两处类型推断，都是踩过坑的：

* 只有 `true` / `false`（含首字母大写）算布尔，`yes` / `no` / `on` / `off` **一律当字符串**
  —— 否则 `country: no` 会静默变成布尔 `False`（YAML 1.1 最有名的咬人坑）。
* 值以 `#` 开头、或以「空格 + 竖线」开头时**必须加引号**：
  `dim_block_header: '## 维度框架…'` —— 不引起来的话 YAML 会把它当注释 / 块标量标记，
  整行变成 `null`，而且**不会有任何报错**（症状：提示词里悄悄丢了一整段）。
* 多行块标量在 YAML 里**每行独立写，不要折行** —— 折行会在断开处补空格，悄悄改变交付文案。
