param(
    [string]$Python = "python",
    [int]$Jobs = 2,
    [ValidateSet("msvc", "mingw64", "auto")]
    [string]$Compiler = "msvc",
    [switch]$SkipInstall,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"
$BuildArgs = @("$PSScriptRoot/tools/build_binary.py", "--target", "windows", "--jobs", "$Jobs", "--windows-compiler", $Compiler)
if ($SkipInstall) { $BuildArgs += "--skip-install" }
if ($DryRun) { $BuildArgs += "--dry-run" }
& $Python @BuildArgs
exit $LASTEXITCODE
