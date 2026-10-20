# VOC洞察平台 · 从"本地脚本"到"真正网页平台"

> 目标：**别人在浏览器里输入一个网址就能用**，而不是必须在你这台电脑上跑脚本。
> 这份文档讲清楚三件事：现在差什么、怎么补、以及**必须先做的那一步**。

---

## 一、动手之前：先认清 5 个卡点

| # | 卡点 | 原来的样子 | 现在 |
|---|---|---|---|
| 1 | **只能本机访问** | 绑 `127.0.0.1` | `--host 0.0.0.0` 已支持 |
| 2 | **没有登录** | 谁打开谁就能删报告 | **已加登录 + 只读访客** |
| 3 | **数据在桌面** | `E:\Desktop\...\runs` | **已支持 `VOC_DATA_DIR`**，现迁到 `C:\VOCData` |
| 4 | **依赖 WorkBuddy 的 Python** | 借用托管 venv | 打包 exe 后彻底独立 |
| 5 | **随会话托管** | 我的后台任务托管，一停就掉 | `start.bat` / 开机自启 / 计划任务 |

---

## 二、⚠️ 第一步必须是开密码

**这是硬前提，不是可选项。**

平台的"删除报告"是 `DELETE` 接口。在没有鉴权时，任何拿到网址的人都能删光你的全部分析产出。
所以顺序只能是：**先加锁 → 再开门**。

```bat
set VOC_PASSWORD=你自己的管理员口令
set VOC_GUEST_PASSWORD=给客户看的只读口令
```

设计成两个角色：

| 角色 | 口令 | 能做什么 |
|---|---|---|
| **admin** | `VOC_PASSWORD` | 新建 / 重跑 / 删除 / 清理残留 / 导出 PDF / 看全部 |
| **guest** | `VOC_GUEST_PASSWORD` | 只能**看**：浏览、打开报告、导出 PDF。删不掉、跑不了 |

> **没设口令时的行为**（刻意的）：本机 `127.0.0.1` 照常可用一切，**远程一律只读**。
> 这样"忘了设密码"不会导致公网裸奔，同时不破坏"本机双击即用"的体验。

已实现的防护：

- 登录失败 8 次 → 锁定该 IP 5 分钟
- 会话用 HMAC 签名 Cookie（`httponly`），密钥随机生成后落盘
- 上传单文件上限 200MB（`VOC_MAX_UPLOAD_MB` 可调）
- 异常信息里的绝对路径会被剥离成 `<路径>`，不泄漏磁盘结构

---

## 三、数据与代码分离

平台不再假设"数据就在代码旁边"。数据根目录按这个顺序解析：

1. 环境变量 `VOC_DATA_DIR`
2. 打包成 exe → `<exe 同级>/VOCData`
3. 源码运行 → 项目根目录（保持老行为）

目录结构：

```
VOCData/                 ← 数据（可整目录拷走 = 完成迁移/备份）
├── runs/                 分析产出（index.json + 每条记录的目录）
├── data/                 演示数据
├── llm_config.json       LLM 配置（含密钥，勿外传）
└── .session_secret       会话签名密钥（自动生成）
```

**迁移 = 拷走 `VOCData` 目录**，代码可以随时换。

---

## 四、三种启动方式

### 方式 A：本机双击（最简单）

```
双击 start.bat
```

### 方式 B：打包成独立 exe（换电脑也能跑）

```bat
python build_exe.py
```

产物 `dist\VOC洞察平台\VOC洞察平台.exe`，**目标机器不需要装 Python**。
把 `exe` 整个目录拷过去，设置 `VOC_PASSWORD`，双击即可。

### 方式 C：开机自启（服务器/长期运行）

把下面存成 `安装自启.bat`，管理员身份运行一次：

```bat
@echo off
set VOC_PASSWORD=你的口令
set VOC_GUEST_PASSWORD=客户口令
schtasks /create /tn "VOC洞察平台" /tr "\"%~dp0start.bat\"" /sc onlogon /rl highest /f
```

---

## 五、让它变成"网页平台"：暴露到公网

本机跑起来只是第一步。**要让客户在浏览器里输入网址就能用**，还需要一个隧道把本地端口反向代理出去（自动配好 HTTPS，且**不会**把 8080 端口直接开到公网）。

### 方案 1：Cloudflare 快速隧道（免费，先用这个试水）

```bat
winget install --id Cloudflare.cloudflared
双击 公网访问.bat
```

会打印出一个 `https://xxxx.trycloudflare.com` 网址，直接发给客户即可。

- ✅ 免费、自带 HTTPS、不用改路由器、不用开放端口
- ⚠️ **地址每次重启都会变** —— 想固定地址见「方案 3」

> **国内访问速度**：Cloudflare 免费线路在大陆没有优化节点，部分运营商可能较慢。
> 先让真实客户实测，若嫌慢改用方案 2（cpolar）或方案 4（境内云服务器）。

### 方案 2：cpolar（国内访问更快）

```bat
到 https://www.cpolar.com/download 下载安装
双击 公网访问.bat   选 2
```

国内网络下延迟明显低于 Cloudflare；免费账号地址也是随机的。

### 方案 3：正式域名 + 固定地址（推荐长期使用）

**这是"永久不变的网址"的唯一正解。** 一次性配置，之后地址永不变。

#### 第 1 步：买域名

推荐 **Cloudflare Registrar**（`dash.cloudflare.com` → Register Domains），
因为 Named Tunnel 要求 DNS 托管在 Cloudflare，两件事正好配套。
2026 年 `.com` 约 **$10/年**，注册与续费同价，WHOIS 隐私免费。

若付不了款，**Porkbun / Namecheap 都支持支付宝**，买完把 NS 改成 Cloudflare 给的两个
nameserver 即可。⚠️ 不要买"首年 $1、续费 $23"那种促销型，续费价才是真价格。

买完在 Cloudflare 控制台 `Add a Site` 添加该域名，选 Free 计划。

#### 第 2 步：跑配置脚本

```
双击 配置命名隧道.bat
```

按提示输入域名即可，脚本会自动完成：

1. `cloudflared tunnel login`（弹浏览器授权）
2. `cloudflared tunnel create voc`（创建命名隧道，拿到永久 ID）
3. 写 `%USERPROFILE%\.cloudflared\config.yml`
4. `cloudflared tunnel route dns voc voc.你的域名.com`
5. 试跑 5 秒并实测 `https://voc.你的域名.com` 是否返回 200
6. 询问是否装成 **Windows 开机自启服务**

也可以命令行传参：

```bat
配置命名隧道.bat vocinsight.com voc 8080
```

#### 第 3 步：日常使用

配好之后，`公网访问.bat` 会**自动识别**到命名隧道，直接用永久地址，
并优先走 Windows 服务（开机自启、崩溃自动重启）。

### 命名隧道 vs 快速隧道

| | 命名隧道 | 快速隧道 |
|---|---|---|
| 地址 | `https://voc.你的域名` **永久不变** | `https://xxx.trycloudflare.com` **每次都变** |
| 需要域名 | 是 | 否 |
| 开机自启 | 支持（Windows 服务） | 不支持 |
| 适合 | 正式给客户用 | 临时演示、自测 |

### 方案 4：部署到云服务器

如果你有 VPS / 云主机，直接把项目传上去跑即可（Linux 用 `start.sh`），
数据目录放在带持久磁盘的路径上。这是最"正规"的形态，也最容易做备份与监控。

---

## 六、上线前的检查清单

- [ ] `VOC_PASSWORD` 已设置，且**不是**弱口令
- [ ] `VOC_GUEST_PASSWORD` 已设置（给客户只读账号）
- [ ] 用**访客口令**登录，试着点删除 —— 应该提示"只读访客身份，无法执行写操作"
- [ ] 数据目录已迁出桌面，并**至少备份一次**
- [ ] 隧道地址能在**手机流量**（不是同一 WiFi）下打开
- [ ] 浏览器地址栏是 `https://`（不是 `http://`）

---

## 七、常见问题

**Q：忘记管理员口令了？**
删掉数据目录里的 `.session_secret` 并重启可让所有人重新登录（但**不会**改变口令本身）。
口令只存在于环境变量里，改完环境变量重启即可。

**Q：客户要能看到全部历史报告吗？**
可以。访客能看到全部记录。不想让访客看到某条，就用管理员把它删掉。

**Q：能不能限制访客只能看某个平台？**
目前是全局只读。要做平台级权限可以扩展 `auth.py` 的 `can_read(role, platform)`。

**Q：数据会不会因为重启丢？**
不会。产出都写在 `VOCData/runs/`，重启服务只是重新读取索引。

---

## 八、方案 5：GitHub + 云端常驻（关机也不影响别人用）

> 目标：**这台电脑关了，网站照样 7×24 运行**，其他人照常用。
> 本质是把后端搬到一台永不关机的云机器，再把 `voc.你的域名.com` 指向它。

### 适用前提

- 代码推到 GitHub（仓库已就绪：`.gitignore` 已排除 `llm_config.json` / `VOCData` 等密钥与数据，**可放心公开**）
- 选一个 GitHub 关联的海外 PaaS（数据可放境外，已确认）：**Render / Railway / Fly.io**

### 已备好的部署物料（项目根目录）

| 文件 | 作用 |
|---|---|
| `requirements.txt` | 锁定的依赖（fastapi/uvicorn/pandas/openpyxl/gunicorn 等） |
| `Procfile` | `gunicorn ... --bind 0.0.0.0:$PORT`（Heroku 风格，Render/Railway 通用） |
| `render.yaml` | Render 原生配置：含 1GB 持久盘 `/data`、环境变量占位 |
| `Dockerfile` | Fly.io / 任意 Docker 平台；含无头 Chromium（PDF 导出） |
| `.dockerignore` | 同 `.gitignore`，避免把密钥打进镜像 |
| `llm_config.example.json` | LLM 配置脱敏模板（真实密钥走环境变量，不入库） |
| `web/serve.py` | 已支持 `$PORT` 环境变量 |

### 步骤

**① 推到 GitHub**

```bash
# 在 GitHub 新建一个空仓库（不要勾 README），拿到地址后：
git remote add origin https://github.com/<你>/<仓库>.git
git branch -M main
git push -u origin main
```

**② 部署到 Render（最省事，GitHub 一键关联）**

1. 打开 https://render.com → Sign in with GitHub → 授权
2. New → Web Service → 选刚推的仓库
3. Render 会读 `render.yaml`；在面板里补三个环境变量（从你本机 `llm_config.json` 抄，**别提交到仓库**）：
   - `LLM_CONNECTOR_BASE_URL` = `base_url`
   - `LLM_CONNECTOR_API_KEY`  = `api_key`
   - `LLM_CONNECTOR_MODEL`    = `model`
4. 确认磁盘 `vocdata` 已挂到 `/data`（`VOC_DATA_DIR` 已设成 `/data`）
5. Deploy → 拿到 `https://voc-insight.onrender.com`

> ⚠️ Render 免费版**闲置会休眠**，首次访问约慢 30–60 秒；要常驻选 Starter 及以上，或改用 Railway（$5 额度、不休眠）。
> PDF 导出在免费版需另外装 Chromium；主流程不受影响。

**③ 把账号迁到云端**（云端 `/data` 是空的，需重建）

Render 控制台 → 你的服务 → Shell，执行：

```bash
python manage_accounts.py add 3049044540@qq.com
# 按提示输入密码 1330527
```

**④ 切换域名到云端（关键一步，关本机前做）**

Cloudflare 控制台 → DNS：
- 把 `voc` 这条 **CNAME 的目标**从「隧道」改成 `voc-insight.onrender.com`
- 代理状态（橙色云）保持 **开**

改完后：本机 `cloudflared` 可以关了，`voc.你的域名.com` 直接走云端，这台电脑关机无影响。

### 想保留历史报告？

本机 `C:/VOCData/runs`（约 57MB）在云端是空的。要么：
- 接受"云端从零开始"，历史报告留在这台电脑以后再说；
- 或把 `C:/VOCData/runs` 打包上传到云端磁盘（Render Shell 里用 `curl`/base64 等方式灌入 `/data/runs`）。

### 回滚

DNS 把 CNAME 目标改回隧道、重启本机 `cloudflared` 即可回到"本机托管"模式。