param([int]$Port = 8093)
$ErrorActionPreference = 'Stop'
$viewerRoot = Split-Path -Parent $PSScriptRoot
$viewerArtifacts = Join-Path $viewerRoot 'artifacts/graph-viewer'
New-Item -ItemType Directory -Path $viewerArtifacts -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $viewerRoot 'data/processed/HI-Small-MVP/manifest.json'))) { throw 'Prepare the local demo dataset first; see docs/graph-viewer.md.' }
$viewerListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($viewerListener) {
    $viewerHealth = Invoke-RestMethod -Uri ('http://127.0.0.1:' + $Port + '/api/health') -TimeoutSec 8
    if ($viewerHealth.service -ne 'traceon-graph-viewer') { throw 'Port belongs to another service; choose -Port.' }
    Write-Output ('Graph viewer is already running: http://127.0.0.1:' + $Port)
    exit 0
}
$viewerPython = (Get-Command python -ErrorAction Stop).Source
$viewerServer = Join-Path $viewerRoot 'graph-viewer/server.py'
$viewerProcess = Start-Process -FilePath $viewerPython -ArgumentList @(('"{0}"' -f $viewerServer),'--port',$Port) -WorkingDirectory $viewerRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $viewerArtifacts 'service.log') -RedirectStandardError (Join-Path $viewerArtifacts 'service-error.log') -PassThru
$viewerProcess.Id | Set-Content -LiteralPath (Join-Path $viewerArtifacts 'service.pid')
Write-Output ('Graph viewer launch requested: http://127.0.0.1:' + $Port)
