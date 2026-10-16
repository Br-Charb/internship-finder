$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $ProjectRoot ".commands-build"
$OutputZip = Join-Path $ProjectRoot "internship-finder-commands.zip"

# Keep cleanup constrained to this project's known build directory.
if ((Split-Path -Parent $BuildDir) -ne $ProjectRoot -or (Split-Path -Leaf $BuildDir) -ne ".commands-build") {
    throw "Refusing to clean unexpected build path: $BuildDir"
}
if (Test-Path -LiteralPath $BuildDir) {
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Path (Join-Path $BuildDir "jobfinder") | Out-Null

Copy-Item -LiteralPath (Join-Path $ProjectRoot "discord_commands.py") -Destination $BuildDir
Copy-Item -LiteralPath (Join-Path $ProjectRoot "sources.toml") -Destination $BuildDir
foreach ($File in @("__init__.py", "notifications.py")) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "jobfinder\$File") -Destination (Join-Path $BuildDir "jobfinder")
}

# Build Linux x86_64 wheels even when this script runs on Windows. AWS Lambda
# supplies boto3, so only the Discord signature-verification dependency is bundled.
$PythonExe = "python"
$PythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonExe = "py"
    $PythonPrefix = @("-3")
}
& $PythonExe @PythonPrefix -m pip install `
    --requirement (Join-Path $ProjectRoot "requirements-discord-commands.txt") `
    --target $BuildDir `
    --platform manylinux2014_x86_64 `
    --implementation cp `
    --python-version 3.13 `
    --abi cp313 `
    --only-binary=:all: `
    --upgrade

Compress-Archive -Path (Join-Path $BuildDir "*") -DestinationPath $OutputZip -Force
Write-Host "Created $OutputZip"
