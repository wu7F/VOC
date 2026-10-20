# web/ · Stage 9 C2（薄 Web 层 + 独立 worker 进程）

把内核包成一个**网页可用**的平台。**这一层是薄的** —— 分析逻辑一行都不在这里。

```
web/
  app.py          HTTP 层：路由 / 上传 / 回吐报告        ← 不含任何分析逻辑
  store.py        提交队列（SQLite，**跨进程**）
  worker.py       worker：心跳线程 + 落盘日志 + worker_id
  worker_main.py  **独立 worker 进程的入口**
  logs.py         跨进程 worker 日志（带缓冲）
  auth.py         PBKDF2 口令 + HMAC 签名会话 + 角色隔离（标准库）
  storage.py      存储抽象（local 已验证 / s3 骨架）
  serve.py        起服务（--worker / --auth / --storage）
  static/         最小前端：登录 + 上传 + 任务列表 + 报告 iframe
```

**C1 → C2 换了四样东西，内核一行没动：**

| 组件 | C1 | C2 |
|---|---|---|
| 队列 | `queue.json` + 线程锁 | `queue.db`（SQLite，跨进程原子认领） |
| 跑 job 的人 | Web 进程内的线程 | **独立 worker 进程**（默认 `--worker spawn`） |
| 跑完看到的日志 | 内存环形缓冲 | `<state>/logs/<job_id>.log`（落盘） |
| 谁能看什么 | 没有这个概念 | 可选认证 + owner 隔离 |

---

## 启动

```powershell
# 必须用带 openpyxl 的解释器（内核读 xlsx 时延迟导入它）
C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe web\serve.py

# 只起 Web，worker 自己另开一个终端跑（扩容 / 排查时最好用）
python web\serve.py --worker off
python -m web.worker_main --id worker-01

# 退回 C1 形态（worker 跑在 Web 进程里，仅调试用）
python web\serve.py --worker inproc

# 强制免登录 / 指定存储后端
python web\serve.py --no-auth
python web\serve.py --storage s3
```

依赖（只有 Web 层需要，**内核仍然零依赖**）：

```powershell
pip install -e ".[web]"        # fastapi + uvicorn + python-multipart
pip install -e ".[web,xlsx]"   # 还要读 xlsx 再加 openpyxl
pip install -e ".[web,s3]"     # 上对象存储再加 boto3
```

> `pyproject.toml` 只声明了 `voc_core` / `voc_core.adapters` / `web` 三个包。
> 内核在运行时按 `PLATFORM_ROOT` 解析**平台级资产**（`prompts/*.md`、`frames/*.yaml`、`voc.py`），
> 这些不是包内资源 → **当前受支持的安装方式是 editable**（`pip install -e .`），
> 树留在原地才解析得到。要做可分发的 wheel 得先把这些资产迁进包内，那是之后的事。

## 用户管理（认证）

```powershell
python -m web.auth adduser ethan --role admin     # 口令从 stdin 读
python -m web.auth passwd  ethan
python -m web.auth list
```

**默认关闭** —— 没有用户就是本机单用户形态，行为与 C1 完全一致。
一旦建过用户，认证**自动开启**（服务不用重启）：「建了用户却还免登录」是往不安全方向错。

## 环境变量

都有默认值，全部落在平台目录内。验收时用来隔离，不污染平台。
**Web 与 worker 必须看到同一份 `VOC_WEB_STATE_DIR`** —— 它们只通过 `queue.db` 说话。

| 变量 | 默认 | 作用 |
|---|---|---|
| `VOC_WEB_RUNS_DIR` | `<platform>/runs` | job 落地目录 |
| `VOC_WEB_UPLOAD_DIR` | `<platform>/uploads` | 上传文件暂存 |
| `VOC_WEB_STATE_DIR` | `<platform>/web_state` | `queue.db` / `users.json` / `secret.key` / `logs/` |
| `VOC_WEB_WORKER_MODE` | `spawn` | `spawn`(子进程) / `inproc`(线程) / `off` |
| `VOC_WEB_AUTH` | 推断 | `1` 强制开 / `0` 强制关 / 未设 = 有用户就开 |
| `VOC_WEB_SECRET` | 密钥文件 | 会话签名密钥（多实例共享时显式给） |
| `VOC_WEB_COOKIE_SECURE` | 关 | 走 HTTPS 时开 |
| `VOC_WEB_STORAGE` | `local` | `local` / `s3` |
| `VOC_WEB_S3_*` | — | bucket / prefix / region / endpoint / cache / 凭据 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 前端页面 |
| GET | `/api/health` | 存活 + worker 模式与存活 + 队列计数 |
| GET | `/api/meta` | 枚举 + 当前登录者 + worker 模式 + 存储后端 |
| POST | `/api/login` | 口令换 cookie（`HttpOnly`） |
| POST | `/api/logout` | 清除 cookie |
| GET | `/api/me` | 当前登录者 |
| GET | `/api/workers` | 所有 worker 与心跳新鲜度 |
| POST | `/api/jobs` | multipart 上传 → 建 `JobSpec` → 入队，返回 `201 {job_id}` |
| GET | `/api/jobs` | 任务列表（**按 owner 过滤**） |
| GET | `/api/jobs/{id}` | 详情：七步状态 + 闸门 + 跨进程日志 + 复现命令 |
| GET | `/api/jobs/{id}/report` | 看板 HTML（自包含） |
| GET | `/api/jobs/{id}/file/{kind}` | `dashboard`/`insight`/`quality`/`dimensions`/`features` **白名单** |

`/file/{kind}` 走**显式白名单**（`app.REPORT_FILES`），不做任意路径拼接 —— 否则就是目录穿越洞。

## 状态语义（**这块最容易做错**）

界面上的状态是三层信息折出来的：**队列状态 × `job.json.status` × 闸门结论**。

```
队列状态（queue.db）        只描述「已提交但 job.json 还答不了」的那一小段
job.json.status            权威状态：done / failed / waiting_manual
report/quality_report.json 闸门结论（Stage 6）
```

| 界面状态 | 来源 | 含义 |
|---|---|---|
| `queued` / `running` | 队列 | 排队中 / 跑着 |
| `done` | job.json + 闸门干净 | 完成 |
| `done_with_warnings` | job.json done + 闸门有 warning | **完成**，只是有警告 |
| `done_with_errors` | job.json done + 闸门有 error | **完成**，但产物有质量问题 |
| `waiting_manual` | 退出码 3 | **不是失败** —— 等人工回填，回填后重跑同一条命令即续跑 |
| `failed` | 退出码 1 / 异常 | 真失败 |
| `interrupted` | worker 心跳停了 | 可重跑（内核自带断点续跑） |

> **为什么 `done_with_errors` 不是 `failed`**：`validate`（闸门）退出码 1 表示「这批产物有问题」，
> 而**不是**「执行失败」—— Stage 7 定的规矩，闸门是出口检查、不阻断整体。
> 把它显示成「失败」会让用户以为分析崩了，然后去重跑，白白浪费一次 LLM 额度。

> **C2 的补充**：`queue.db` 里的行**跑完后留着**（提交账本，要记 owner），
> 所以 `entry.state == done` 是正常的 —— 它**不参与**状态合成，
> 跑完以后状态一律归 `job.json` 管。

---

## 六个刻意的设计选择

**1. 队列用 SQLite，不引入数据库服务。**
仍然**零第三方依赖**（`sqlite3` 是标准库）。它一次解决三件事：跨进程原子认领、
**提交时不做文件改名**（本机 `os.replace` 在工作区内实测最慢 11.68s，见设计选择 5）、
以及「谁还活着」这个 C2 才有的新问题。

> **为什么没用 WAL**（量出来的，不是查资料来的）：`tools/_probe_sqlite_mode.py`
> 压 8 线程 × 40 job × 3 轮 —— WAL **3/3 轮都报错**（`attempt to write a readonly
> database`，报在别人的 `BEGIN IMMEDIATE` 上），DELETE / TRUNCATE 全程干净；
> 事务延迟也是 DELETE 最低。所以用 SQLite 默认。详见 `store.py` 文件头。

**2. worker 是**独立进程**，不是线程。**
三条被量过的理由：抢事件循环、stdout 会挂死、崩一起崩（见下）。
**验收实测**：七步跑满期间 Web 最坏一次响应 **0.03s**（C1 冻住时整整两分钟不响应），
`server.log` 是 **0 字节** —— 内核一行回显都没漏进 Web 进程。

**3. 跨进程只通过两个共享文件通信。**
`queue.db`（谁该跑什么）与 `logs/*.log`（跑到哪了）。没有额外的消息中间件，
没有 RPC —— 排查时直接看盘上的文件就行。

**4. 上传文件不直接放进 job 目录。**
`JobSpec.input` 是**外部路径**，内核在 `run_job` 里把它复制进 `<job>/input/`（job 自包含）。
Web 层只管经 `web.storage` 落地，**不碰 job 内部布局**。
`Storage.ensure_local(key)` 是这份抽象里唯一不可妥协的契约 —— 内核只认本地路径，
所以任何后端都必须保证它返回一个真的在本地盘上、内核立即能读的路径（S3 后端按需拉回缓存目录）。

**5. 端点一律同步 `def`，阻塞 I/O 绝不进事件循环。**
（**这不是洁癖 —— 是修过一次真实故障的。**）

C1 第一版把 `POST /api/jobs` 写成了 `async def`，里面却做着同步落盘与 `queue.add`。
一次验收里，提交后**第一次**状态响应要等 **119.6s**，期间连 `GET /api/health` 都超时。
用 `faulthandler` 定时转储全线程调用栈抓到现场：

    事件循环线程：create_job → queue.add → _write → atomic_replace → os.replace   ← 卡在这一行
    worker 线程： claim_next                                                      ← 等同一把 Queue._lock

放大它的是两个本机因素：`os.replace` 在**工作区内**会秒级阻塞（实测 20 次里最慢一次
**11.68s**，而同样的代码在系统临时目录里最慢只要 **0.01s**），且 `atomic_replace`
还会**重试 10 次**。两者相乘 ≈ 87 秒 —— 与「handler 早在 13:55:20 就把 6MB 写完了，
客户端却到 13:56:47 才拿到响应」的观测完全吻合。

改成同步 `def`（FastAPI 会把它丢进线程池）之后，同一份验收：

    提交返回     0.3s   （原 119.6s）
    总耗时    175.6s   （原 335.0s）

`tests/test_web_blocking.py` 已把「任何 `/api` 端点都不许是协程函数」钉成回归判据
并做过变异验证（改回 `async def` 会立刻翻红）。

**6. 认证用标准库，不上第三方 JWT / OAuth 库。**
内核刻意零依赖，认证是可选件；一旦引入第三方库，装 Web 层就变成装服务。
而需要的只是三样：口令哈希（`hashlib.pbkdf2_hmac`）、签名 cookie（`hmac`）、
角色判定 —— 全是标准库。

---

## 已知限制（按设计，不是缺陷）

- **串行执行**：一次只跑一个 job。内核是「本地文件密集 + 长任务」，七步又是严格链式；
  并发会同时抢磁盘与 LLM 配额，语义没验证过。
  （队列本身已支持多 worker 并发认领 —— 真要并行，再多起一个 worker 进程即可。）
- **`running` 中的 job 在 worker 被杀后只能人工重跑**（标成 `interrupted`，不自动重试 ——
  自动重试会把同一批 LLM 任务灌两遍）。worker 一死心跳就停，30s 后 `reap_stale` 回收它。
- **S3 后端未做集成验证**：本机没 boto3、没有桶。`ensure_local` 的契约已按
  「下载到缓存目录」写好，但要上云的第一天得跑一次真桶的集成测试。
  缺 boto3 时它会**明确报错**，不会静默退回本地。
- **会话没有注销列表**：cookie 是签名的、TTL 7 天；要立刻吊销只能换密钥。
- **没有 CSRF token**：纯内网工具，且写操作都要登录。暴露到公网之前必须补。
- **uvicorn 与 worker 子进程的 stdout 必须有人消费**（**实测踩过，会挂死**）：
  内核大量回显 → 若 stdout 接的是一根没人读的管道，64KB 缓冲写满即阻塞，
  现象是「中间件文件全写完了但步骤永不返回」。本层已把 worker 子进程的 stdout
  重定向到 `<state>/logs/worker_proc.log`；部署 Web 时接终端 / journald / 日志文件都行，
  **不要接没人读的 pipe**。
- **本机 `os.replace` 会在工作区内秒级阻塞**（实测最大 11.68s）。
  `atomic_replace` 与 `storage` 都带退避重试兜住它，所以**写盘必须做在同步端点或 worker 里**。
- **worker 子进程是后台线程拉起的**（`_startup` 里不直接 `spawn_worker`）：
  同一个瞬时文件锁也会扣住**打开** `worker_proc.log` 这一步（C1 回归时实测卡在启动钩子里
  10s+，服务 30s 内 `/api/health` 不可达）。所以 spawn 丢到 `worker-boot` 线程去，
  开日志再慢也只是 worker 晚几秒。副作用：**「Web 能响应」与「worker 已就绪」之间差着几秒** ——
  判存活要看 `/api/health` 的 `worker_alive` 或 `/api/workers`，别一上来就断言。
  实在打不开日志会退到 `DEVNULL`：少一份日志可以，服务起不来不行。
- **等锁上限 `busy_timeout=2s`**（默认值，故意压得很低）：队列被别的进程占住时，
  `GET /api/health` 宁可**降级返回空**，也不能原地等到客户端判它超时。
  （原来是 15s，C2 验收就栽在这上面：探活 3s 超时的脚本直接判「服务没起来」，
  其实它只是在排一把马上就放的锁。）单机争用实测几十毫秒，2s 已经很宽松。

---

## 往 C3 迁的时候，动的是哪一块

| 组件 | C2（现在） | 更远 |
|---|---|---|
| 队列 | SQLite 单机 | 多机队列（Redis / DB） |
| 文件 | 本地 `runs/` | 对象存储（`--storage s3` 已留口） |
| 认证 | 本机用户表 | LDAP / SSO |
| **分析内核** | 本机 | **仍然本机** —— 云托管跑不了任意 Python 长任务 |

最后一行是不会变的：`cloud-service` 只能承担存储 / 认证 / 前端托管。
**分析引擎必须留在能被 SSH / 内网访问的 Python 进程里。**
