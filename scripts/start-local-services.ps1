# Start workspace-local Neo4j and stateless rule/RAG APIs. No Docker launch.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskArtifacts = Join-Path $taskRoot 'artifacts'
New-Item -ItemType Directory -Path $taskArtifacts -Force | Out-Null
$taskNeo4jListener = Get-NetTCPConnection -LocalPort 7474 -State Listen -ErrorAction SilentlyContinue
if (-not $taskNeo4jListener) {
    & (Join-Path $PSScriptRoot 'start-local-neo4j.ps1')
}
$taskRuleListener = Get-NetTCPConnection -LocalPort 8091 -State Listen -ErrorAction SilentlyContinue
if (-not $taskRuleListener) {
    $taskPython = (Get-Command python -ErrorAction Stop).Source
    $taskServer = Join-Path $taskRoot 'rule-service/server.py'
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @(('"{0}"' -f $taskServer),'--port','8091') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskArtifacts 'rule-service.log') -RedirectStandardError (Join-Path $taskArtifacts 'rule-service-error.log') -PassThru
    $taskProcess.Id | Set-Content -LiteralPath (Join-Path $taskArtifacts 'rule-service.pid')
    Write-Output "Rule API launch requested (PID $($taskProcess.Id))."
}
Write-Output 'Neo4j Browser: http://127.0.0.1:7474'
Write-Output 'Rule API health: http://127.0.0.1:8091/health'
$taskRagIndex = Join-Path $taskRoot 'data/rag/index.json'
$taskRagListener = Get-NetTCPConnection -LocalPort 8092 -State Listen -ErrorAction SilentlyContinue
if (Test-Path -LiteralPath $taskRagIndex) {
    if (-not $taskRagListener) {
        $taskRagPython = (Get-Command python -ErrorAction Stop).Source
        $taskRagServer = Join-Path $taskRoot 'rag-service/rag_server.py'
        $taskRagProcess = Start-Process -FilePath $taskRagPython -ArgumentList @(('"{0}"' -f $taskRagServer),'--port','8092') -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskArtifacts 'rag-service.log') -RedirectStandardError (Join-Path $taskArtifacts 'rag-service-error.log') -PassThru
        $taskRagProcess.Id | Set-Content -LiteralPath (Join-Path $taskArtifacts 'rag-service.pid')
        Write-Output "RAG API launch requested (PID $($taskRagProcess.Id))."
    }
    Write-Output 'RAG API health: http://127.0.0.1:8092/health'
    Write-Output 'RAG API contract: http://127.0.0.1:8092/openapi.json'
} else {
    Write-Output 'RAG index missing: run python rag-service/corpus.py before starting the RAG API.'
}
