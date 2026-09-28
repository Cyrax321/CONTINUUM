# CONTINUUM - one-command demo for Windows PowerShell.
# Run:  powershell -ExecutionPolicy Bypass -File .\try-it.ps1
# Modes match try-it.sh: demo (default), test, cli ..., shell
#
# Bootstraps from a fresh clone (issue #281): creates .venv when missing and
# installs the package with its dev extra, so the README's "from a clone"
# promise holds without manual setup steps. uv is used when available (it is
# what the README recommends); python -m venv + pip is the fallback.
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Root = $PSScriptRoot
Set-Location $Root

function Exit-IfNativeFailed {
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

$venvScripts = Join-Path $Root ".venv\Scripts"
$venvPython = Join-Path $venvScripts "python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "no .venv found; creating one"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & uv venv
        Exit-IfNativeFailed
    } else {
        & python -m venv .venv
        Exit-IfNativeFailed
    }
}

if ($venvPython -and (Test-Path $venvPython)) {
    $env:PATH = "$venvScripts;$env:PATH"
}
$python = if (Test-Path $venvPython) { $venvPython } else { "python" }

# Install when the venv cannot import the package yet. Runs against the
# freshly created venv and is skipped on later invocations, so the demo
# stays one command while an existing environment is left untouched.
# The probe runs with EAP=Continue: on Windows PowerShell 5.1 a native
# command writing to stderr under a stderr redirect with EAP=Stop is a
# terminating NativeCommandError, and a failed import is the expected case
# here, not an error.
$ErrorActionPreference = "Continue"
& $python -c "import continuum" 2>$null
$installed = ($LASTEXITCODE -eq 0)
$ErrorActionPreference = "Stop"
if (-not $installed) {
    Write-Host "installing continuum-agent (dev extra)"
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) {
        & uv pip install -e ".[dev]"
    } else {
        & $python -m pip install -e ".[dev]"
    }
    Exit-IfNativeFailed
}

$env:PYTHONPATH = Join-Path $Root "src"

$mode = if ($args.Count -gt 0) { $args[0] } else { "demo" }

switch ($mode) {
    "demo" {
        & $python (Join-Path $Root "examples\crash_recovery_agent.py")
        Exit-IfNativeFailed
    }
    "test" {
        & $python -m pytest
        Exit-IfNativeFailed
    }
    "cli" {
        $cliArgs = @()
        if ($args.Count -gt 1) {
            $cliArgs = $args[1..($args.Count - 1)]
        }
        & $python -m continuum.cli @cliArgs
        Exit-IfNativeFailed
    }
    "shell" {
        Write-Host "PATH and PYTHONPATH set. Try: continuum --help"
        $shell = if (Get-Command pwsh -ErrorAction SilentlyContinue) { "pwsh" } else { "powershell" }
        & $shell -NoExit
        Exit-IfNativeFailed
    }
    default {
        Write-Host "usage: .\try-it.ps1 [demo|test|cli ...|shell]"
        exit 1
    }
}
