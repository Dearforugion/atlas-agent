# ATLAS - installer of the pentest toolkit for Windows.
# Everything user-level is installed into .\tools and .\.venv.
# Tools that exist only on Linux are reported at the end.

[CmdletBinding()]
param(
    [switch]$SkipRuby,
    [switch]$SkipWordlists
)

$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch {}

$root = $PSScriptRoot
if (-not $root) { $root = (Get-Location).Path }
Set-Location $root

$toolsDir = Join-Path $root 'tools'
$venvDir = Join-Path $root '.venv'
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
$venvScripts = Join-Path $venvDir 'Scripts'
$goBin = Join-Path $env:USERPROFILE 'go\bin'
$nmapDirs = @('C:\Program Files (x86)\Nmap', 'C:\Program Files\Nmap')
$gitBin = 'C:\Program Files\Git\bin'
$gitUsrBin = 'C:\Program Files\Git\usr\bin'
$sevenZip = 'C:\Program Files\7-Zip\7z.exe'
$rubyDir = Join-Path $toolsDir 'ruby'

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $goBin | Out-Null

function Write-Step([string]$text) {
    Write-Host ''
    Write-Host "== $text ==" -ForegroundColor Cyan
}
function Write-Ok([string]$text) { Write-Host "  [ok] $text" -ForegroundColor Green }
function Write-Warn([string]$text) { Write-Host "  [!] $text" -ForegroundColor Yellow }
function Write-Bad([string]$text) { Write-Host "  [x] $text" -ForegroundColor Red }

function Have([string]$name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Add-PathDir([string]$dir) {
    if (-not $dir) { return }
    if (-not (Test-Path -LiteralPath $dir)) { return }
    try { $dir = (Resolve-Path -LiteralPath $dir).Path.TrimEnd('\') } catch { return }
    $key = $dir.ToLowerInvariant()

    $current = @($env:Path -split ';' | Where-Object { $_ -and $_.TrimEnd('\').ToLowerInvariant() -ne $key })
    $env:Path = $dir + ';' + ($current -join ';')

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $userPath) { $userPath = '' }
    $userParts = @($userPath -split ';' | Where-Object { $_ })
    $userKeys = @($userParts | ForEach-Object { $_.TrimEnd('\').ToLowerInvariant() })
    if ($userKeys -notcontains $key) {
        [Environment]::SetEnvironmentVariable('Path', (($userParts + $dir) -join ';'), 'User')
    }
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (($machine, $user) -join ';')
    foreach ($dir in @($venvScripts, $toolsDir, $goBin, $rubyDir + '\bin', $gitBin) + $nmapDirs) {
        Add-PathDir $dir
    }
}

Refresh-Path

function Install-Winget([string]$id, [string]$command) {
    if ($command -and (Have $command)) {
        Write-Ok "$command already installed"
        return $true
    }
    if (-not (Have 'winget')) {
        Write-Warn 'winget not found - install App Installer from Microsoft Store'
        return $false
    }
    Write-Host "  [..] winget install $id" -ForegroundColor Gray
    $output = & winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1
    $code = $LASTEXITCODE
    Refresh-Path
    if ($command -and (Have $command)) {
        Write-Ok "$command installed"
        return $true
    }
    if ($code -eq 0) {
        Write-Warn "$id installed, but $command is not visible yet (open a new terminal)"
        return $true
    }
    if ($output) {
        $tail = @($output | Select-Object -Last 4) -join ' | '
        Write-Warn "winget $id failed: $tail"
    } else {
        Write-Warn "winget $id failed (exit $code)"
    }
    return $false
}

function Ensure-Venv {
    if (Test-Path $venvPy) {
        Write-Ok "Python venv: $venvDir"
        return $true
    }
    Write-Step 'Python virtual environment'
    $created = $false
    if (Have 'py') {
        & py -3.12 -m venv $venvDir 2>&1 | Out-Null
        $created = Test-Path $venvPy
    }
    if (-not $created -and (Have 'python')) {
        & python -m venv $venvDir 2>&1 | Out-Null
        $created = Test-Path $venvPy
    }
    if (-not $created) {
        Write-Warn 'Python 3.12 not found - trying winget'
        Install-Winget 'Python.Python.3.12' 'python' | Out-Null
        if (Have 'python') {
            & python -m venv $venvDir 2>&1 | Out-Null
            $created = Test-Path $venvPy
        }
    }
    if (-not $created) {
        Write-Bad 'Could not create .venv. Install Python 3.12+ and rerun.'
        return $false
    }
    Add-PathDir $venvScripts
    Write-Ok "venv created: $venvDir"
    return $true
}

function Test-LocalCommand([string]$name) {
    if (Have $name) { return $true }
    foreach ($ext in @('.exe', '.cmd', '.bat', '')) {
        if (Test-Path -LiteralPath (Join-Path $venvScripts ($name + $ext))) { return $true }
    }
    return $false
}

function Install-PipPackage([string]$package, [string]$command) {
    if ($command -and (Test-LocalCommand $command)) {
        Write-Ok "$command already installed"
        return $true
    }
    Write-Host "  [..] pip install $package" -ForegroundColor Gray
    $output = & $venvPy -m pip install --upgrade --no-input --disable-pip-version-check --quiet $package 2>&1
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Add-PathDir $venvScripts
        Write-Ok "$package installed"
        return $true
    }
    $tail = @($output | Select-Object -Last 5) -join ' | '
    Write-Warn "$package failed: $tail"
    return $false
}

function Install-GitHubZip([string]$repo, [string]$assetPattern, [string]$exeName) {
    $target = Join-Path $toolsDir $exeName
    if (Test-Path -LiteralPath $target) {
        Write-Ok "$exeName already installed"
        return $true
    }
    Write-Host "  [..] download $repo ($exeName)" -ForegroundColor Gray
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/releases/latest" `
            -Headers @{ 'User-Agent' = 'atlas-installer' } -TimeoutSec 30
        $asset = @($release.assets | Where-Object { $_.name -like $assetPattern })[0]
        if (-not $asset) {
            Write-Warn "$repo : no asset matching $assetPattern"
            return $false
        }
        $zip = Join-Path $env:TEMP ("atlas-" + $asset.name)
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing -TimeoutSec 300
        $tmp = Join-Path $env:TEMP ("atlas-unpack-" + [guid]::NewGuid().ToString('N'))
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $found = @(Get-ChildItem -Path $tmp -Recurse -File -Filter $exeName)[0]
        if (-not $found) {
            Write-Warn "$repo : $exeName not found in archive"
            return $false
        }
        Copy-Item -LiteralPath $found.FullName -Destination $target -Force
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue
        Write-Ok "$exeName installed to $target"
        return $true
    } catch {
        Write-Warn "$repo download failed: $($_.Exception.Message)"
        return $false
    }
}

function Ensure-Git {
    if (Have 'git') { return $true }
    Install-Winget 'Git.Git' 'git' | Out-Null
    return (Have 'git')
}

function Install-GitHubRepo([string]$repo, [string]$destName) {
    $dest = Join-Path $toolsDir $destName
    if (Test-Path -LiteralPath $dest) {
        $items = @(Get-ChildItem -LiteralPath $dest -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -ne '.git' })
        if ($items.Count -gt 0) {
            Write-Ok "$destName already cloned"
            return $dest
        }
        # A failed clone can leave an empty .git behind; retry it.
        Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Ensure-Git)) {
        Write-Warn "git is required for $destName"
        return $null
    }
    Write-Host "  [..] git clone $repo" -ForegroundColor Gray
    $output = & git clone --depth 1 $repo $dest 2>&1
    $items = @(Get-ChildItem -LiteralPath $dest -Force -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -ne '.git' })
    if ($LASTEXITCODE -eq 0 -and $items.Count -gt 0) {
        Write-Ok "$destName cloned"
        return $dest
    }
    $tail = @($output | Select-Object -Last 4) -join ' | '
    Write-Warn "$destName clone failed: $tail"
    Remove-Item -LiteralPath $dest -Recurse -Force -ErrorAction SilentlyContinue
    return $null
}

function Write-Wrapper([string]$name, [string]$content) {
    $path = Join-Path $toolsDir $name
    $text = $content -replace "`r?`n", "`r`n"
    [IO.File]::WriteAllText($path, $text, (New-Object System.Text.ASCIIEncoding))
}

function Get-RubyExe {
    $cmd = Get-Command 'ruby.exe' -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    if (Test-Path -LiteralPath $rubyDir) {
        $found = @(Get-ChildItem -Path $rubyDir -Recurse -File -Filter 'ruby.exe' -ErrorAction SilentlyContinue |
            Sort-Object FullName)[0]
        if ($found) { return $found.FullName }
    }
    return $null
}

function Ensure-Ruby {
    $existing = Get-RubyExe
    if ($existing) {
        Add-PathDir (Split-Path -Parent $existing)
        Write-Ok "ruby already installed: $existing"
        return $true
    }
    if ($SkipRuby) {
        Write-Warn 'Ruby skipped (-SkipRuby); WhatWeb will not be installed'
        return $false
    }
    Write-Step 'Ruby (for WhatWeb)'
    $seven = $null
    if (Test-Path -LiteralPath $sevenZip) { $seven = $sevenZip }
    elseif (Have '7z') { $seven = '7z' }
    if (-not $seven) {
        Write-Warn '7-Zip not found - install Ruby manually from rubyinstaller.org'
        return $false
    }
    try {
        $releases = Invoke-RestMethod -Uri 'https://api.github.com/repos/oneclick/rubyinstaller2/releases?per_page=40' `
            -Headers @{ 'User-Agent' = 'atlas-installer' } -TimeoutSec 30
        $release = @($releases | Where-Object { $_.tag_name -like 'RubyInstaller-3.3.*' })[0]
        if (-not $release) { $release = @($releases)[0] }
        $asset = @($release.assets | Where-Object { $_.name -like '*-x64.7z' })[0]
        if (-not $asset) {
            Write-Warn 'No portable Ruby x64 archive found'
            return $false
        }
        $archive = Join-Path $env:TEMP $asset.name
        Write-Host "  [..] download $($asset.name)" -ForegroundColor Gray
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $archive -UseBasicParsing -TimeoutSec 300
        if (Test-Path -LiteralPath $rubyDir) {
            Remove-Item -LiteralPath $rubyDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        New-Item -ItemType Directory -Force -Path $rubyDir | Out-Null
        & $seven x $archive ("-o" + $rubyDir) -y 2>&1 | Out-Null
        $rubyExe = @(Get-ChildItem -Path $rubyDir -Recurse -File -Filter 'ruby.exe' | Sort-Object FullName)[0]
        if (-not $rubyExe) {
            Write-Warn 'ruby.exe not found after extraction'
            return $false
        }
        Add-PathDir $rubyExe.DirectoryName
        Add-PathDir (Join-Path $rubyExe.DirectoryName '..\bin')
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        Write-Ok "Ruby installed: $($rubyExe.DirectoryName)"
        return $true
    } catch {
        Write-Warn "Ruby installation failed: $($_.Exception.Message)"
        return $false
    }
}

function Install-Gem([string]$gem, [string]$command) {
    if ($command -and (Have $command)) {
        Write-Ok "$command already installed"
        return $true
    }
    if (-not (Have 'gem')) { return $false }
    Write-Host "  [..] gem install $gem" -ForegroundColor Gray
    $output = & gem install $gem --no-document 2>&1
    if ($LASTEXITCODE -eq 0) {
        Refresh-Path
        Write-Ok "$gem installed"
        return $true
    }
    $tail = @($output | Select-Object -Last 5) -join ' | '
    Write-Warn "$gem failed: $tail"
    return $false
}

# ---------------------------------------------------------------------------
# 1. Python tools
# ---------------------------------------------------------------------------
if (Ensure-Venv) {
    Write-Step 'Python pentest tools'
    & $venvPy -m pip install --upgrade --no-input --disable-pip-version-check pip 2>&1 | Out-Null
    $pipTools = @(
        @('wafw00f', 'wafw00f'),
        @('sqlmap', 'sqlmap'),
        @('dirsearch', 'dirsearch'),
        @('sslyze', 'sslyze'),
        @('arjun', 'arjun'),
        @('dnsrecon', 'dnsrecon'),
        @('fierce', 'fierce'),
        @('xsstrike', 'xsstrike')
    )
    foreach ($item in $pipTools) {
        Install-PipPackage $item[0] $item[1] | Out-Null
    }
    Add-PathDir $venvScripts
}

# ---------------------------------------------------------------------------
# 2. Native Windows packages (winget)
# ---------------------------------------------------------------------------
Write-Step 'Windows packages'
Install-Winget 'Insecure.Nmap' 'nmap' | Out-Null
if (-not (Have 'git')) { Install-Winget 'Git.Git' 'git' | Out-Null }

# ---------------------------------------------------------------------------
# 3. Go tools
# ---------------------------------------------------------------------------
Write-Step 'Go tools'
if (-not (Have 'go')) {
    Install-Winget 'GoLang.Go' 'go' | Out-Null
}
if (Have 'go') {
    Add-PathDir $goBin
    $env:GOBIN = $goBin
    $goTools = @(
        @('github.com/projectdiscovery/httpx/cmd/httpx@latest', 'httpx'),
        @('github.com/projectdiscovery/nuclei/v3/cmd/nuclei@latest', 'nuclei'),
        @('github.com/projectdiscovery/subfinder/v2/cmd/subfinder@latest', 'subfinder'),
        @('github.com/projectdiscovery/katana/cmd/katana@latest', 'katana'),
        @('github.com/projectdiscovery/dnsx/cmd/dnsx@latest', 'dnsx'),
        @('github.com/projectdiscovery/naabu/v2/cmd/naabu@latest', 'naabu'),
        @('github.com/ffuf/ffuf/v2@latest', 'ffuf'),
        @('github.com/OJ/gobuster/v3@latest', 'gobuster'),
        @('github.com/tomnomnom/waybackurls@latest', 'waybackurls'),
        @('github.com/lc/gau/v2/cmd/gau@latest', 'gau'),
        @('github.com/hahwul/dalfox/v2@latest', 'dalfox'),
        @('github.com/owasp-amass/amass/v4/cmd/amass@latest', 'amass')
    )
    foreach ($item in $goTools) {
        $module = $item[0]
        $command = $item[1]
        $exe = Join-Path $goBin ($command + '.exe')
        if (Test-Path -LiteralPath $exe) {
            Write-Ok "$command already installed"
            continue
        }
        Write-Host "  [..] go install $module" -ForegroundColor Gray
        $output = & go install $module 2>&1
        if ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $exe)) {
            Write-Ok "$command installed"
        } else {
            $tail = @($output | Select-Object -Last 5) -join ' | '
            Write-Warn "$command failed: $tail"
        }
    }
    $nuclei = Join-Path $goBin 'nuclei.exe'
    if (Test-Path -LiteralPath $nuclei) {
        Write-Host '  [..] updating nuclei templates' -ForegroundColor Gray
        try {
            $p = Start-Process -FilePath $nuclei -ArgumentList @('-update-templates', '-silent') -NoNewWindow -PassThru
            if (-not $p.WaitForExit(120000)) {
                try { $p.Kill() } catch {}
            }
        } catch {}
    }
} else {
    Write-Warn 'Go not installed - ProjectDiscovery tools skipped'
}

# ---------------------------------------------------------------------------
# 4. Standalone Windows binaries
# ---------------------------------------------------------------------------
Write-Step 'Standalone binaries'
Install-GitHubZip 'epi052/feroxbuster' '*windows*feroxbuster*.zip' 'feroxbuster.exe' | Out-Null
Install-GitHubZip 'rbsec/sslscan' 'sslscan-*.zip' 'sslscan.exe' | Out-Null
Install-GitHubZip 'bee-san/RustScan' '*x86_64-windows*rustscan*.zip' 'rustscan.exe' | Out-Null

# ---------------------------------------------------------------------------
# 5. Repositories that run through Python / Perl / Bash wrappers
# ---------------------------------------------------------------------------
Write-Step 'Wrapper tools'

$xsstrike = Install-GitHubRepo 'https://github.com/s0md3v/XSStrike.git' 'xsstrike'
if ($xsstrike) {
    $req = Join-Path $xsstrike 'requirements.txt'
    if (Test-Path -LiteralPath $req) {
        & $venvPy -m pip install --no-input --disable-pip-version-check --quiet -r $req 2>&1 | Out-Null
    }
    $wrapper = '@echo off' + "`r`n" +
        'setlocal' + "`r`n" +
        'cd /d "%~dp0xsstrike"' + "`r`n" +
        '"' + $venvPy + '" xsstrike.py %*' + "`r`n" +
        'endlocal & exit /b %ERRORLEVEL%' + "`r`n"
    Write-Wrapper 'xsstrike.cmd' $wrapper
    Write-Ok 'xsstrike.cmd wrapper'
}

$commix = Install-GitHubRepo 'https://github.com/commixproject/commix.git' 'commix'
if ($commix) {
    $wrapper = '@echo off' + "`r`n" +
        'setlocal' + "`r`n" +
        'cd /d "%~dp0commix"' + "`r`n" +
        '"' + $venvPy + '" commix.py %*' + "`r`n" +
        'endlocal & exit /b %ERRORLEVEL%' + "`r`n"
    Write-Wrapper 'commix.cmd' $wrapper
    Write-Ok 'commix.cmd wrapper'
}

$paramspider = Install-GitHubRepo 'https://github.com/devanshbatham/ParamSpider.git' 'paramspider'
if ($paramspider) {
    # Repo v2 has a package entry point (paramspider.main:main), not paramspider.py.
    & $venvPy -m pip install --no-input --disable-pip-version-check --quiet -e $paramspider 2>&1 | Out-Null
    Add-PathDir $venvScripts
    if (Test-LocalCommand 'paramspider') {
        Write-Ok 'paramspider installed'
    } else {
        Write-Warn 'paramspider install failed'
    }
}

$nikto = Install-GitHubRepo 'https://github.com/sullo/nikto.git' 'nikto'
if ($nikto) {
    # Git Perl ships without XML::Writer, and Nikto refuses to start without it.
    # Vendor the pure-Perl module into tools\perl5 and expose it via PERL5LIB.
    $perl5 = Join-Path $toolsDir 'perl5'
    $writer = Join-Path $perl5 'XML\Writer.pm'
    if (-not (Test-Path -LiteralPath $writer)) {
        Write-Host '  [..] XML::Writer (Nikto dependency)' -ForegroundColor Gray
        try {
            $tgz = Join-Path $env:TEMP 'atlas-XML-Writer.tar.gz'
            $tmp = Join-Path $env:TEMP ('atlas-xmlwriter-' + [guid]::NewGuid().ToString('N'))
            Invoke-WebRequest -Uri 'https://cpan.metacpan.org/authors/id/J/JO/JOSEPHW/XML-Writer-0.900.tar.gz' `
                -OutFile $tgz -UseBasicParsing -TimeoutSec 120
            New-Item -ItemType Directory -Force -Path $tmp | Out-Null
            & tar -xzf $tgz -C $tmp 2>&1 | Out-Null
            $found = @(Get-ChildItem -Path $tmp -Recurse -File -Filter 'Writer.pm' |
                Where-Object { $_.FullName -match 'XML-Writer' })[0]
            if ($found) {
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $writer) | Out-Null
                Copy-Item -LiteralPath $found.FullName -Destination $writer -Force
                Write-Ok 'XML::Writer installed to tools\perl5'
            } else {
                Write-Warn 'XML::Writer archive did not contain Writer.pm'
            }
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $tgz -Force -ErrorAction SilentlyContinue
        } catch {
            Write-Warn "XML::Writer download failed: $($_.Exception.Message)"
        }
    } else {
        Write-Ok 'XML::Writer already installed'
    }
    $perl = Join-Path $gitUsrBin 'perl.exe'
    if (-not (Test-Path -LiteralPath $perl)) { $perl = 'perl' }
    $wrapper = '@echo off' + "`r`n" +
        'setlocal' + "`r`n" +
        'set "PERL5LIB=%~dp0perl5"' + "`r`n" +
        'cd /d "%~dp0nikto\program"' + "`r`n" +
        '"' + $perl + '" nikto.pl %*' + "`r`n" +
        'endlocal & exit /b %ERRORLEVEL%' + "`r`n"
    Write-Wrapper 'nikto.cmd' $wrapper
    Write-Ok 'nikto.cmd wrapper (Git Perl)'
}

$testssl = Install-GitHubRepo 'https://github.com/testssl/testssl.sh.git' 'testssl'
if ($testssl) {
    $script = Join-Path $testssl 'testssl.sh'
    if (Test-Path -LiteralPath $script) {
        # Git for Windows may checkout CRLF; bash needs LF in the shebang.
        $text = [IO.File]::ReadAllText($script) -replace "`r`n", "`n"
        [IO.File]::WriteAllText($script, $text, (New-Object System.Text.UTF8Encoding($false)))
    }

    # Git Bash lacks hexdump, which testssl.sh requires. Provide a small shim
    # backed by xxd and put it first on PATH for the wrapper.
    $shimDir = Join-Path $toolsDir 'testssl-bin'
    New-Item -ItemType Directory -Force -Path $shimDir | Out-Null
    $shimText = @'
#!/usr/bin/env bash
set -eu
canonical=0
format=""
file=""
while [ $# -gt 0 ]; do
  case "$1" in
    -C) canonical=1; shift ;;
    -v) shift ;;
    -e) format="$2"; shift 2 ;;
    -e*) format="${1#-e}"; shift ;;
    -*) shift ;;
    *) file="$1"; shift ;;
  esac
done
if [ -n "$file" ] && [ "$file" != "-" ]; then
  if [ ! -r "$file" ]; then
    echo "hexdump: $file: No such file or directory" >&2
    exit 1
  fi
  if [ "$canonical" = 1 ]; then
    xxd "$file"
  else
    hex=$(xxd -p "$file" | tr -d '\n')
    case "$format" in *%02X*) printf '%s' "$hex" | tr 'a-f' 'A-F' ;; *) printf '%s' "$hex" ;; esac
  fi
else
  if [ "$canonical" = 1 ]; then
    xxd
  else
    hex=$(xxd -p | tr -d '\n')
    case "$format" in *%02X*) printf '%s' "$hex" | tr 'a-f' 'A-F' ;; *) printf '%s' "$hex" ;; esac
  fi
fi
'@
    $shimPath = Join-Path $shimDir 'hexdump'
    [IO.File]::WriteAllText($shimPath, ($shimText -replace "`r`n", "`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Ok 'hexdump shim for testssl.sh'

    $bash = Join-Path $gitBin 'bash.exe'
    if (-not (Test-Path -LiteralPath $bash)) { $bash = 'bash' }
    # Git Bash treats backslashes in a Windows path as escapes, so cd into the
    # checkout and use the relative script name instead.
    $wrapper = '@echo off' + "`r`n" +
        'setlocal' + "`r`n" +
        'set "MSYS2_ARG_CONV_EXCL=*"' + "`r`n" +
        'set "PATH=%~dp0testssl-bin;%PATH%"' + "`r`n" +
        'cd /d "%~dp0testssl"' + "`r`n" +
        '"' + $bash + '" testssl.sh %*' + "`r`n" +
        'endlocal & exit /b %ERRORLEVEL%' + "`r`n"
    Write-Wrapper 'testssl.cmd' $wrapper
    Write-Ok 'testssl.cmd wrapper (Git Bash)'
}

$nosqlmap = Install-GitHubRepo 'https://github.com/codingo/NoSQLMap.git' 'nosqlmap'
if ($nosqlmap) {
    $script = Join-Path $nosqlmap 'nosqlmap.py'
    if (Test-Path -LiteralPath $script) {
        # NoSQLMap is Python 2 code. Install its deps and convert the checkout
        # to Python 3 in place when 2to3 is available.
        & $venvPy -m pip install --no-input --disable-pip-version-check --quiet `
            couchdb requests pbkdf2 pymongo ipcalc 2>&1 | Out-Null
        & $venvPy -m py_compile $script 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host '  [..] converting NoSQLMap from Python 2 to Python 3' -ForegroundColor Gray
            & $venvPy -m lib2to3 -w -n $nosqlmap 2>&1 | Out-Null
            Get-ChildItem -Path $nosqlmap -Recurse -File -Filter '*.bak' -ErrorAction SilentlyContinue |
                Remove-Item -Force -ErrorAction SilentlyContinue
            & $venvPy -m py_compile $script 2>&1 | Out-Null
        }
        Get-ChildItem -Path $nosqlmap -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
        if ($LASTEXITCODE -eq 0) {
            $wrapper = '@echo off' + "`r`n" +
                'setlocal' + "`r`n" +
                'cd /d "%~dp0nosqlmap"' + "`r`n" +
                '"' + $venvPy + '" nosqlmap.py %*' + "`r`n" +
                'endlocal & exit /b %ERRORLEVEL%' + "`r`n"
            Write-Wrapper 'nosqlmap.cmd' $wrapper
            Write-Ok 'nosqlmap.cmd wrapper (Python 3 converted)'
        } else {
            Write-Warn 'nosqlmap skipped: Python 3 conversion failed (use Docker/WSL)'
        }
    }
}

# ---------------------------------------------------------------------------
# 6. Ruby tools (WhatWeb, WPScan)
# ---------------------------------------------------------------------------
if (Ensure-Ruby) {
    Write-Step 'Ruby tools'
    $rubyExe = Get-RubyExe
    $whatwebRepo = Install-GitHubRepo 'https://github.com/urbanadventurer/WhatWeb.git' 'whatweb'
    if ($whatwebRepo -and $rubyExe) {
        # The whatweb gem is stale; the git checkout plus addressable is current.
        Install-Gem 'addressable' $null | Out-Null
        $script = Join-Path $whatwebRepo 'whatweb'
        if (Test-Path -LiteralPath $script) {
            $wrapper = '@echo off' + "`r`n" +
                'setlocal' + "`r`n" +
                '"' + $rubyExe + '" "' + $script + '" %*' + "`r`n" +
                'endlocal & exit /b %ERRORLEVEL%' + "`r`n"
            Write-Wrapper 'whatweb.cmd' $wrapper
            Write-Ok 'whatweb.cmd wrapper'
        } else {
            Write-Warn 'WhatWeb checkout is missing the whatweb script'
        }
    }

    if (Have 'wpscan') {
        Write-Ok 'wpscan already installed'
    } else {
        # yajl-ruby has no precompiled x64-mingw-ucrt gem, so native gem install
        # fails without an MSYS2/DevKit toolchain. Do not attempt it every run.
        Write-Warn 'wpscan skipped: yajl-ruby needs MSYS2/DevKit on native Windows'
        Write-Host '        Install MSYS2 and run "ridk install" (option 3), then "gem install wpscan".' -ForegroundColor DarkGray
        Write-Host '        Or run WPScan inside WSL/Kali: gem install wpscan' -ForegroundColor DarkGray
    }
}

# ---------------------------------------------------------------------------
# 7. PATH, wordlists and final status
# ---------------------------------------------------------------------------
Add-PathDir $venvScripts
Add-PathDir $toolsDir
Add-PathDir $goBin
foreach ($dir in $nmapDirs) { Add-PathDir $dir }

# PyPI httpx (HTTP client) installs httpx.exe, which shadows ProjectDiscovery
# httpx. The library stays for theHarvester, but its CLI must go.
$goHttpx = Join-Path $goBin 'httpx.exe'
$venvHttpx = Join-Path $venvScripts 'httpx.exe'
if ((Test-Path -LiteralPath $goHttpx) -and (Test-Path -LiteralPath $venvHttpx)) {
    Remove-Item -LiteralPath $venvHttpx -Force -ErrorAction SilentlyContinue
    Write-Ok 'removed PyPI httpx.exe (ProjectDiscovery httpx is used)'
}

# wfuzz 2.4.x uses removed Python 3.12 modules (imp, collections.MutableMapping).
# It is not installable on this interpreter; remove a previously broken copy.
if (Test-Path -LiteralPath (Join-Path $venvScripts 'wfuzz.exe')) {
    & $venvPy -m pip uninstall -y wfuzz 2>&1 | Out-Null
    Write-Warn 'wfuzz removed: it does not support Python 3.12 (use WSL/Linux)'
}

if (-not $SkipWordlists) {
    Write-Step 'Wordlists'
    $wordlistDir = Join-Path $root 'workspace\wordlists'
    New-Item -ItemType Directory -Force -Path $wordlistDir | Out-Null
    $common = Join-Path $wordlistDir 'common.txt'
    if (-not (Test-Path -LiteralPath $common)) {
        try {
            Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/danielmiessler/SecLists/master/Discovery/Web-Content/common.txt' `
                -OutFile $common -UseBasicParsing -TimeoutSec 60
            Write-Ok "common.txt downloaded"
        } catch {
            Write-Warn 'common.txt download failed (run installer again later)'
        }
    } else {
        Write-Ok "common.txt already present"
    }
}

Write-Step 'Installed tools'
$check = @(
    'nmap', 'sqlmap', 'nuclei', 'ffuf', 'gobuster', 'dirsearch', 'feroxbuster',
    'subfinder', 'httpx', 'nikto', 'whatweb', 'wafw00f', 'dalfox', 'xsstrike',
    'amass', 'dnsx', 'testssl', 'sslscan', 'wpscan', 'katana', 'naabu', 'gau',
    'waybackurls', 'commix', 'paramspider', 'sslyze', 'arjun', 'dnsrecon',
    'fierce', 'rustscan', 'nosqlmap', 'python', 'git'
)
$missing = @()
foreach ($name in $check) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) {
        Write-Host ("  [+] " + $name.PadRight(14) + $command.Source) -ForegroundColor Green
    } else {
        $missing += $name
        Write-Host ("  [-] " + $name.PadRight(14) + "not installed") -ForegroundColor DarkYellow
    }
}

Write-Host ''
if ($missing.Count -eq 0) {
    Write-Host 'All toolkit components are ready.' -ForegroundColor Green
} else {
    Write-Host ('Missing: ' + ($missing -join ', ')) -ForegroundColor Yellow
}
Write-Host ''
Write-Host 'Not available natively on Windows (use WSL/Kali): hydra, masscan, joomscan.' -ForegroundColor DarkGray
Write-Host 'WPScan: install MSYS2 and run "ridk install" (option 3), then "gem install wpscan".' -ForegroundColor DarkGray
Write-Host 'Metasploit: download https://windows.metasploit.com/metasploitframework-latest.msi (admin/UAC).' -ForegroundColor DarkGray
Write-Host 'Open a new terminal if a just-installed tool is not visible in the current one.' -ForegroundColor DarkGray
Write-Host ''
