# 构建可安装的插件包
#
# 用法：
#   pwsh tools/build-package.ps1
#   pwsh tools/build-package.ps1 -Version 1.2.3
#
# 产物：dist/maibot-group-loopguard-<version>.zip
# 解压到 <MaiBot>/plugins/ 即可。

[CmdletBinding()]
param(
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pluginDir = Join-Path $repoRoot "plugins/maibot-group-loopguard"
$manifestPath = Join-Path $pluginDir "_manifest.json"

if (-not (Test-Path $manifestPath)) {
    throw "未找到 manifest：$manifestPath"
}

if (-not $Version) {
    $manifest = Get-Content $manifestPath -Raw -Encoding utf8 | ConvertFrom-Json
    $Version = $manifest.version
}

# 清掉解释器缓存，避免打进包体
Get-ChildItem $pluginDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force

$distDir = Join-Path $repoRoot "dist"
New-Item -ItemType Directory -Force -Path $distDir | Out-Null
$zipPath = Join-Path $distDir ("maibot-group-loopguard-{0}.zip" -f $Version)

if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}

Compress-Archive -Path $pluginDir -DestinationPath $zipPath -CompressionLevel Optimal

$sizeKb = (Get-Item $zipPath).Length / 1KB
Write-Host ("已生成 {0}（{1:N1} KB）" -f $zipPath, $sizeKb)
Write-Host "安装：unzip 到 <MaiBot>/plugins/ 后重载插件"
