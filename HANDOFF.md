# VOC 洞察平台 · 交接说明（HANDOFF）

> 这份文档写给**接手这个项目的任何账号或人**。读完这一份，就能直接改代码、发版、验收，不需要重述历史背景。
> 最后更新：2026-10-21（对应 commit `d79c852`）

---

## 1. 这是什么

一个**多平台 VOC（用户之声）洞察平台**。上传各电商/论坛平台的评论或订单明细，自动产出分析报告与可视化看板。

**线上地址**：<https://voc.voctyc.cn>
**代码仓库**：<https://github.com/wu7F/VOC>（public，默认分支 `main`）

---

## 2. 板块结构（两级导航）

平台在 `server/app.py` 的 `PLATFORMS` 列表里定义，每项含 `id / name / label_cn / group / kind / icon / color / desc / hint`。

| 父板块 | 子板块 | platform id | kind | 分析管线 |
|---|---|---|---|---|
| **产品VOC** | Amazon | `Amazon` | `ecommerce` | 电商评论洞察（内置引擎 / LLM） |
| 产品VOC | Wayfair | `Wayfair` | `ecommerce` | 同上 |
| 产品VOC | Reddit | `Reddit` | `forum` | 帖子 + 评论，支持三种上传形态 |
| **售后VOC** | Amazon售后 | `AmazonCs` | `aftersale` | **售后独立聚合管线**（无 LLM） |
| 售后VOC | Wayfair售后 | `WayfairCs` | `aftersale` | 同上 |

`kind` 决定走哪条管线，在 `server/app.py::_run_job` 处分叉：
- `ecommerce` / `forum` → 常规评论洞察
- `aftersale` → `_run_aftersale_job` → `server/aftersale.py::run_aftersale`

---

## 3. 售后VOC 的数据契约（最容易踩坑的地方）

**输入是订单级明细表**（不是评论）。必须能识别出这三类列（中英文别名都支持）：

| 含义 | 可识别列名（部分） | 必需性 |
|---|---|---|
| 产品 | `产品` `产品名` `product` `ASIN` `SKU` | 必需 |
| 是否售后 | `是否售后` `售后` `订单状态` `is_aftersale` `status` | 必需 |
| 售后原因 | `售后原因` `原因` `reason` `refund_reason` | 强烈建议 |
| 日期 | `日期` `订单日期` `date` | 可选 |

判定规则：命中「是 / 退 / 换 / 售后 / 退款 / yes / true」记为**售后**，命中「否 / 无 / 正常 / no / false」记为**非售后**。

> 售后VOC 上传时**「品类」非必填**，留空直接分析即可。

**输出**（`result.json` 里 `report_type: "aftersale"`）：
```jsonc
{
  "report_type": "aftersale",
  "aftersale": {
    "platform": "AmazonCs",
    "total_orders": 12, "aftersale_orders": 6, "overall_rate": 0.5,
    "products": [{"product": "户外折叠椅", "total": 5, "aftersale": 3, "rate": 0.6}],
    "reasons":  [{"reason": "物流慢", "count": 2, "pct": 0.3333}]
  }
}
```

前端渲染 `web/app.js::renderAftersale()`（KPI + 柱状图 + 饼图 + 明细表），跨平台总览走 `GET /api/aftersale/summary` → `openAftersaleSummary()`。

---

## 3.5 飞书表格直连（售后 VOC 数据源）

售后 VOC 现在可以直接从**飞书电子表格 / 多维表格**拉数据，不用再「导出 Excel → 上传」。

**入口**：售后 VOC 平台 → 新建分析页 → 「飞书表格直连」卡片 → 粘贴链接 → 读取表格（先预览表头）→ 导入并分析。

**支持的链接**
- 电子表格 `https://xxx.feishu.cn/sheets/<spreadsheet_token>?sheet=<sheet_id>`
- 多维表格 `https://xxx.feishu.cn/base/<app_token>?table=<table_id>`
- 也接受裸 token（`shtcn…` / `bascn…`）；维基/文档链接不支持

**鉴权**：飞书开放平台**自建应用**的 app_id / app_secret 换 tenant_access_token。
- 优先读环境变量 `VOC_FEISHU_APP_ID` / `VOC_FEISHU_APP_SECRET`（生产推荐，已接进 docker-compose）
- 其次读 `data/feishu_config.json`（网页上「飞书应用凭证」里填过一次会存到这儿，文件权限 0600）
- 需要的权限：`sheets:spreadsheet:readonly`（电子表格）或 `bitable:app:readonly`（多维表格），并把目标表格授权给该应用

**代码位置**
| 文件 | 作用 |
|---|---|
| `server/feishu.py` | 链接解析、token 缓存、取数（sheets v2 values / bitable v1 records）、转 CSV、凭证读写 |
| `server/app.py` | `GET /api/feishu/config`、`POST /api/feishu/config`、`POST /api/feishu/pull` |
| `web/index.html` | `#cardFeishu` 卡片（仅售后 VOC 平台显示） |
| `web/app.js` | `updateFeishuUI / bindFeishuUI / fsPreview / fsStart / fsSaveCred` |
| `web/style.css` | `.fs-*` 样式 |

**实现要点**
- 只用标准库 `urllib`，不新增依赖
- 数据流：飞书 → 二维数组 → UTF-8(BOM) CSV → 落盘 → 复用现有 `run_aftersale` 管线，**不改动售后分析逻辑本身**
- `dry_run=True` 时只返回表头 + 前 8 行预览，并用售后管线同一套别名规则预判「产品/是否售后/售后原因」三列能否识别
- 目前**只允许导入到售后 VOC**（Amazon售后 / Wayfair售后），传其它平台会 400 拒绝

---

## 3.6 售后 VOC 的两种口径：明细模式 vs 台账模式

亚马逊售后台账（客诉/邀评登记）**只登记已发生的售后**，没有全量订单数做分母，
算出来的"售后率"一定是 100%（或 0%），属于误导性数字。平台因此引入两种口径：

| 模式 | 触发条件 | 输出 |
|---|---|---|
| **明细模式** `detail` | 有可靠的「是否售后」列，**且覆盖 ≥90% 的行** | 总单数 / 售后单数 / **售后率**（有值），产品级率也有值 |
| **台账模式** `ledger` | 无可靠 flag 列，或 flag 列覆盖不足 | 登记条数 / 售后记录数 / 售后率 = **None（不计算）**；产品级率同样为 None |

台账模式下如果存在可靠的 flag 列（如「具体事项」= 售后/对产品满意），
仍会用它**剔除明确标记为非售后**的行（`filtered_out`），但**不参与计算率**。

**看板呈现**
- 顶部橙色「台账口径」说明条，写明为什么不算率
- KPI：售后登记条数 / 售后记录数 / 整体售后率（显示 `—`）/ 涉及产品 / 涉及子店铺
- 柱状图改为「各产品售后登记量」（占全部登记的百分比），不再画 100% 的率
- 饼图用**归一化后的问题大类**（见下）
- 新增「各子店铺售后分布」表：子店铺 / 登记条数 / 占比 / 主要问题 TOP3 / 分布条
- 跨平台总览弹窗同步支持台账口径

### 问题原因归一化
台账里的「内容要点」是自由文本（可达数百种），平台按关键词规则归成 8 个大类：
`破损损坏 / 尺寸规格 / 缺件漏发 / 功能故障 / 物流配送 / 安装使用 / 要求退货 / 要求换货`（其余归「其他问题」）。
原始细分值仍保留在 `reasons[]`，大类在 `categories[]`。

### 子店铺维度
台账里的「类型」/「店铺」列（行善、香港店、威卡斯、麦吉天、浙捷、初颂、禾逸钦…）
被识别为 `shop` 角色，输出 `shops[]`：子店铺 / 登记条数 / 主要问题 TOP3。

### 列角色分配的坑（重要）
`aftersale.py` 的列角色**不是按别名顺序硬匹配**，而是：
1. **先按「售后语义可判定率」选 flag 列** —— 「类型」列装的是店铺名（行善/香港店…），
   若直接当售后标志会把 800+ 行全判成"非售后"，售后率变 0%
2. **角色互斥**：一列只承担一个角色，flag 优先占用，reason/shop/product 依次排除
3. **非语义列排除**：含「截图/图片/邮箱/订单号/金额/电话」等词的列不参与判定
   （订单号里含 `1`/`0`/`退` 等字符会被误判为售后标志）
4. flag 列还要满足「值域有区分度」（既有 True 又有 False），否则不可信

`adapters._find_header_row` 负责定位表头行：说明行（长句）会被跳过，
取**第一个「非空格 ≥2 且过半是短词（≤10字）」**的行；比「非空最多」更稳
（台账的数据行可能比表头列还多）。

### 多 sheet 合并
`adapters.read_table(path, multi_sheet=True)` 会读 xlsx 的**所有业务 sheet** 并合并，
跳过 WPS 保留表（`WpsReserved_*`），各 sheet 表头不同也没关系（行是 dict，列名取并集）。
`run_aftersale` 走这个模式；产品 VOC 仍用单 sheet，不受影响。

---

## 4. 代码结构

```
server/           后端（FastAPI）
  app.py          入口路由 + PLATFORMS 定义 + 任务调度（_run_job / _run_aftersale_job）
  aftersale.py    售后独立聚合管线（新增）
  feishu.py       飞书表格数据源（电子表格/多维表格 → CSV）
  store.py        数据集存储（注意：锁是模块级 _LOCK，不是实例属性）
  auth.py         邮箱账号制登录、限流（8 次错锁 5 分钟）
  pipeline.py     评论洞察管线
  llm_engine.py   LLM 标注引擎
  offline_engine.py 内置离线引擎（默认，无需配 key）
  taxonomy.py     维度体系
  report.py       报告生成
web/              前端（原生 JS + ECharts，无构建步骤）
  serve.py        本地启动入口
  index.html      页面骨架（两级导航 #platformGroups / #platformTabs、#asModal 弹窗）
  app.js          全部逻辑（约 2800 行）
  style.css       样式（约 900 行）
  login.css       登录页
Dockerfile / docker-compose.yml   容器化
```

**技术栈**：Python 3.13 + FastAPI + uvicorn；前端原生 JS + ECharts；无需 npm 构建。

---

## 5. 本地开发

```bash
cd "E:/Desktop/VOC洞察平台"          # Windows
python web/serve.py                   # 或见 README 的方式 A/B/C
```
默认端口 8848（本地），容器内 8080。

改完代码后建议自检：
```bash
python -m py_compile server/*.py      # 语法
node --check web/app.js                # 前端语法
```

---

## 6. 部署（云端常驻）

### 生产环境
| 项 | 值 |
|---|---|
| 服务器 | 阿里云 **轻量应用服务器**（Ubuntu 24.04） |
| 主机名 | `iZ2ze6j7dayyti1bzm8qf2Z` |
| 登录用户 | `admin` |
| 项目目录 | `~/VOC` |
| 容器 | `voc-voc-1`，镜像 `voc-insight:latest` |
| 端口 | `127.0.0.1:8080`（只绑本地，由隧道对外） |
| 数据卷 | `voc_data`（挂到容器 `/data`）—— **重建容器不丢账号/报告/上传** |
| 公网 | Cloudflare **命名隧道** `voc` → `localhost:8080`，域名 `voc.voctyc.cn` |
| 隧道配置 | `/etc/cloudflared/config.yml`（含 ingress 段，缺了会 503） |

### 发版三行（VPS 上执行）
```bash
cd ~/VOC
git fetch origin main
git reset --hard FETCH_HEAD
docker compose up -d --build
```
端口、卷名、隧道配置都不变，**不需要改任何其他配置**。

### 登录账号
- 邮箱：`3049044540@qq.com`
- 密码：**不写入本仓库**（仓库是公开的），请通过安全渠道单独同步。

---

## 7. 协作工作流（重要）

本项目由 AI 助手 + 人类协作推进，**沙箱环境没有 VPS 的 SSH 权限**，不能直接改服务器。固定流程：

```
1. 改源码  E:\Desktop\VOC洞察平台\
2. 自检    py_compile + node --check
3. 提交推送 git commit + git push origin HEAD:main
4. 人工执行 VPS 上那三行发版命令
5. 公网验收  curl --noproxy '*' https://voc.voctyc.cn/...
```

### 公网验收常用命令
```bash
# 静态资源是否是新版本
curl -s --noproxy '*' https://voc.voctyc.cn/app.js | grep -c renderAftersale

# 登录并拿 Cookie
curl -s --noproxy '*' -c /tmp/c.txt -X POST https://voc.voctyc.cn/api/auth/login \
  -H "Content-Type: application/json" -d '{"email":"...","password":"..."}'

# 平台分组
curl -s --noproxy '*' -b /tmp/c.txt https://voc.voctyc.cn/api/platforms

# 售后跨平台总览
curl -s --noproxy '*' -b /tmp/c.txt https://voc.voctyc.cn/api/aftersale/summary
```

> ⚠️ 沙箱有代理，验收公网地址**必须加 `--noproxy '*'`**，否则拿到的是代理的响应。

### 分支
- `main` —— **唯一权威分支**，线上跑的就是它
- `legacy-voc-platform` —— 早期单表单版备份，仅供回溯，不要在它上面开发
- ⚠️ 本地 clone 出来的分支名可能是 `master`（内容与 `main` 一致），**推送时务必显式 `git push origin HEAD:main`**

---

## 8. 已完成 / 待办

**已完成**
- 邮箱账号制登录（记住密码、失败锁定、启动遮罩）
- 两级板块导航（产品VOC / 售后VOC）
- 售后VOC 独立分析模式（产品售后率 + 原因分布 + 跨平台总览弹窗）
- Docker 化 + Cloudflare 隧道 + 云端常驻，公网可访问
- 报告记录、PDF 导出、报告记录分板块

**可继续深化的方向**
- 售后维度更聚焦：目前复用电商的维度体系，可做售后专属标签集（退换货 / 物流 / 客服态度 / 质量问题 / 描述不符）
- 售后时间趋势（按周/月看售后率变化）—— `aftersale.py` 已识别日期列，可扩展
- 售后原因跨平台对比视图
- 按 ASIN / SKU 细分到单品级
- LLM 引擎接入（`llm_engine.py` 已有骨架，售后管线目前是纯聚合、不走 LLM）

---

## 9. 相关文档

- `README.md` —— 功能总览、指标口径、目录结构、FAQ
- `docs_公网部署.md` —— 从本机脚本到公网常驻的完整部署教程
