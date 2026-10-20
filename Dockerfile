# VOC洞察平台 · 容器镜像（阿里云轻量 / VPS / Fly.io / Railway 通用）
FROM python:3.13-slim

WORKDIR /app

# PDF 导出需要无头 Chromium，但它会让镜像大 ~300MB、装包也慢。
# 默认不装：前端会自动回退到「浏览器打印成 PDF」，功能不丢。
# 需要服务端直接出 PDF 时：docker compose build --build-arg INSTALL_CHROMIUM=1
ARG INSTALL_CHROMIUM=0
RUN if [ "$INSTALL_CHROMIUM" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends chromium \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV VOC_DATA_DIR=/data \
    PORT=8080
RUN mkdir -p /data

EXPOSE 8080

# 默认用 uvicorn（单进程、启动快、日志直观）；
# 也可以用 gunicorn 起多 worker——但本应用带后台线程，单进程更稳。
CMD ["python", "web/serve.py", "--host", "0.0.0.0", "--port", "8080", "--no-browser"]
