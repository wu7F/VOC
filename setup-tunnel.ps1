<#
  VOC洞察平台 · 命名隧道（Named Tunnel）一键配置

  命名隧道 = 永久不变的公网地址。配好之后不管重启多少次，
  https://<子域>.<你的域名> 永远指向你这台机器。

  用法（推荐用 bat 包装器，会自动绕过执行策略）：
      配置命名隧道.bat
      配置命名隧道.bat --domain vocinsight.com --sub voc --port 8080

  前置条件：
      1. 域名已购买，且 NS 已指向 Cloudflare（控制台会给你两个 nameserver）
      2. 已在 dash.cloudflare.com 添加过这个域名（Free 计划即可）
#>

[CmdletBinding()]
param(
  [string]$Domain = "",
  [string]$Sub = "voc",
  [int]$Port = 8080,
  [string]$TunnelName = "voc",
  [switch]$SkipService
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Say($msg, $color = "Gray") { Write-Host $msg -ForegroundColor $color }
function Die($msg) { Write-Host "✗ $msg" -ForegroundColor Red; exit 1 }

# ---------- 0. 环境检查 ----------
$cf = Get-Command cloudflared -ErrorAction SilentlyContinue
if (-not $cf) {
  $guess = @(
    "C:\Program Files (x86)\cloudflared\cloudflared.exe",
    "C:\Program Files\cloudflared\cloudflared.exe",
    "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe"
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($guess) { $cfPath = $guess }
  else { Die "没找到 cloudflared。请先执行：winget install --id Cloudflare.cloudflared" }
} else { $cfPath = $cf.Source }

Say "cloudflared : $cfPath" DarkGray
& $cfPath --version | Out-Null

$cfDir  = Join-Path $env:USERPROFILE ".cloudflared"
New-Item -ItemType Directory -Force -Path $cfDir | Out-Null

# ---------- 1. 问域名 ----------
if (-not $Domain) {
  Say ""
  Say "请输入你已经买好、并且 NS 已指向 Cloudflare 的域名。" Cyan
  Say "  例如：vocinsight.com      （不要带 https:// 和路径）" DarkGray
  $Domain = (Read-Host "域名").Trim()
}
$Domain = $Domain -replace "^(https?://)?", "" -replace "/.*$", "" -replace "\s", ""
if (-not $Domain -or $Domain -notmatch "\.") { Die "域名格式不对：$Domain" }

$Hostname = "$Sub.$Domain"
Say ""
Say "目标地址   : https://$Hostname" Green

# ---------- 2. 登录授权 ----------
$cred = Get-ChildItem "$cfDir\*.json" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not (Test-Path "$cfDir\cert.pem")) {
  Say ""
  Say "即将打开浏览器，请登录你的 Cloudflare 账号并授权这个域名…" Cyan
  Start-Sleep -Milliseconds 400
  & $cfPath tunnel login
  if (-not (Test-Path "$cfDir\cert.pem")) { Die "授权未完成（没找到 cert.pem），请重试" }
  Say "授权成功" Green
} else {
  Say "已有 Cloudflare 授权，跳过登录" DarkGray
}

# ---------- 3. 创建命名隧道 ----------
$existing = (& $cfPath tunnel list 2>$null | Select-Object -Skip 1) | Where-Object { $_ -match "\s$TunnelName\s" }
if ($existing) {
  Say "隧道 $TunnelName 已存在，跳过创建" DarkGray
} else {
  Say ""
  Say "创建命名隧道 $TunnelName …" Cyan
  & $cfPath tunnel create $TunnelName
  if ($LASTEXITCODE -ne 0) { Die "创建隧道失败" }
}

# 拿到隧道 ID 与凭据文件
$tunnelId = $null; $credFile = $null
$rows = (& $cfPath tunnel list 2>$null) -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ -match "\s$TunnelName\s" }
foreach ($r in $rows) {
  $p = ($r -split "\s+") | Where-Object { $_ }
  if ($p[1] -eq $TunnelName) { $tunnelId = $p[0] }
}
if (-not $tunnelId) { Die "拿不到隧道 ID，试试 cloudflared tunnel list 查看" }
$credFile = Get-ChildItem "$cfDir\$tunnelId.json" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $credFile) { Die "找不到凭据文件 $cfDir\$tunnelId.json" }
Say "隧道 ID   : $tunnelId" DarkGray

# ---------- 4. 写 config.yml ----------
$cfg = Join-Path $cfDir "config.yml"
$yml = @"
# VOC洞察平台 · 命名隧道配置（由 配置命名隧道.bat 生成）
# 改完保存后，重启 cloudflared 生效：cloudflared tunnel run $TunnelName
tunnel: $TunnelId
credentials-file: $($credFile.FullName -replace '\\','/')

ingress:
  # 公网子域名 → 本地平台端口
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
  # 兜底规则：没有第二条 cloudflared 会拒绝启动，**不要删**
  - service: http_status:404
"@
Set-Content -Path $cfg -Value $yml -Encoding UTF8
Say "配置已写入 : $cfg" Green

# ---------- 5. 绑定 DNS ----------
Say ""
Say "绑定 DNS（$Hostname → 隧道）…" Cyan
& $cfPath tunnel route dns $TunnelName $Hostname 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }

# ---------- 6. 试跑 ----------
Say ""
Say "试运行 5 秒验证链路…" Cyan
$job = Start-Process -FilePath $cfPath `
        -ArgumentList "tunnel", "--no-autoupdate", "run", $TunnelName `
        -NoNewWindow -PassThru -RedirectStandardOutput "$cfDir\run.out" -RedirectStandardError "$cfDir\run.err"
Start-Sleep -Seconds 5
$ok = $false
try {
  $r = Invoke-WebRequest "https://$Hostname/api/health" -UseBasicParsing -TimeoutSec 20
  if ($r.StatusCode -eq 200) { $ok = $true }
} catch { }
Stop-Process -Id $job.Id -Force -ErrorAction SilentlyContinue

if ($ok) {
  Say ""
  Say "✓ 通了！你的永久公网地址：" Green
  Say "    https://$Hostname" Cyan
} else {
  Say ""
  Say "! 本机自测没通过。常见原因：" Yellow
  Say "  · 平台的 8080 端口没起来（先双击 start.bat）" DarkGray
  Say "  · DNS 还没生效（刚配好通常 1~5 分钟）" DarkGray
  Say "  · 平台和 cloudflared 不在同一台机器" DarkGray
  Say ""
  Say "  过几分钟再打开 https://$Hostname 试试。" DarkGray
}

# ---------- 7. 装成开机自启服务 ----------
Say ""
if (-not $SkipService) {
  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
              ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($isAdmin) {
    $ans = Read-Host "是否把 cloudflared 装成开机自启服务？(y/N)"
    if ($ans -eq "y") {
      & $cfPath service install $TunnelName 2>&1 | ForEach-Object { Write-Host "   $_" -ForegroundColor DarkGray }
      Start-Service cloudflared -ErrorAction SilentlyContinue
      Say "已装为 Windows 服务，开机自动运行" Green
    }
  } else {
    Say "（要开机自启，请用管理员身份运行本脚本）" DarkGray
  }
}

Say ""
Say "完成。下次启动平台：双击 start.bat 即可（隧道会由服务自动维持）。" Cyan