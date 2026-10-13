$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BuildDir = Join-Path $ProjectRoot ".lambda-build"
$OutputZip = Join-Path $ProjectRoot "internship-finder-lambda.zip"

# Keep cleanup constrained to this project's known build directory.
if ((Split-Path -Parent $BuildDir) -ne $ProjectRoot -or (Split-Path -Leaf $BuildDir) -ne ".lambda-build") {
    throw "Refusing to clean unexpected build path: $BuildDir"
}
if (Test-Path -LiteralPath $BuildDir) {
    Remove-Item -LiteralPath $BuildDir -Recurse -Force
}
New-Item -ItemType Directory -Path $BuildDir | Out-Null
New-Item -ItemType Directory -Path (Join-Path $BuildDir "jobfinder") | Out-Null

Copy-Item -LiteralPath (Join-Path $ProjectRoot "run.py") -Destination $BuildDir
Copy-Item -LiteralPath (Join-Path $ProjectRoot "sources.toml") -Destination $BuildDir
foreach ($File in @("__init__.py", "match.py", "sources.py", "util.py")) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "jobfinder\$File") -Destination (Join-Path $BuildDir "jobfinder")
}

Compress-Archive -Path (Join-Path $BuildDir "*") -DestinationPath $OutputZip -Force
Write-Host "Created $OutputZip"
