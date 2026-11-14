# Optional workspace-only fallback when Docker is unavailable.
# Expects official distributions under .tools; never changes system Java/services.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskNeo4j = Join-Path $taskRoot '.tools/neo4j-community-5.26.32'
$taskJava = Join-Path $taskRoot '.tools/jdk-21.0.1'
if (-not (Test-Path -LiteralPath (Join-Path $taskNeo4j 'bin/neo4j.bat'))) { throw 'Neo4j distribution is missing under .tools' }
if (-not (Test-Path -LiteralPath (Join-Path $taskJava 'bin/java.exe'))) { throw 'JDK 21 distribution is missing under .tools' }
# Neo4j's Windows launcher cannot parse this host's Unicode workspace path.
# A task-specific temporary junction keeps all runtime/data files in .tools.
$taskHashBytes = [System.Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($taskRoot))
$taskSuffix = [Convert]::ToHexString($taskHashBytes).Substring(0,12)
$taskAlias = Join-Path $env:TEMP "traceon-runtime-$taskSuffix"
if (-not (Test-Path -LiteralPath $taskAlias)) {
    New-Item -ItemType Junction -Path $taskAlias -Target (Join-Path $taskRoot '.tools') | Out-Null
}
if ((Get-Item -LiteralPath $taskAlias).Target -ne (Join-Path $taskRoot '.tools')) { throw 'Runtime alias points to another directory' }
$taskNeo4j = Join-Path $taskAlias 'neo4j-community-5.26.32'
$taskJava = Join-Path $taskAlias 'jdk-21.0.1'
if (Get-NetTCPConnection -LocalPort 7474,7687 -State Listen -ErrorAction SilentlyContinue) { throw 'Neo4j ports are already in use; inspect the existing database first.' }
$taskEnvFile = Join-Path $taskRoot '.env'
if (-not (Test-Path -LiteralPath $taskEnvFile)) {
    $taskRandom = [byte[]]::new(24)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($taskRandom)
    $taskPassword = [Convert]::ToBase64String($taskRandom).Replace('+','-').Replace('/','_')
    "NEO4J_PASSWORD=$taskPassword`nNEO4J_USER=neo4j`nNEO4J_DATABASE=neo4j`nNEO4J_URI=bolt://localhost:7687`nSERVER_PORT=8080`n" | Set-Content -LiteralPath $taskEnvFile -Encoding utf8NoBOM
}
$taskLine = Get-Content -LiteralPath $taskEnvFile | Where-Object { $_ -match '^NEO4J_PASSWORD=' } | Select-Object -First 1
$taskPassword = ($taskLine -split '=',2)[1]
if ($taskPassword.Length -lt 8) { throw 'NEO4J_PASSWORD must have at least 8 characters' }
$env:JAVA_HOME = $taskJava
$taskConfig = @'
server.default_listen_address=127.0.0.1
server.http.enabled=true
server.http.listen_address=127.0.0.1:7474
server.bolt.enabled=true
server.bolt.listen_address=127.0.0.1:7687
server.memory.heap.initial_size=256m
server.memory.heap.max_size=512m
server.memory.pagecache.size=256m
db.transaction.timeout=15s
'@
$taskConfig | Set-Content -LiteralPath (Join-Path $taskNeo4j 'conf/neo4j.conf') -Encoding utf8NoBOM
if (-not (Test-Path -LiteralPath (Join-Path $taskNeo4j 'data/dbms/auth.ini'))) {
    & (Join-Path $taskNeo4j 'bin/neo4j-admin.bat') dbms set-initial-password $taskPassword
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize Neo4j credentials' }
}
$taskConsole = Join-Path $taskNeo4j 'console.log'
$taskErrors = Join-Path $taskNeo4j 'console-error.log'
$taskCommand = '""{0}" console"' -f (Join-Path $taskNeo4j 'bin/neo4j.bat')
$taskProcess = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d','/s','/c',$taskCommand) -WorkingDirectory $taskNeo4j -WindowStyle Hidden -RedirectStandardOutput $taskConsole -RedirectStandardError $taskErrors -PassThru
Write-Output "Neo4j launch requested (PID $($taskProcess.Id)). Logs: $taskConsole"
