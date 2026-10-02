param([switch]$RuntimeOnly, [switch]$WithoutGameResources)
$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$python = Join-Path $projectRoot ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Create the project .venv first." }
$tauriRoot = Join-Path $projectRoot "apps/desktop/src-tauri"
$dist = Join-Path $tauriRoot "runtime"
$work = Join-Path $projectRoot "artifacts/runtime-build"
New-Item -ItemType Directory -Force -Path $dist,$work | Out-Null
Push-Location $projectRoot
try {
    if (-not $WithoutGameResources) {
        & $python -B (Join-Path $projectRoot "scripts/prepare-game-setup.py") --offline --tauri-resources
        if ($LASTEXITCODE -ne 0) { throw "Verified game setup resources are missing; run prepare-game-setup.py first, or use -WithoutGameResources." }
    }
    & $python -B -m PyInstaller --noconfirm --onedir --name ella-runtime --distpath $dist --workpath $work --specpath $work --collect-submodules ella_runtime --collect-data ella_runtime --collect-all playwright --hidden-import uvicorn.logging --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on --hidden-import websockets --hidden-import win32com.client --hidden-import keyring.backends.Windows run_runtime.py
    if ($LASTEXITCODE -ne 0) { throw "Runtime packaging failed." }
    $runtimeExe = Join-Path $dist "ella-runtime/ella-runtime.exe"
    if (-not (Test-Path -LiteralPath $runtimeExe -PathType Leaf)) { throw "Packaged runtime is missing." }
    $gabsSource = Join-Path $projectRoot "artifacts/game-plugins/gabs/gabs.exe"
    $resources = @("runtime/**/*")
    if (-not $WithoutGameResources) { $resources += "game-setup/**/*" }
    if (-not $WithoutGameResources -and (Test-Path -LiteralPath $gabsSource -PathType Leaf)) {
        $gabsDestination = Join-Path $tauriRoot "artifacts/game-plugins/gabs"
        New-Item -ItemType Directory -Force -Path $gabsDestination | Out-Null
        Copy-Item -LiteralPath $gabsSource -Destination $gabsDestination -Force
        $gabsLicense = Join-Path $projectRoot "scripts/licenses/GABS-MIT.txt"
        if (-not (Test-Path -LiteralPath $gabsLicense -PathType Leaf)) { throw "GABS redistribution license is missing." }
        Copy-Item -LiteralPath $gabsLicense -Destination (Join-Path $gabsDestination "LICENSE.txt") -Force
        # Machine-specific credentials/configuration stay in local application data.
        $resources += "artifacts/game-plugins/gabs/gabs.exe"
        $resources += "artifacts/game-plugins/gabs/LICENSE.txt"
    }
    $bundleJson = @{bundle=@{resources=$resources;targets=@("nsis")}} | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $tauriRoot "tauri.bundle.conf.json"), $bundleJson, [Text.UTF8Encoding]::new($false))
    if (-not $RuntimeOnly) {
        & pnpm --filter '@ella/desktop' tauri build --config (Join-Path $tauriRoot "tauri.bundle.conf.json")
        if ($LASTEXITCODE -ne 0) { throw "Desktop packaging failed." }
    }
    Write-Output "Packaged runtime: $runtimeExe"
} finally { Pop-Location }
