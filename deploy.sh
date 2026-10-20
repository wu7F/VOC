#!/usr/bin/env bash
# 一键部署 VOC 到阿里云轻量应用服务器
#   支持系统：Ubuntu 20.04/22.04/24.04、Debian 11/12、Alibaba Cloud Linux 3、CentOS/RHEL 8+
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/wu7F/VOC/main/deploy.sh | bash
#   或： git clone https://github.com/wu7F/VOC.git && cd VOC && bash deploy.sh
set -euo pipefail

REPO="https://github.com/wu7F/VOC.git"
DIR="VOC"

# ---------- 0) 识别系统 ----------
OS_ID="unknown"; OS_LIKE=""
if [ -f /etc/os-release ]; then
  . /etc/os-release
  OS_ID="${ID:-unknown}"; OS_LIKE="${ID_LIKE:-}"
fi
echo "== VOC 部署脚本 =="
echo "[0/6] 系统识别: ${OS_ID} ${OS_LIKE}"

PKG=""
if command -v apt-get >/dev/null 2>&1; then
  PKG="apt"
elif command -v dnf >/dev/null 2>&1; then
  PKG="dnf"
elif command -v yum >/dev/null 2>&1; then
  PKG="yum"
fi
echo "      包管理器: ${PKG:-未识别}"

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

# 交互读取：`curl | bash` 时 stdin 是脚本本身，必须显式读 /dev/tty，否则会把脚本内容当成输入
read_tty() { # $1=提示语  $2=1 表示密码（不回显）
  local prompt="$1" secret="${2:-0}"
  if [ -r /dev/tty ]; then
    if [ "$secret" = "1" ]; then IFS= read -rsp "$prompt" REPLY </dev/tty || true
    else IFS= read -rp "$prompt" REPLY </dev/tty || true; fi
  else
    REPLY=""
  fi
}

# ---------- 1) 小内存自动加 swap（1GB 机型构建镜像时防 OOM） ----------
echo "[1/6] 检查内存 / swap..."
MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "${MEM_MB:-0}" -lt 1600 ] && [ "${SWAP_MB:-0}" -lt 512 ]; then
  echo "      物理内存 ${MEM_MB}MB，swap ${SWAP_MB}MB -> 创建 2GB swap 文件"
  if [ ! -f /swapfile ]; then
    $SUDO fallocate -l 2G /swapfile 2>/dev/null || $SUDO dd if=/dev/zero of=/swapfile bs=1M count=2048
    $SUDO chmod 600 /swapfile
    $SUDO mkswap /swapfile >/dev/null
    $SUDO swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
    echo "      swap 已启用（重启后自动挂载）"
  fi
else
  echo "      内存 ${MEM_MB}MB / swap ${SWAP_MB}MB，无需处理"
fi

# ---------- 2) 装 Docker ----------
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  echo "[2/6] Docker + compose 已存在，跳过"
else
  echo "[2/6] 安装 Docker..."
  case "$PKG" in
    apt)
      # 走阿里云镜像源，国内速度快很多
      curl -fsSL https://get.docker.com | $SUDO sh -s -- --mirror Aliyun
      ;;
    dnf|yum)
      # Alibaba Cloud Linux 3 / CentOS Stream / RHEL 8+：固定用 centos/8 的 docker-ce 源
      # 注意：ALinux3 的 $releasever 会被解析成 "3"，直接套 centos 模板会 404，
      # 因此这里直接写死 centos/8 的 baseurl，避免依赖发行版变量。
      $SUDO $PKG install -y dnf-plugins-core >/dev/null 2>&1 || true
      $SUDO tee /etc/yum.repos.d/docker-ce.repo >/dev/null <<'EOF'
[docker-ce-stable]
name=Docker CE Stable - $basearch
baseurl=https://mirrors.aliyun.com/docker-ce/linux/centos/8/$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://mirrors.aliyun.com/docker-ce/linux/centos/gpg
EOF
      $SUDO rpm --import https://mirrors.aliyun.com/docker-ce/linux/centos/gpg 2>/dev/null || true
      $SUDO $PKG install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      $SUDO systemctl enable --now docker
      ;;
    *)
      echo "!! 未识别的系统，请手动安装 Docker 后重跑，或换成 Ubuntu 22.04 镜像"
      exit 1
      ;;
  esac
  $SUDO systemctl enable --now docker 2>/dev/null || true
  $SUDO usermod -aG docker "$USER" 2>/dev/null || true
  echo "      Docker 已安装"
fi

# 拿到一个可用的 docker 命令（当前会话可能还没进 docker 组）
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if $SUDO docker info >/dev/null 2>&1; then DOCKER="$SUDO docker"; fi
fi
COMPOSE="$DOCKER compose"
if ! $COMPOSE version >/dev/null 2>&1; then
  if command -v docker-compose >/dev/null 2>&1; then COMPOSE="$SUDO docker-compose"; fi
fi

# ---------- 3) 拉代码 ----------
echo "[3/6] 获取代码..."
if [ -f web/serve.py ]; then
  # 已经是仓库根目录（在该目录里直接 bash deploy.sh）→ 不再 clone
  echo "      当前目录已是项目根目录，跳过 clone"
  if [ -d .git ]; then
    git pull --ff-only 2>/dev/null && echo "      已更新到最新代码" || echo "      跳过更新（无网络或有本地改动），继续用当前代码"
  fi
elif [ -d "$DIR/.git" ]; then
  echo "      目录 $DIR 已存在，尝试更新..."
  cd "$DIR"
  git pull --ff-only 2>/dev/null || echo "      更新失败，继续用现有代码"
elif [ -d "$DIR" ]; then
  echo "!! 目录 $DIR 已存在但不是完整仓库（缺 web/serve.py）。"
  echo "   请先清理后重跑：  cd ~ && rm -rf VOC && bash deploy.sh"
  echo "   （VOC 里只有代码；账号/任务数据在容器卷里，不受影响）"
  exit 1
else
  echo "      克隆仓库..."
  if ! git clone "$REPO" "$DIR"; then
    echo "      直接 clone 失败，尝试国内镜像 ghfast.top ..."
    if git clone "https://ghfast.top/https://github.com/wu7F/VOC.git" "$DIR"; then
      echo "      ✅ 已通过镜像拉取成功"
    else
      echo "!! git clone 失败（国内访问 GitHub 常见）。可选方案："
      echo "   1) 本机打包上传：本地执行  scp -r voc-platform root@<公网IP>:/root/VOC"
      echo "   2) 或在服务器上配置代理后重试  git config --global http.proxy http://127.0.0.1:7897"
      exit 1
    fi
  fi
  cd "$DIR"
fi
echo "      当前目录: $(pwd)"

# ---------- 4) 写 .env ----------
echo "[4/6] 配置环境变量..."
if [ ! -f .env ] || ! grep -q "VOC_DEEPSEEK_API_KEY" .env 2>/dev/null; then
  read_tty "请输入 VOC_DEEPSEEK_API_KEY（输入不回显，粘贴后按回车；直接回车可跳过）: " 1; echo
  KEY="$REPLY"
  printf 'VOC_DEEPSEEK_API_KEY=%s\n' "$KEY" > .env
  chmod 600 .env
  [ -z "$KEY" ] && echo "      已跳过：登录能进，但跑分析任务会失败，之后可在 .env 里补"
fi

# ---------- 5) 构建并启动 ----------
echo "[5/6] 构建镜像并启动容器（restart: always）..."

# 国内拉 Docker Hub 常常失败：先试直连，失败则自动配镜像加速再拉
BASE_IMG="python:3.11-slim"
if ! $DOCKER image inspect "$BASE_IMG" >/dev/null 2>&1; then
  if ! timeout 120 $DOCKER pull -q "$BASE_IMG" >/dev/null 2>&1; then
    echo "      直连 Docker Hub 失败，尝试国内镜像加速源..."
    for M in https://docker.m.daocloud.io https://docker.1ms.run https://hub.rat.dev; do
      $SUDO mkdir -p /etc/docker
      [ -f /etc/docker/daemon.json ] && $SUDO cp -n /etc/docker/daemon.json /etc/docker/daemon.json.bak 2>/dev/null || true
      printf '{\n  "registry-mirrors": ["%s"]\n}\n' "$M" | $SUDO tee /etc/docker/daemon.json >/dev/null
      $SUDO systemctl restart docker 2>/dev/null || true
      sleep 3
      if timeout 240 $DOCKER pull -q "$BASE_IMG" >/dev/null 2>&1; then
        echo "      已切换镜像源 $M 并拉取成功"
        break
      fi
      echo "      源不可用: $M"
    done
  fi
fi
if ! $DOCKER image inspect "$BASE_IMG" >/dev/null 2>&1; then
  echo "!! 基础镜像 $BASE_IMG 仍拉不下来。请在阿里云控制台/或手动配好 Docker 镜像加速后重跑本脚本。"
  exit 1
fi

$COMPOSE up -d --build

# ---------- 6) 建首账号 ----------
echo "[6/6] 创建第一个管理员账号..."
read_tty "管理员邮箱: "; EMAIL="$REPLY"
read_tty "管理员密码（输入不回显，粘贴后按回车）: " 1; echo; PW="$REPLY"
if [ -n "${EMAIL:-}" ] && [ -n "${PW:-}" ]; then
  $COMPOSE exec -T voc python -m web.auth --state-dir /data/web_state adduser "$EMAIL" --role admin --password "$PW"
else
  echo "      已跳过，稍后可手动执行："
  echo "      docker compose exec -T voc python -m web.auth --state-dir /data/web_state adduser <邮箱> --role admin --password <密码>"
fi

# ---------- 自检 ----------
echo
echo "== 自检 =="
sleep 2
$DOCKER ps --filter name=voc --format '  容器状态: {{.Status}}' || true
if curl -fsS -o /dev/null "http://127.0.0.1:8080/" 2>/dev/null; then
  echo "  本地 8080 已响应 ✅"
else
  echo "  本地 8080 暂未响应，可看日志： $DOCKER logs -f voc"
fi

echo
echo "✅ 部署完成！"
echo "  - 容器: voc（restart: always，开机自启）"
echo "  - 数据卷: voc_data（账号 / 任务 / 上传持久化，重建容器不丢）"
echo "  - 监听: 127.0.0.1:8080（仅本机回环，外部走 Cloudflare 隧道）"
echo "  - 下一步: 用 Cloudflare Tunnel 把 voc.voctyc.cn 指向 http://localhost:8080"
echo "    详见 DEPLOY_VPS.md 的「Cloudflare 隧道」一节"
