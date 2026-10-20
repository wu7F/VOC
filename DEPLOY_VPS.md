# 部署到阿里云轻量应用服务器（Docker）

目标：买一台 7×24 的小机器，跑你现在的 `python web/serve.py`，用已有的 Cloudflare
隧道 `voc.voctyc.cn` 暴露出去。**本机关了完全不影响线上**，且国内访问最快。

---

## 1. 买机器

- 阿里云 → **轻量应用服务器**
- 地域：选离用户近的（华东1 杭州 / 华南1 深圳 / 华北2 北京）
- 镜像：**Ubuntu 22.04 / 24.04**（首选）
  - Alibaba Cloud Linux 3 / CentOS 也能跑（脚本已兼容 dnf/yum），但对本项目的资料最多、踩坑最少的是 Ubuntu
- 规格：**2C2G** 最稳；**2C1G 也能跑**（本项目零依赖、内存占用约 100–200MB），
  脚本会自动补 2GB swap 防止构建镜像时 OOM
- 带宽：3–5 Mbps 固定最便宜；「200Mbps 峰值 + 不限流量」那类套餐也能用，
  但对本项目（纯文本分析与少量上传）属于买多了

> 价格参考：常规价 2C1G/30G ≈ ¥40/月（年付有折扣）；新用户特惠档 2C2G 常能看到 ¥68–99/首年。
> 先看「特惠/新人」入口，再决定档位；**年付比月付划算很多**。

---

## 2. 一键部署

登录服务器后执行（自动装 Docker → 拉代码 → 写密钥 → 构建启动 → 建首账号）：

```bash
curl -fsSL https://raw.githubusercontent.com/wu7F/VOC/main/deploy.sh | bash
```

若 raw 地址不通，可先手动拉再跑：

```bash
git clone https://github.com/wu7F/VOC.git && cd VOC && bash deploy.sh
```

脚本会交互式问你：DeepSeek 密钥、管理员邮箱、管理员密码。

脚本已处理这些国内常见坑：
- `curl | bash` 下的交互输入（显式读 `/dev/tty`，否则会把脚本内容当输入）
- 1GB 小内存自动建 2GB swap（防 `docker build` 被 OOM kill）
- Docker Hub 拉不动时自动切国内镜像加速源（`docker.m.daocloud.io` 等）
- pip 走阿里云 PyPI 镜像（`Dockerfile` 里默认已配）
- Ubuntu 走 apt、Alibaba Cloud Linux / CentOS 走 dnf + 阿里云 docker-ce 源

若 `git clone` 在服务器上失败（国内访问 GitHub 偶发 reset），两种走法：
```bash
# 1) 本机打包上传（最稳）
scp -r voc-platform root@<服务器公网IP>:/root/VOC
# 2) 或在服务器上给 git 配代理后重试
git config --global http.proxy http://127.0.0.1:7897
```

---

## 3. Cloudflare 隧道（复用你已有的域名）

你本地已有一条隧道 `voc` 把 `voc.voctyc.cn` 指向你电脑。在 VPS 上二选一：

### 方案 A（推荐，新建隧道）

```bash
# 装 cloudflared
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

cloudflared tunnel login            # 浏览器授权（会打开你的 Cloudflare）
cloudflared tunnel create vocvps     # 新建一条隧道
cloudflared tunnel route dns vocvps voc.voctyc.cn   # 把域名指向新隧道
# 后台常驻（用仓库里的 cloudflared.service）
cp cloudflared.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now cloudflared
```

`cloudflared tunnel run vocvps` 默认连 `http://localhost:8080`，正好对上容器端口。

### 方案 B（复用本地隧道）

把本地 `~/.cloudflared/`（含 `cert.pem` 与 `<tunnel-id>.json`）拷到 VPS 同路径，
然后**本机先停掉 `cloudflared`**，VPS 上执行 `cloudflared tunnel run voc` 即可。

---

## 4. 收尾

- 本机停掉 `cloudflared`（之前是它把域名指到你电脑的）。
- 浏览器开 `https://voc.voctyc.cn`，用第 2 步建的管理员邮箱 + 密码登录。

---

## 5. 日常维护

```bash
# 更新代码并重建
cd VOC && git pull && docker compose up -d --build

# 看日志
docker compose logs -f voc

# 加账号（路径指向持久卷 /data）
docker compose exec voc python -m web.auth --state-dir /data/web_state \
  adduser 邮箱 --role admin --password 密码

# 备份数据卷
docker compose exec voc tar czf - -C /data . > voc_data_$(date +%F).tgz
```

---

## 6. 安全

- 阿里云控制台「防火墙」**不要**开放 8080：容器端口只绑在 `127.0.0.1`，
  外部只走 Cloudflare 隧道，源站不直接暴露。
- `.env` 含密钥，已在 `.gitignore`；切勿提交。

---

## 7. 故障排查

- 登录正常但跑分析任务报错：多半是 `VOC_DEEPSEEK_API_KEY` 没填或填错。
  补到 `.env` 后 `docker compose up -d` 重启容器即可。
- 域名打不开：先 `docker compose ps` 看容器是否 running；再 `cloudflared tunnel info vocvps` 看隧道状态。
- 拉镜像慢：可给 Docker 配阿里云镜像加速（控制台搜「容器镜像服务」→ 镜像加速器）。

---

## 8. 与 Render 方案的关系

`render.yaml` 仍然有效（Render 走 Python 运行时，忽略本 Dockerfile）。
想换回 Render，只需在 Render 控制台关联同一仓库即可，互不影响。
