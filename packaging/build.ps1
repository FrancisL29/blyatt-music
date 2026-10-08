# Empaqueta la app de escritorio: PyInstaller (carpeta) + Inno Setup (instalador .exe).
#   powershell -File packaging\build.ps1 -Version 1.2.3 [-Python py]
# Resultado: build\installer\Blyatt-Setup-1.2.3.exe  (lo usa .github/workflows/release.yml)
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Invoke-WebRequest es lentisimo con la barra de progreso
$root = Split-Path $PSScriptRoot -Parent
$bin = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force $bin | Out-Null

function Get-Checked($url, $out, $sha) {
    Invoke-WebRequest $url -OutFile $out -UseBasicParsing
    if ($sha -and (Get-FileHash $out -Algorithm SHA256).Hash -ne $sha.ToUpper()) { throw "checksum incorrecto: $url" }
}

# ffmpeg (remux del audio HLS de canciones explicitas/restringidas)
if (-not (Test-Path "$bin\ffmpeg.exe")) {
    $u = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    $sha = (Invoke-WebRequest "$u.sha256" -UseBasicParsing).Content.Trim().Split()[0]
    $zip = Join-Path $env:TEMP "blyatt-ffmpeg.zip"
    Get-Checked $u $zip $sha
    $x = Join-Path $env:TEMP "blyatt-ffmpeg"
    Remove-Item -Recurse -Force $x -ErrorAction SilentlyContinue
    Expand-Archive $zip $x
    Copy-Item (Get-ChildItem $x -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName "$bin\ffmpeg.exe"
}
# node LTS (resuelve el reto JS de YouTube con yt-dlp-ejs)
if (-not (Test-Path "$bin\node.exe")) {
    $idx = Invoke-RestMethod "https://nodejs.org/dist/index.json"   # PS 5.1 no desenrolla el array en el pipe
    $v = ($idx | ForEach-Object { $_ } | Where-Object { $_.lts } | Select-Object -First 1).version
    $sums = (Invoke-WebRequest "https://nodejs.org/dist/$v/SHASUMS256.txt" -UseBasicParsing).Content
    $sha = ($sums -split "`n" | Where-Object { $_ -match "  win-x64/node.exe$" }).Split(" ")[0]
    Get-Checked "https://nodejs.org/dist/$v/win-x64/node.exe" "$bin\node.exe" $sha
}
# instalador de WebView2 (solo se ejecuta en PCs que no lo tienen)
if (-not (Test-Path "$bin\MicrosoftEdgeWebview2Setup.exe")) {
    Get-Checked "https://go.microsoft.com/fwlink/p/?LinkId=2124703" "$bin\MicrosoftEdgeWebview2Setup.exe" $null
}

Push-Location $root
try {
    Set-Content "_build_version.py" "VERSION = `"$Version`"" -Encoding ascii
    & $Python -m PyInstaller --noconfirm --clean --windowed --name Blyatt `
        --icon "$root\assets\blyatt.ico" `
        --distpath "$root\build\dist" --workpath "$root\build\work" --specpath "$root\build" `
        --add-data "$root\index.html;." --add-data "$root\kara.html;." --add-data "$root\assets;assets" `
        --add-data "$bin\ffmpeg.exe;bin" --add-data "$bin\node.exe;bin" `
        --collect-data ytmusicapi --collect-all yt_dlp_ejs --collect-binaries onnxruntime `
        --exclude-module tkinter `
        "$root\main.py"
    if ($LASTEXITCODE) { throw "PyInstaller fallo" }
} finally {
    Remove-Item "_build_version.py" -ErrorAction SilentlyContinue
    Pop-Location
}

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { Write-Warning "Inno Setup 6 no instalado: queda solo build\dist\Blyatt"; exit 0 }
& $iscc /Q "/DAppVer=$Version" "$PSScriptRoot\blyatt.iss"
if ($LASTEXITCODE) { throw "Inno Setup fallo" }
Write-Host "OK: build\installer\Blyatt-Setup-$Version.exe"
