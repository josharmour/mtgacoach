# Compile the current source with the local Arena references, then stage only
# our DLL and a hash manifest. Does not deploy into Arena or start any app.
param(
    [Parameter(Mandatory = $true)][string]$MtgaDir,
    [string]$Dotnet = "dotnet",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Project = Join-Path $RepoRoot "bepinex-plugin\MtgaCoachBridge\MtgaCoachBridge.csproj"
$CoreDir = Join-Path $RepoRoot "src\arenamcp\resources\bepinex\BepInEx\core"
$ManagedDir = Join-Path $MtgaDir "MTGA_Data\Managed"
$Verifier = Join-Path $RepoRoot "scripts\verify_bridge_release.py"
$Output = Join-Path $RepoRoot ("dist\bridge-build-" + [guid]::NewGuid().ToString("N"))
$References = [ordered]@{}
foreach ($Name in @("Assembly-CSharp.dll", "Core.dll", "SharedClientCore.dll", "Wizards.MDN.GreProtobuf.dll", "UnityEngine.dll", "UnityEngine.CoreModule.dll", "UnityEngine.PhysicsModule.dll", "Newtonsoft.Json.dll", "Google.Protobuf.dll")) {
    $References[$Name] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $ManagedDir $Name)).Hash.ToLowerInvariant()
}
foreach ($Name in @("BepInEx.dll", "0Harmony.dll")) {
    $References[$Name] = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $CoreDir $Name)).Hash.ToLowerInvariant()
}
$SourcesBefore = & $Python $Verifier --source-hashes
if ($LASTEXITCODE -ne 0) { throw "Cannot fingerprint bridge source" }
$Compiler = & $Dotnet --version
if ($LASTEXITCODE -ne 0) { throw "Cannot identify .NET SDK" }
New-Item -ItemType Directory -Path $Output | Out-Null
& $Dotnet build $Project -c Release --no-incremental --output $Output "-p:MtgaDir=$MtgaDir" "-p:BepInExDir=$CoreDir" -p:ContinuousIntegrationBuild=true 2>&1 | Tee-Object -FilePath (Join-Path $Output "build.log")
if ($LASTEXITCODE -ne 0) { throw "Bridge compilation failed; staged DLL was not changed" }
$SourcesAfter = & $Python $Verifier --source-hashes
if ($LASTEXITCODE -ne 0 -or $SourcesBefore -ne $SourcesAfter) { throw "Bridge sources changed during build" }
$BuiltDll = Join-Path $Output "MtgaCoachBridge.dll"
$Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BuiltDll).Hash.ToLowerInvariant()
$ResourceDir = Join-Path $RepoRoot "src\arenamcp\resources"
Copy-Item -LiteralPath $BuiltDll -Destination (Join-Path $ResourceDir "MtgaCoachBridge.dll") -Force
$Manifest = [ordered]@{
    schema_version = 1
    built_at_utc = [DateTime]::UtcNow.ToString("o")
    compiler = "dotnet $Compiler"
    source_sha256 = ($SourcesBefore | ConvertFrom-Json)
    reference_sha256 = $References
    dll_sha256 = $Hash
}
$Manifest | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $ResourceDir "MtgaCoachBridge.build.json")
& $Python $Verifier
if ($LASTEXITCODE -ne 0) { throw "Staged bridge verification failed" }
Write-Host "Fresh bridge staged; build evidence: $Output"
Write-Host "No running Arena DLL was changed."
