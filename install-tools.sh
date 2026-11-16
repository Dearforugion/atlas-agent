#!/bin/bash
# ATLAS - установка полного пентест-инструментария (Linux)
set -e

echo "[atlas] Installing pentest tools..."

if [ ! -x ".venv/bin/python" ]; then
  echo "[error] .venv not found. Run setup.ps1 first."
  exit 1
fi

echo "[atlas] pip tools..."
".venv/bin/python" -m pip install --quiet -r requirements.txt
".venv/bin/python" -m pip install --quiet wafw00f sqlmap commix xsstrike dalfox nosqlmap sslyze dirsearch

echo "[atlas] apt tools..."
apt-get install -y nmap nikto dnsrecon fierce theharvester sslscan > /dev/null 2>&1 || true

if command -v go >/dev/null 2>&1; then
  echo "[atlas] Go tools..."
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
fi

if command -v cargo >/dev/null 2>&1; then
  echo "[atlas] Rust tool (rustscan)..."
  cargo install rustscan
fi

echo "[atlas] Install finished."
echo "[atlas] Check status: python -m agent doctor   and in chat: pentest_tools"
