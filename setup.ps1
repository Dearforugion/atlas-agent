# Создание личного окружения ATLAS и проверка установки.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

chcp 65001 | Out-Null
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Создаю личное окружение .venv ..." -ForegroundColor Cyan
    py -3.12 -m venv .venv
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        python -m venv .venv
    }
}

# config.json мог быть сохранён PowerShell 5.1 с BOM — убираем его,
# чтобы JSON читался любыми инструментами.
$configPath = Join-Path $PSScriptRoot "config.json"
if (Test-Path $configPath) {
    $bytes = [IO.File]::ReadAllBytes($configPath)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $text = [Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3)
        [IO.File]::WriteAllText($configPath, $text, [Text.UTF8Encoding]::new($false))
        Write-Host "config.json: BOM удалён" -ForegroundColor Green
    }
}

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Не удалось создать .venv. Установи Python 3.12+." -ForegroundColor Red
    exit 1
}

Write-Host "Окружение готово: $PSScriptRoot\.venv" -ForegroundColor Green
& .\.venv\Scripts\python.exe -m pip install --upgrade pip --quiet
if (Test-Path "requirements.txt") {
    & .\.venv\Scripts\python.exe -m pip install -r requirements.txt --quiet
}
& .\.venv\Scripts\python.exe -m agent selftest
if ($LASTEXITCODE -ne 0) {
    Write-Host "Автотест не пройден — проверь сообщения выше." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Дальше:" -ForegroundColor Cyan
Write-Host "  1. Подключи модель (см. README.md, раздел о подключении модели)"
Write-Host "  2. Запусти: .\run.cmd"
Write-Host "  3. Диагностика модели: .\run.cmd doctor"