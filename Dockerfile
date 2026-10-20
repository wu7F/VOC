# VOC 洞察平台 · 容器镜像
# 内核零依赖（仅标准库 + 延迟导入的 openpyxl），Web 依赖见 requirements.txt。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码（用 .dockerignore 排除无关目录）
COPY . .

# 持久化目录指到 /data：由 compose 的 named volume 挂载，容器重建也不丢
ENV VOC_WEB_STATE_DIR=/data/web_state \
    VOC_WEB_RUNS_DIR=/data/runs \
    VOC_WEB_UPLOAD_DIR=/data/uploads \
    VOC_WEB_AUTH=1

EXPOSE 8080

# 单容器同时跑 Web + worker 子进程（serve.py 默认 spawn worker）
CMD ["python", "web/serve.py", "--host", "0.0.0.0", "--port", "8080"]
