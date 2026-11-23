param([int]$Port=8080)
# Start only TraceOn's backend; use start-local-services.ps1 for Neo4j/rule/RAG.
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskBackend=Join-Path $taskRoot 'backend'
$taskArtifacts=Join-Path $taskRoot 'artifacts/spring-backend'
if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Choose a port between 1024 and 65535.' }
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) { throw "Port $Port is already in use. Inspect the existing process." }
if (-not $env:JAVA_HOME) {
    $taskJavaHome=Join-Path $taskRoot '.tools/jdk-21.0.1'
    if (Test-Path -LiteralPath (Join-Path $taskJavaHome 'bin/java.exe')) { $env:JAVA_HOME=$taskJavaHome }
}
$taskJava=if ($env:JAVA_HOME) { Join-Path $env:JAVA_HOME 'bin/java.exe' } else { (Get-Command java -ErrorAction Stop).Source }
Push-Location $taskBackend
try {
    & (Join-Path $taskBackend 'gradlew.bat') --no-daemon bootJar
    if ($LASTEXITCODE -ne 0) { throw 'Backend build failed.' }
} finally { Pop-Location }
$taskJar=Join-Path $taskBackend 'build/libs/traceon-0.0.1-SNAPSHOT.jar'
New-Item -ItemType Directory -Path $taskArtifacts -Force | Out-Null
$taskStamp=Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$taskProcess=Start-Process -FilePath $taskJava -ArgumentList @('-jar',('"{0}"' -f $taskJar),"--server.port=$Port") -WorkingDirectory $taskBackend -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskArtifacts "$taskStamp.log") -RedirectStandardError (Join-Path $taskArtifacts "$taskStamp-error.log") -PassThru
$taskProcess.Id | Set-Content -LiteralPath (Join-Path $taskArtifacts "port-$Port.pid")
Write-Output "Spring backend launch requested (PID $($taskProcess.Id)). http://127.0.0.1:$Port/api/health"
