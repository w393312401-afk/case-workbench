$ErrorActionPreference = 'Stop'
$syncWorkspace = Split-Path -Parent $PSScriptRoot
$syncConfiguration = Get-Content -LiteralPath (Join-Path $syncWorkspace 'config.json') -Raw -Encoding utf8 | ConvertFrom-Json
$syncAuthDirectory = Join-Path $syncConfiguration.runtime_root 'text-public-sync/cloudflare-auth'
$syncNode = (Get-Command node.exe).Source
$syncWrangler = Join-Path $PSScriptRoot 'node_modules/wrangler/bin/wrangler.js'
if (-not (Test-Path -LiteralPath $syncWrangler)) { throw '自动发布组件尚未安装。' }
$env:XDG_CONFIG_HOME = $syncAuthDirectory
$env:CLOUDFLARE_AUTH_USE_KEYRING = 'true'
$env:CLOUDFLARE_ACCOUNT_ID = '01267175a0eda8ddb6d390d6869ffe16'
$env:CLOUDFLARE_SEND_METRICS = 'false'
$env:WRANGLER_SEND_METRICS = 'false'
Push-Location -LiteralPath $syncWorkspace
try {
    & $syncNode $syncWrangler auth create case-workbench-text-sync --scopes account:read pages:write
    if ($LASTEXITCODE -ne 0) { throw '授权没有完成，请在浏览器完成授权后重试。' }
    Write-Output '自动同步发布授权已完成。'
} finally {
    Pop-Location
}
