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

`render.yaml` 已预置 **Starter 常驻档 + 1GB 持久盘**（挂载到 `/data`，并把状态/任务/上传目录指到盘上），照下面走即可一键部署：

1. 把本仓库推到 GitHub（空仓库）。
2. Render 新建 Web Service → 关联该仓库 → Branch `main` → Runtime `Python 3`。
3. Build：`pip install -r requirements.txt`，Start：`python web/serve.py --host 0.0.0.0 --port $PORT`（与 `render.yaml` 一致，可留空让 Render 读 yaml）。
4. 环境变量：仓库已带 `VOC_WEB_AUTH=1`；**还需在控制台补 `VOC_DEEPSEEK_API_KEY=<你的密钥>`**（`sync:false`，不会进仓库）。
5. 部署后，打开 Render 的 **Shell**，建第一个账号（路径要指到持久盘）：

   ```bash
   python -m web.auth --state-dir /data/web_state adduser 你的邮箱 --role admin --password 你的密码
   ```

   建完不用重启，服务会自动热加载新账号。
6. （可选）把域名（如 `voc.voctyc.cn`）CNAME 指向 Render 给的地址，停掉本机 `cloudflared` 即可。

> 若改用免费档：把 `render.yaml` 的 `plan` 改回 `free`，并**删掉** `disk` 与三个 `VOC_WEB_*_DIR` 环境变量（免费档无持久盘，设了也白设，且重启会清空账号/任务/上传）。
> 想用 Railway 等也是同样的环境变量与启动命令。
