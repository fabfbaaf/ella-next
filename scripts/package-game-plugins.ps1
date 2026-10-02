$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$outputRoot = Join-Path $projectRoot "artifacts/game-plugins"
$minecraftJar = Join-Path $projectRoot "game-plugins/minecraft/build/libs/ella-minecraft-bridge-0.1.0.jar"
$stardewDll = Join-Path $projectRoot "game-plugins/stardew/bin/Release/net6.0/Ella.StardewBridge.dll"
$stardewManifest = Join-Path $projectRoot "game-plugins/stardew/manifest.json"

foreach ($path in @($minecraftJar, $stardewDll, $stardewManifest)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Missing game build input: $path"
    }
}

$manifest = Get-Content -LiteralPath $stardewManifest -Raw | ConvertFrom-Json
if ($manifest.Version -ne "0.1.0" -or $manifest.EntryDll -ne "Ella.StardewBridge.dll") {
    throw "Stardew manifest version or entry DLL does not match the built plugin."
}

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
Copy-Item -LiteralPath $minecraftJar -Destination $outputRoot -Force
$stardewFolder = Join-Path $outputRoot "Ella.StardewBridge"
New-Item -ItemType Directory -Force -Path $stardewFolder | Out-Null
Copy-Item -LiteralPath $stardewDll -Destination $stardewFolder -Force
Copy-Item -LiteralPath $stardewManifest -Destination $stardewFolder -Force
$zipPath = Join-Path $outputRoot "Ella.StardewBridge-0.1.0.zip"
Compress-Archive -LiteralPath $stardewFolder -DestinationPath $zipPath -Force

@($minecraftJar, $zipPath) | ForEach-Object {
    $file = Get-Item -LiteralPath $_
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    "$hash  $($file.Name)"
} | Set-Content -LiteralPath (Join-Path $outputRoot "SHA256SUMS.txt") -Encoding ASCII
Write-Output "Game plugins packaged in $outputRoot"
