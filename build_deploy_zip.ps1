$ErrorActionPreference = "Stop"
Write-Host "Criando arquivo ZIP para deploy na Square Cloud..." -ForegroundColor Cyan

$exclude = @('.git', '__pycache__', '.venv', 'env', '.env', '.cache', '.spotify_cache', 'tests', 'docs', 'data', 'logs', '*.zip', '*.db', '*.sqlite3', 'build_deploy_zip.ps1', '.agents', '.pytest_cache')

if (Test-Path "Baphomet-deploy.zip") {
    Remove-Item "Baphomet-deploy.zip" -Force
}

Get-ChildItem -Exclude $exclude | Compress-Archive -DestinationPath "Baphomet-deploy.zip" -Force

Write-Host "Arquivo Baphomet-deploy.zip gerado com sucesso!" -ForegroundColor Green
Write-Host "Basta arrastar para a aba de Upload/Commit na Square Cloud." -ForegroundColor Yellow
Pause
