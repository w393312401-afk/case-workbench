$ErrorActionPreference = 'Stop'
$syncWorkspace = Split-Path -Parent $PSScriptRoot
$syncPython = (Get-Command python.exe -ErrorAction Stop).Source
$syncSettingsJson = & $syncPython -X utf8 -c "import json,sys; sys.path.insert(0, sys.argv[1]); from app_config import load_config; from deployment.sync_text_site import publish_settings; print(json.dumps({'config':load_config(sys.argv[1]),'destination':publish_settings(sys.argv[1])}))" $syncWorkspace
if ($LASTEXITCODE -ne 0) { throw '请先在 config.local.json 配置自己的 text_public_sync 发布目标。' }
$syncSettings = $syncSettingsJson | ConvertFrom-Json
$syncConfiguration = $syncSettings.config
$syncAuthDirectory = Join-Path $syncConfiguration.runtime_root 'text-public-sync/cloudflare-auth'
$syncNode = (Get-Command node.exe).Source
$syncWrangler = Join-Path $PSScriptRoot 'node_modules/wrangler/bin/wrangler.js'
if (-not (Test-Path -LiteralPath $syncWrangler)) { throw '自动发布组件尚未安装。' }
$env:XDG_CONFIG_HOME = $syncAuthDirectory
$env:CLOUDFLARE_AUTH_USE_KEYRING = 'true'
$env:CLOUDFLARE_ACCOUNT_ID = $syncSettings.destination.account_id
$env:CLOUDFLARE_SEND_METRICS = 'false'
$env:WRANGLER_SEND_METRICS = 'false'
Push-Location -LiteralPath $syncWorkspace
try {
    & $syncNode $syncWrangler auth create $syncSettings.destination.auth_profile --scopes account:read pages:write
    if ($LASTEXITCODE -ne 0) { throw '授权没有完成，请在浏览器完成授权后重试。' }
    Write-Output '自动同步发布授权已完成。'
} finally {
    Pop-Location
}
