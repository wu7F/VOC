#!/usr/bin/env bash
# 一键部署 VOC 到阿里云轻量应用服务器（Ubuntu 22.04 / 24.04）
# 用法： curl -fsSL <本文件 raw 地址> | bash
#   或： git clone https://github.com/wu7F/VOC.git && cd VOC && bash deploy.sh
set -euo pipefail

echo "== VOC 部署脚本 =="

# 1) 装 Docker（若未装）
if ! command -v docker >/dev/null 2>&1; then
  echo "[1/5] 未检测到 Docker，开始安装..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" 2>/dev/null || true
  echo "Docker 已安装。若后续提示权限不足，请退出重登终端后重跑本脚本。"
else
  echo "[1/5] Docker 已存在，跳过安装"
fi

# 2) 拉代码
echo "[2/5] 获取代码..."
REPO="https://github.com/wu7F/VOC.git"
DIR="VOC"
if [ ! -d "$DIR/.git" ]; then
  git clone "$REPO" "$DIR"
fi
cd "$DIR"

# 3) 写 .env（DeepSeek 密钥，不进仓库）
echo "[3/5] 配置环境变量..."
if [ ! -f .env ] || ! grep -q "VOC_DEEPSEEK_API_KEY" .env 2>/dev/null; then
  read -rsp "请输入 VOC_DEEPSEEK_API_KEY（你的 DeepSeek 密钥）: " KEY
  echo
  printf 'VOC_DEEPSEEK_API_KEY=%s\n' "$KEY" > .env
fi

# 4) 构建并启动
echo "[4/5] 构建镜像并启动容器（restart: always）..."
docker compose up -d --build

# 5) 建首账号
echo "[5/5] 创建第一个管理员账号..."
read -rp "管理员邮箱: " EMAIL
read -rsp "管理员密码: " PW; echo
docker compose exec -T voc python -m web.auth --state-dir /data/web_state adduser "$EMAIL" --role admin --password "$PW"

echo
echo "✅ 部署完成！"
echo "  - 容器: voc（已设自动重启）"
echo "  - 数据卷: voc_data（账号 / 任务 / 上传持久化）"
echo "  - 下一步: 用 Cloudflare Tunnel 把 voc.voctyc.cn 指向本机 http://localhost:8080"
echo "    详见 DEPLOY_VPS.md 的「Cloudflare 隧道」一节"
