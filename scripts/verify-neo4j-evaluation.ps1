[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')]
    [string]$RunName = ('v2-' + (Get-Date -Format 'yyyyMMdd-HHmmss')),
    [ValidateRange(17474,17574)]
    [int]$Port = 17474
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$source = Join-Path $root '.tools/neo4j-community-5.26.32'
$jdk = Join-Path $root '.tools/jdk-21.0.1'
$java = Join-Path $jdk 'bin/java.exe'
if (-not (Test-Path -LiteralPath $java) -or -not (Test-Path -LiteralPath (Join-Path $source 'lib'))) {
    throw 'Prepare Neo4j 5.26.32 and JDK 21 using the local project setup.'
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw 'Evaluation port is already in use.'
}
$output = Join-Path $root "artifacts/neo4j-evaluation/$RunName"
if (Test-Path -LiteralPath $output) { throw 'Choose a new run name to keep the previous run.' }
New-Item -ItemType Directory -Path $output | Out-Null
$check = Join-Path $PSScriptRoot 'verify-neo4j-evaluation.py'
& python $check prepare --output $output
if ($LASTEXITCODE -ne 0) { throw 'Evaluation data preparation failed.' }

# The Neo4j Windows startup tooling is sensitive to Unicode paths.
$neoHome = Join-Path $output 'neo4j'
New-Item -ItemType Directory -Path $neoHome,(Join-Path $neoHome 'conf') | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'lib') -Destination $neoHome -Recurse
Copy-Item -Path (Join-Path $source 'conf/*.xml') -Destination (Join-Path $neoHome 'conf')
$alias = Join-Path $env:TEMP ('traceon-eval-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Junction -Path $alias -Target $output | Out-Null
$runtime = Join-Path $alias 'neo4j'
$conf = Join-Path $runtime 'conf'
$classpath = Join-Path $runtime 'lib/*'
@"
server.default_listen_address=127.0.0.1
server.http.enabled=true
server.http.listen_address=127.0.0.1:$Port
server.bolt.enabled=false
server.memory.heap.initial_size=256m
server.memory.heap.max_size=640m
server.memory.pagecache.size=256m
dbms.usage_report.enabled=false
db.transaction.timeout=30s
"@ | Set-Content -LiteralPath (Join-Path $conf 'neo4j.conf') -Encoding utf8NoBOM
$random = [byte[]]::new(24)
[Security.Cryptography.RandomNumberGenerator]::Fill($random)
$environment = @{
    JAVA_HOME = $jdk
    NEO4J_HOME = $runtime
    NEO4J_CONF = $conf
    TRACEON_EVAL_PASSWORD = [Convert]::ToHexString($random)
}
$previousEnvironment = @{}
$process = $null
try {
    foreach ($name in $environment.Keys) {
        $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
    }
    $admin = @('-cp', $classpath, "-Dbasedir=$runtime", 'org.neo4j.server.startup.Neo4jAdminCommand')
    & $java @admin dbms set-initial-password $env:TRACEON_EVAL_PASSWORD
    if ($LASTEXITCODE -ne 0) { throw 'Could not initialize the evaluation database.' }
    $nodes = Join-Path $alias 'accounts.csv'
    $relationships = Join-Path $alias 'transfers.csv'
    $importReport = Join-Path $alias 'import.report'
    & $java @admin database import full "--nodes=$nodes" "--relationships=$relationships" "--report-file=$importReport" '--max-off-heap-memory=256M' neo4j
    if ($LASTEXITCODE -ne 0) { throw 'Evaluation bulk import failed.' }
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw 'Evaluation port became occupied during import.'
    }
    $javaArgs = @('-Xms256m','-Xmx640m','-cp',('"' + $classpath + '"'),
        'org.neo4j.server.CommunityEntryPoint',('--home-dir="' + $runtime + '"'),
        ('--config-dir="' + $conf + '"'),'--console-mode')
    $process = Start-Process -FilePath $java -ArgumentList $javaArgs -WorkingDirectory $runtime -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $output 'neo4j.log') -RedirectStandardError (Join-Path $output 'neo4j-error.log')
    & python $check compare --output $output --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Neo4j evaluation comparison failed. Check the run report and logs.' }
}
finally {
    if ($null -ne $process -and -not $process.HasExited) {
        Stop-Process -InputObject $process
        if (-not $process.WaitForExit(5000)) { Write-Warning 'Evaluation Neo4j is still stopping.' }
    }
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process')
    }
}
