# 适用于 Fly.io / Railway / 任意 Docker 平台
FROM python:3.13-slim

WORKDIR /app

# PDF 导出用到无头 Chromium（可选功能；缺它也不影响主流程）
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV VOC_DATA_DIR=/data \
    VOC_CHROME_PATH=/usr/bin/chromium \
    PORT=8080
RUN mkdir -p /data

EXPOSE 8080
CMD ["gunicorn", "server.app:app", "--bind", "0.0.0.0:8080", \
     "--workers", "1", "--timeout", "120", "-k", "uvicorn.workers.UvicornWorker"]
