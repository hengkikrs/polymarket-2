$ErrorActionPreference = "Stop"

$src = "C:\Users\Lenovo\Documents\Github\poly v2"
$dst = "C:\Users\Lenovo\Documents\Github\Poly V3"

if (Test-Path -LiteralPath $dst) {
    throw "Target already exists: $dst"
}

robocopy $src $dst /E /XD .git .venv __pycache__ runtime_data logs .pytest_cache /XF *.pyc /NFL /NDL /NJH /NJS /NP
$rc = $LASTEXITCODE
if ($rc -ge 8) {
    exit $rc
}

$dash = Join-Path $dst "web\dashboard.py"
$txt = Get-Content -Raw -LiteralPath $dash
$txt = [regex]::Replace($txt, '(?m)^PORT\s*=.*$', 'PORT = int(os.getenv("DASH_PORT", "5004"))', 1)
Set-Content -NoNewline -Encoding UTF8 -LiteralPath $dash -Value $txt

$envPath = Join-Path $dst ".env"
if (Test-Path -LiteralPath $envPath) {
    $envTxt = Get-Content -Raw -LiteralPath $envPath
    if ($envTxt -match '(?m)^DASH_PORT=') {
        $envTxt = [regex]::Replace($envTxt, '(?m)^DASH_PORT=.*$', 'DASH_PORT=5004')
    } else {
        $envTxt = $envTxt.TrimEnd() + "`r`nDASH_PORT=5004`r`n"
    }

    if ($envTxt -match '(?m)^MOCK_MODE=') {
        $envTxt = [regex]::Replace($envTxt, '(?m)^MOCK_MODE=.*$', 'MOCK_MODE=true')
    } else {
        $envTxt = "MOCK_MODE=true`r`n" + $envTxt
    }
    Set-Content -NoNewline -Encoding UTF8 -LiteralPath $envPath -Value $envTxt
}

New-Item -ItemType Directory -Force -Path (Join-Path $dst "runtime_data"), (Join-Path $dst "logs") | Out-Null
Write-Output "Poly V3 copy completed"
