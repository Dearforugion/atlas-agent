# ATLAS - установка полного пентест-инструментария
$ErrorActionPreference = "Stop"

Write-Host "[atlas] Installing pentest tools..."

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[error] .venv not found. Run setup.ps1 first."
    exit 1
}

Write-Host "[atlas] pip tools..."
& ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
& ".venv\Scripts\python.exe" -m pip install --quiet wafw00f sqlmap commix xsstrike nosqlmap sslyze dirsearch

Write-Host "[atlas] nmap (Windows)..."
winget install --id Nmap.Nmap -e --silent
if ($LASTEXITCODE -ne 0) {
    Write-Host "[atlas] [warn] winget не установил nmap автоматически."
    Write-Host "[atlas] Установи вручную: winget install Nmap.Nmap  или скачай с https://nmap.org/download.html"
}

if (Get-Command go -ErrorAction SilentlyContinue) {
    Write-Host "[atlas] Go tools..."
    go install github.com/projectdiscovery/httpx/cmd/httpx@latest
    go install github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest
    go install github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest
    go install github.com/projectdiscovery/katana/cmd/katana@latest
    go install github.com/projectdiscovery/dnsx/cmd/dnsx@latest
    go install github.com/ffuf/ffuf/v2@latest
    go install github.com/OJ/gobuster/v3@latest
    go install github.com/tunnelix/waybackurls@latest
    go install github.com/lc/gau/v2/cmd/gau@latest
    go install github.com/shenwei356/paramspider/cmd/paramspider@latest
    go install github.com/feroxbuster/feroxbuster/v2@latest
    go install github.com/OWASP/Amass/v3/Amass@latest
    go install github.com/hahwul/dalfox/v2@latest
}

if (Get-Command cargo -ErrorAction SilentlyContinue) {
    Write-Host "[atlas] Rust tool (rustscan)..."
    cargo install rustscan
}

Write-Host "[atlas] Install finished."
Write-Host "[atlas] Check status: run.cmd doctor   and in chat: pentest_tools"
exit 0
