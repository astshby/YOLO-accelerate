# One guarded native PDS flow; outputs stay in the fresh staged project.
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [string]$PdsHome = $env:PDS_HOME,
    [ValidateRange(1,4)][double]$JobLimitGiB = 4,
    [ValidateRange(4,64)][int]$ReserveGiB = 6,
    [ValidateRange(60,3600)][int]$TimeoutSeconds = 1800
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProjectRoot)
if ($root.StartsWith('\\') -or $root -match '[{}"\r\n]') { throw 'Use a native Windows project path.' }
$manifest = Get-Content -Raw -LiteralPath (Join-Path $root 'manifest.json') | ConvertFrom-Json
if ($manifest.schema -notin @('npu128-pango-synthesis-v1', 'adam-upstream-pango-v1', 'npu128-pango-implementation-v1', 'npu128-pango-uart-implementation-v1', 'npu128-pango-uart100-implementation-v1')) { throw 'Unexpected manifest.' }
$projectName = 'npu128_post'
if ($manifest.schema -eq 'adam-upstream-pango-v1') { $projectName = 'adam_upstream' }
$flowName = 'synthesis'
$scriptName = 'synthesize.tcl'
$logName = 'synthesis.log'
$actions = @('synthesize')
if ($manifest.schema -in @('npu128-pango-implementation-v1', 'npu128-pango-uart-implementation-v1', 'npu128-pango-uart100-implementation-v1')) {
    $projectName = 'npu128_impl'
    if ($manifest.schema -eq 'npu128-pango-uart-implementation-v1') { $projectName = 'npu128_uart' }
    if ($manifest.schema -eq 'npu128-pango-uart100-implementation-v1') { $projectName = 'npu128_uart100' }
    $flowName = 'implementation'
    $scriptName = 'implement.tcl'
    $logName = 'implementation.log'
    $actions = @('compile', 'synthesize', 'dev_map', 'place', 'route', 'route_optimize', 'report_timing')
}
$topName = $manifest.top
if ($topName -notmatch '^[A-Za-z_][A-Za-z_0-9]*$') { throw 'Invalid top module.' }
foreach ($file in $manifest.files.PSObject.Properties) {
    $path = [IO.Path]::GetFullPath((Join-Path $root $file.Name))
    if (-not $path.StartsWith($root.TrimEnd('\')+'\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Manifest file escapes the project.'
    }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $file.Value) {
        throw "Staged source hash mismatch: $($file.Name)"
    }
}
if ([string]::IsNullOrWhiteSpace($PdsHome)) { throw 'Set PDS_HOME or pass -PdsHome.' }
$exe = Join-Path $PdsHome 'bin\pds_shell.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Missing pds_shell.exe.' }
foreach ($name in @("$projectName.pds", $logName, 'guard.json', 'guard-samples.jsonl')) {
    if (Test-Path -LiteralPath (Join-Path $root $name)) { throw "Preserve existing $name; stage a fresh project." }
}
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -match '^(pds|pds_shell|dw|ads|ppc|ppp|pne|evp|synthesize|synplify|synplify_pro)\.exe$'
})
if ($running.Count) { throw "Another PDS/synthesis process is active: $($running.ProcessId -join ',')" }

function Read-Memory {
    $c = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    if ($null -eq $c.AvailableBytes -or $null -eq $c.CommittedBytes -or
        $null -eq $c.CommitLimit -or $c.CommitLimit -le $c.CommittedBytes) {
        throw 'Windows memory accounting unavailable; refusing to continue.'
    }
    return [ordered]@{ timestamp = (Get-Date -Format o); available_bytes = [uint64]$c.AvailableBytes;
        free_commit_bytes = ([uint64]$c.CommitLimit-[uint64]$c.CommittedBytes) }
}
$before = Read-Memory
$threshold = [uint64](($ReserveGiB+$JobLimitGiB)*1GB)
if ($before.available_bytes -lt $threshold -or $before.free_commit_bytes -lt $threshold) {
    throw "Need $($ReserveGiB+$JobLimitGiB) GiB physical and commit headroom before starting."
}
# Environment changes are process-local. Native PDS spawns executables using PATHEXT.
if (($env:PATHEXT -split ';') -notcontains '.EXE') { $env:PATHEXT = '.EXE;.COM;.BAT;.CMD;'+$env:PATHEXT }
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
Add-Type -Path (Join-Path $root 'tools\pango\GuardedJob.cs')
$command = '-work_dir "'+$root+'" -project_name '+$projectName+' -file "'+(Join-Path $root $scriptName)+'"'
$receipt = [ordered]@{ status = 'running'; before = $before; job_limit_bytes = [uint64]($JobLimitGiB*1GB);
    reserve_bytes = [uint64]($ReserveGiB*1GB); timeout_seconds = $TimeoutSeconds;
    flow = $flowName; command = ('"'+$exe+'" '+$command); manifest_sha256 = (Get-FileHash (Join-Path $root 'manifest.json')).Hash.ToLowerInvariant();
    peak_job_commit_bytes = [uint64]0; minimum_available_bytes = $before.available_bytes;
    minimum_free_commit_bytes = $before.free_commit_bytes }
$job = $null
$timer = [Diagnostics.Stopwatch]::StartNew()
try {
    $job = New-Object NpuGuardedJob($exe, $command, $root, (Join-Path $root $logName), $receipt.job_limit_bytes)
    $receipt.root_pid = $job.RootPid
    Write-Output "Started guarded NPU $flowName PID $($job.RootPid); cap=$JobLimitGiB GiB, reserve=$ReserveGiB GiB."
    do {
        $sample = Read-Memory
        $sample.active_job_processes = $job.ActiveProcesses()
        $sample.peak_job_commit_bytes = $job.PeakCommitBytes()
        $receipt.peak_job_commit_bytes = $sample.peak_job_commit_bytes
        $receipt.minimum_available_bytes = [math]::Min($receipt.minimum_available_bytes, $sample.available_bytes)
        $receipt.minimum_free_commit_bytes = [math]::Min($receipt.minimum_free_commit_bytes, $sample.free_commit_bytes)
        [IO.File]::AppendAllText((Join-Path $root 'guard-samples.jsonl'), (($sample | ConvertTo-Json -Compress)+[Environment]::NewLine))
        if ($sample.available_bytes -lt $receipt.reserve_bytes -or $sample.free_commit_bytes -lt $receipt.reserve_bytes) {
            throw 'Windows reserve crossed; stopping only this NPU job.'
        }
        if ($timer.Elapsed.TotalSeconds -gt $TimeoutSeconds) { throw "NPU $flowName timeout." }
        if ($sample.active_job_processes) { Start-Sleep -Milliseconds 1000 }
    } while ($sample.active_job_processes)
    $receipt.exit_code = $job.ExitCode()
    if ($receipt.exit_code -ne 0) { throw "PDS exited with $($receipt.exit_code); inspect $logName." }
    $log = Get-Content -Raw -LiteralPath (Join-Path $root $logName)
    foreach ($action in $actions) {
        if ($log -notmatch ('(?m)^Executing : '+[regex]::Escape($action)+'\b[^\r\n]* successfully\.')) {
            throw "PDS $action completion marker absent; $flowName not accepted."
        }
    }
    if ($flowName -eq 'synthesis' -and -not (Test-Path -LiteralPath (Join-Path $root "prj_tasks\syn_1\synthesize\${topName}_syn.vm"))) {
        throw 'Mapped netlist absent; synthesis not accepted.'
    }
    $receipt.status = 'PASS'
} catch {
    $receipt.status = 'FAIL'
    $receipt.reason = $_.Exception.Message
    if ($null -ne $job) { $job.Stop() }
    throw
} finally {
    if ($null -ne $job) { $job.Dispose() }
    $receipt.elapsed_seconds = [math]::Round($timer.Elapsed.TotalSeconds, 3)
    [IO.File]::WriteAllText((Join-Path $root 'guard.json'), (($receipt | ConvertTo-Json -Depth 5)+[Environment]::NewLine))
}
Write-Output "PASS guarded $flowName; peak job commit $([math]::Round($receipt.peak_job_commit_bytes/1MB,1)) MiB."
