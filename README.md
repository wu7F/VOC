# VOC 洞察平台

多源评论导出 → 标签 → 看板与洞察报告的通用 VOC 平台，含 Web 服务（`web/`）与内核（`voc_core/`）。

## 本地运行

```bash
pip install -e ".[web,xlsx]"
python web/serve.py            # 默认 127.0.0.1:8080
```

账号（可选）：有 `web_state/users.json` 即开启登录，否则免登录。

```bash
python -m web.auth --state-dir web_state adduser 你的邮箱 --role admin --password 你的密码
```

## 部署到 Render（关掉本机也能用）

1. 把本仓库推到 GitHub（空仓库）。
2. Render 新建 Web Service → 关联该仓库。
3. Build：`pip install -r requirements.txt`，Start：`python web/serve.py --host 0.0.0.0 --port $PORT`。
4. 环境变量：`VOC_WEB_AUTH=1`、`VOC_DEEPSEEK_API_KEY=<你的密钥>`。
5. 部署后，Render 的 Shell 里执行上面 `adduser` 命令建第一个账号。
6. （可选）挂持久盘到 `/data`，并设置 `VOC_WEB_STATE_DIR=/data/web_state` 等，保证账号/任务不随重启丢失。

> 免费档会休眠、重启会清空本地状态；要常驻和持久化请选 paid 档或改用 Railway。
