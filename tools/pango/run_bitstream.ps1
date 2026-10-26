# Generate only a snapshot's hash-checked UART routed database; no hardware access.
param(
    [Parameter(Mandatory=$true)][string]$ProjectRoot,
    [Parameter(Mandatory=$true)][string]$PdsHome,
    [ValidateRange(1,4)][double]$JobLimitGiB = 3,
    [ValidateRange(4,64)][int]$ReserveGiB = 6,
    [ValidateRange(60,3600)][int]$TimeoutSeconds = 1200
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ProjectRoot)
if ($root.StartsWith('\\') -or $root -match '[{}"\r\n]') { throw 'Use a native Windows path.' }
$manifestPath = Join-Path $root 'bitstream-manifest.json'
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.schema -ne 'npu128-pango-bitstream-v1' -or $manifest.device -ne 'PG2L200H-6IFBB676' -or $manifest.clock_hz -notin @(27000000,100000000)) {
    throw 'Unexpected diagnostic bitstream manifest.'
}
$expectedUsercode = '12802701'
$expectedTop = 'npu_uart_board'
if ($manifest.clock_hz -eq 100000000) { $expectedUsercode = '12810001'; $expectedTop = 'npu_uart_board100' }
if ($manifest.bulk_weights) {
    if ($manifest.clock_hz -ne 100000000) { throw 'Bulk profile requires 100 MHz.' }
    $expectedUsercode = '12810002'
}
if ($manifest.top -ne $expectedTop -or $manifest.bitstream_configuration.scbv -ne 'High Voltage' -or $manifest.bitstream_configuration.usercode -ne $expectedUsercode) {
    throw 'Unexpected board configuration voltage or diagnostic usercode.'
}
function Check-Inputs {
    foreach ($file in $manifest.immutable_sha256.PSObject.Properties) {
        $path = [IO.Path]::GetFullPath((Join-Path $root $file.Name))
        if (-not $path.StartsWith($root.TrimEnd('\')+'\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Input escapes project.' }
        if ((Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() -ne $file.Value) {
            throw "Bitstream input changed: $($file.Name)"
        }
    }
}
Check-Inputs
foreach ($name in @('bitstream.log', 'bitstream-guard.json', 'bitstream-samples.jsonl')) {
    if (Test-Path -LiteralPath (Join-Path $root $name)) { throw "Preserve $name; use a new snapshot." }
}
$exe = Join-Path $PdsHome 'bin\pds_shell.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Missing pds_shell.exe.' }
$running = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^(pds|pds_shell|dw|ads|ppc|ppp|pne|evp|synthesize|synplify|synplify_pro)\.exe$' })
if ($running.Count) { throw 'Another PDS job is active.' }
function Read-Memory {
    $c = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
    if ($null -eq $c.AvailableBytes -or $null -eq $c.CommittedBytes -or $null -eq $c.CommitLimit -or $c.CommitLimit -le $c.CommittedBytes) {
        throw 'Windows memory accounting unavailable.'
    }
    return [ordered]@{ timestamp = (Get-Date -Format o); available_bytes = [uint64]$c.AvailableBytes;
        free_commit_bytes = ([uint64]$c.CommitLimit-[uint64]$c.CommittedBytes) }
}
$before = Read-Memory
$threshold = [uint64](($ReserveGiB+$JobLimitGiB)*1GB)
if ($before.available_bytes -lt $threshold -or $before.free_commit_bytes -lt $threshold) { throw 'Insufficient physical or commit headroom.' }
if (($env:PATHEXT -split ';') -notcontains '.EXE') { $env:PATHEXT = '.EXE;.COM;.BAT;.CMD;'+$env:PATHEXT }
$env:OMP_NUM_THREADS = '1'
$env:OPENBLAS_NUM_THREADS = '1'
$env:MKL_NUM_THREADS = '1'
Add-Type -Path (Join-Path $root 'bitstream-input\GuardedJob.cs')
$command = '-work_dir "'+$root+'" -project_name uart_bitstream -file "'+(Join-Path $root 'generate-bitstream.tcl')+'"'
$receipt = [ordered]@{ status = 'running'; before = $before; job_limit_bytes = [uint64]($JobLimitGiB*1GB);
    reserve_bytes = [uint64]($ReserveGiB*1GB); timeout_seconds = $TimeoutSeconds; command = $command;
    manifest_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash.ToLowerInvariant();
    peak_job_commit_bytes = [uint64]0; minimum_available_bytes = $before.available_bytes; minimum_free_commit_bytes = $before.free_commit_bytes }
$job = $null
$timer = [Diagnostics.Stopwatch]::StartNew()
try {
    $job = New-Object NpuGuardedJob($exe, $command, $root, (Join-Path $root 'bitstream.log'), $receipt.job_limit_bytes)
    $receipt.root_pid = $job.RootPid
    Write-Output "Started guarded bitstream PID $($job.RootPid); cap=$JobLimitGiB GiB, reserve=$ReserveGiB GiB."
    do {
        $sample = Read-Memory
        $sample.active_job_processes = $job.ActiveProcesses()
        $sample.peak_job_commit_bytes = $job.PeakCommitBytes()
        $receipt.peak_job_commit_bytes = $sample.peak_job_commit_bytes
        $receipt.minimum_available_bytes = [math]::Min($receipt.minimum_available_bytes, $sample.available_bytes)
        $receipt.minimum_free_commit_bytes = [math]::Min($receipt.minimum_free_commit_bytes, $sample.free_commit_bytes)
        [IO.File]::AppendAllText((Join-Path $root 'bitstream-samples.jsonl'), (($sample | ConvertTo-Json -Compress)+[Environment]::NewLine))
        $liveLog = Get-Content -Raw -LiteralPath (Join-Path $root 'bitstream.log')
        if ($liveLog -match '(?m)^Process "(Compile|Synthesize|Device Map|Place|Route|Route Optimize)" started\.') {
            throw 'Unexpected implementation rerun; stop this job immediately.'
        }
        if ($sample.available_bytes -lt $receipt.reserve_bytes -or $sample.free_commit_bytes -lt $receipt.reserve_bytes) { throw 'Windows reserve crossed.' }
        if ($timer.Elapsed.TotalSeconds -gt $TimeoutSeconds) { throw 'Bitstream timeout.' }
        if ($sample.active_job_processes) { Start-Sleep -Milliseconds 1000 }
    } while ($sample.active_job_processes)
    $receipt.exit_code = $job.ExitCode()
    if ($receipt.exit_code -ne 0) { throw "PDS exited with $($receipt.exit_code)." }
    $log = Get-Content -Raw -LiteralPath (Join-Path $root 'bitstream.log')
    if ($log -notmatch '(?m)^Executing : gen_bit_stream\b[^\r\n]* successfully\.') { throw 'Bitstream completion marker absent.' }
    if ($log -match '(?m)^[EC]:') { throw 'Bitstream error or critical warning; reject this output.' }
    if ($log -match '(?m)^Executing : (compile|synthesize|dev_map|place|route|route_optimize)\b') { throw 'Unexpected implementation rerun.' }
    Check-Inputs
    $bits = @(Get-ChildItem -LiteralPath (Join-Path $root 'prj_tasks') -Filter '*.sbit' -File -Recurse)
    if ($bits.Count -ne 1 -or $bits[0].Length -lt 1024) { throw 'Expected one nonempty diagnostic sbit.' }
    $receipt.bitstream = $bits[0].FullName.Substring($root.Length+1)
    $receipt.bitstream_sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $bits[0].FullName).Hash.ToLowerInvariant()
    $receipt.bitstream_bytes = $bits[0].Length
    $receipt.status = 'PASS'
} catch {
    $receipt.status = 'FAIL'; $receipt.reason = $_.Exception.Message
    if ($null -ne $job) { $job.Stop() }
    throw
} finally {
    if ($null -ne $job) { $job.Dispose() }
    $receipt.elapsed_seconds = [math]::Round($timer.Elapsed.TotalSeconds, 3)
    [IO.File]::WriteAllText((Join-Path $root 'bitstream-guard.json'), (($receipt | ConvertTo-Json -Depth 5)+[Environment]::NewLine))
}
Write-Output "PASS guarded bitstream; $($receipt.bitstream_bytes) bytes; SHA256=$($receipt.bitstream_sha256)."
