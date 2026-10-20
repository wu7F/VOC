# eval/ · Stage 8 评测回归（护城河）

改一次口径，**5 分钟内知道有没有变差**。

```
eval/
  golden/<案>/case.json      一个评测案：输入在哪、怎么跑
  golden/<案>/expected.json  人工核对过的锚点（下界断言）
  baselines/<案>.json        冻结的指标基线（--freeze 生成）
```

## 两层判据，缺一不可

| 层 | 文件 | 性质 | 谁写的 | 违反了会怎样 |
|---|---|---|---|---|
| **锚点** | `expected.json` | 下界断言：写了的必须成立 | **人**（对着真实产物核过） | 报「锚点违反」，退出码 1 |
| **基线** | `baselines/<案>.json` | 全量指标快照 | 机器（`--freeze`） | 逐格列 diff，退出码 1 |

为什么不能只留一层：

* **只有基线** → 换个环境重跑就全红（时间戳、路径回显），很快就没人看了；
* **只有锚点** → 只守住了写下来的那几格，剩下的静默漂移没人知道。

反过来也成立：**锚点会「红」，基线只会「diff」**。基线动了不等于错 —— 可能是有意
改口径。所以基线的作用是「让改动可见」，锚点的作用是「让错误可见」。

## 三个案

| id | tier | 模式 | 输入 | 用时 | 守什么 |
|---|---|---|---|---|---|
| `reddit-ng-consumer` | core | `annot-tail` | 归档的真实 LLM 标注（154 帖 + 7662 评论） | ~15s | **真结论**：分段轴归桶、画像覆盖、关键结论 top-N、标杆引文可回溯 |
| `pipeline-mock-consumer` | extended | `pipeline` | 原始导出跑完 7 步（mock 后端） | ~160s | 整条编排链路的确定性 |
| `pipeline-mock-b2b` | extended | `pipeline` | 同上，只换 `b2b_role` 框架 | ~90s | 换框架时上游不变、下游按框架声明改（轴 `购买状态` → `决策角色`，维度 7 → 8） |

> **默认只跑 `core`**（1 个案，~15s）—— 这是「5 分钟」承诺的落点。
> `extended` 两案的瓶颈是 `annotate` 要给 7662 条评论写中间文件（~150s），
> 留给「改编排层 / 换框架 / 发版前」跑。

### 为什么 `annot-tail` 案能复现真结论

归档目录 `annot/runs/ng2026/` 里是 2026-10-06 那次真实分析留下的**逐条 LLM 标注**
（154 个帖子 JSON + 7662 个评论 JSON），`reports/reddit_analysis_dimensions.json`
是当年人眼复核过的结论。两者之间的 `aggregate → report → dashboard → validate`
是**确定性**的，实测字段级完全复现（仅 `generated_at` 不同，用时 ~10s）。

## 指标（路线图要求的四项 + 两项契约）

| 指标 | 口径 | 路线图对应 |
|---|---|---|
| `evidence_verbatim_rate` | 1 − 引文未命中率，直接复用 Stage 6 那条检查 | 引文可核查率 |
| `persona_coverage` | Σ 画像标签计数 ÷ 全部单元 | 画像覆盖率（`count / 全部单元`）|
| `tag_coverage` | Σ 非画像标签计数 ÷ 全部单元 | 标签覆盖 |
| `excluded_unknown_ratio` | 排除的无法判定单元 ÷ 全部单元 | `excluded_unknown_units` 比例 |
| `conclusions` | 每个维度 count 最高的 top-3 `[标签, 计数]` | 关键结论是否稳定 |
| `products` | `dimensions` / `features` 去掉 `generated_at` 后的规范 sha1 | — |

> `persona_coverage` / `tag_coverage` 是**密度**不是去重覆盖率（同一单元可命中
> 同维度的多个标签）。路线图要的是「有基线、一变就看得见」，不需要统计上无懈可击。

## 用法

```bash
python tools/regress.py                 # 比对基线（默认 core）
python tools/regress.py --all           # 含 extended 两案
python tools/regress.py --case reddit-ng-consumer
python tools/regress.py --freeze        # 首次建立基线
python tools/regress.py --update        # 已确认改动，接受新答案
python tools/regress.py --markdown out/verify/stage8/regress.md

python voc.py eval                      # 同一套内核的另一个入口
python -m unittest tests.test_eval      # CI 入口（跑 core 案 + 纯函数测试）
python tools/_mutate_stage8.py          # 变异验证：证明回归真守得住
```

退出码：`0` 通过 ｜ `1` 锚点违反或有漂移 ｜ `2` 配置错误。

## 怎么加一个品类

1. `eval/golden/<新案>/case.json` —— 写明 `mode` / 输入路径 / 框架；
   * `annot-tail`：需要一份归档的逐条标注目录（`paths.annot_dir`）；
   * `pipeline`：给 `spec.input` / `spec.comments` 即可，mock 后端保证确定性。
2. `python tools/regress.py --case <新案> --freeze` 建基线；
3. 人工核对一次结论，把**该守的那几格**写进 `expected.json`（宁可少写，不要写会抖的）；
4. 再跑一次 `python tools/regress.py --case <新案>`，确认零漂移。

> 目前只有 1 个真实品类（Nursery Glider Recliner）。路线图原话是「3–5 个品类」，
> 这里受限于手头只有一份真实数据 —— 框架已经按「一案一目录、丢进去就生效」设计，
> 有第二个品类时按上面四步加即可。

## 踩过的坑

* **瞬时文件锁**：本机杀软 / 备份服务会扣住刚写出的文件 2–15s，导致覆盖写
  `PermissionError [WinError 5]`。`eval._run` 识别此类错误后指数退避重试（~12.5s
  窗口），`pipeline.atomic_replace` 同样重试（~13.8s）。这是环境噪声，不是逻辑错。
* **别用跑前/跑后差分推断产物**（Stage 7 的教训在评测里同样致命）：确定性重跑时
  字节不变 → 差分空集 → 幂等平凡为真。这里的 `products` 指纹是**直接算**的。
