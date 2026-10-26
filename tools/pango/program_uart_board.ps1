# Temporary FPGA configuration only. No flash, eFuse or security commands.
param(
    [Parameter(Mandatory=$true)][string]$Bitstream,
    [Parameter(Mandatory=$true)][string]$Validation,
    [Parameter(Mandatory=$true)][string]$PdsHome,
    [Parameter(Mandatory=$true)][string]$RunDirectory,
    [ValidateRange(1024,65535)][int]$ServerPort = 65421
)
$ErrorActionPreference = 'Stop'
$bits = [IO.Path]::GetFullPath($Bitstream)
$run = [IO.Path]::GetFullPath($RunDirectory)
if ($run.StartsWith('\\') -or $run -match '[{}"\r\n]' -or $bits -match '[{}"\r\n]') { throw 'Use safe native Windows paths.' }
if (Test-Path -LiteralPath $run) { throw 'Preserve board records; use a fresh run directory.' }
$proof = Get-Content -Raw -LiteralPath $Validation | ConvertFrom-Json
$expectedUsercode = '12802701'
$expectedTop = 'npu_uart_board'
if ($proof.clock_hz -eq 100000000) { $expectedUsercode = '12810001'; $expectedTop = 'npu_uart_board100' }
if ($proof.bulk_weights) {
    if ($proof.clock_hz -ne 100000000) { throw 'Bulk profile requires 100 MHz.' }
    $expectedUsercode = '12810002'
}
if ($proof.schema -ne 'npu128-uart-bitstream-assessment-v1' -or $proof.status -ne 'PASS' -or
    $proof.device -ne 'PG2L200H-6IFBB676' -or $proof.clock_hz -notin @(27000000,100000000) -or
    $proof.actual_settings.'Top Module' -ne $expectedTop -or
    $proof.actual_settings.Usercode -ne $expectedUsercode -or $proof.actual_settings.'Set Configuration Bank Voltage(SCBV)' -ne 'High Voltage') {
    throw 'Accepted diagnostic bitstream evidence required.'
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $bits).Hash.ToLowerInvariant() -ne $proof.bitstream_sha256) { throw 'Bitstream hash mismatch.' }
New-Item -ItemType Directory -Path $run | Out-Null
Copy-Item -LiteralPath $Validation -Destination (Join-Path $run 'bitstream-validation.json')
$tcl = @'
cfg_set_tcl_break -flag true
cfg_connect -ip 127.0.0.1 -port @PORT@
set chain [cfg_scan_chain]
if {$chain != 1} {error "Expected exactly one FPGA; refuse configuration"}
set identifier [cfg_read_device_property -mode 0 -device_index 0]
if {![string equal -nocase $identifier "00603899"]} {error "Unexpected FPGA ID; refuse configuration"}
cfg_set_cable_property -index 0 -freq 5MHz
cfg_assign_file -file {@BITSTREAM@} -device_index 0
cfg_set_device_property -device_index 0 -program 1
set programmed [cfg_program -device_index 0]
puts "UART_DIAG_PROGRAM_RETURN=$programmed"
set verified [cfg_verify -device_index 0]
puts "UART_DIAG_VERIFY_RETURN=$verified"
set usercode [cfg_read_device_property -mode 2 -device_index 0]
puts "UART_DIAG_USERCODE=$usercode"
if {![string equal -nocase $usercode "@USERCODE@"]} {error "Diagnostic usercode mismatch"}
set status [cfg_read_device_property -mode 3 -device_index 0]
puts "UART_DIAG_CONFIG_STATUS=$status"
scan $status %x status_value
if {($status_value & 0x1000) == 0 || ($status_value & 0x1800001f) != 0} {error "DONE or configuration error status failed"}
cfg_disconnect
puts "UART_DIAG_JTAG_CHECKS_PASS"
exit
'@
$scriptPath = Join-Path $run 'program.tcl'
[IO.File]::WriteAllText($scriptPath, $tcl.Replace('@PORT@', [string]$ServerPort).Replace('@USERCODE@', $expectedUsercode).Replace('@BITSTREAM@', $bits.Replace('\','/')))
Add-Type -Path (Join-Path $PSScriptRoot 'GuardedJob.cs')
$exe = Join-Path $PdsHome 'bin\cdt_cfg_shell.exe'
$command = '-work_dir "'+$run+'" -file "'+$scriptPath+'"'
$receipt = [ordered]@{ schema = 'npu128-uart-jtag-v1'; status = 'running';
    bitstream_sha256 = $proof.bitstream_sha256; validation_sha256 = (Get-FileHash -LiteralPath $Validation -Algorithm SHA256).Hash.ToLowerInvariant();
    scope = 'volatile JTAG configuration and verify; no flash/eFuse/key operations';
    job_limit_bytes = [uint64](1GB); timeout_seconds = 300; peak_job_commit_bytes = [uint64]0 }
$timer = [Diagnostics.Stopwatch]::StartNew()
$job = $null
try {
    $job = New-Object NpuGuardedJob($exe, $command, $run, (Join-Path $run 'program.log'), $receipt.job_limit_bytes)
    $receipt.root_pid = $job.RootPid
    Write-Output "Started temporary JTAG configuration PID $($job.RootPid)."
    do {
        $receipt.peak_job_commit_bytes = $job.PeakCommitBytes()
        if ($timer.Elapsed.TotalSeconds -gt 300) { throw 'JTAG timeout; inspect board state before retrying.' }
        $active = $job.ActiveProcesses()
        if ($active) { Start-Sleep -Milliseconds 1000 }
    } while ($active)
    $receipt.exit_code = $job.ExitCode()
    $log = Get-Content -Raw -LiteralPath (Join-Path $run 'program.log')
    if ($receipt.exit_code -ne 0 -or $log -notmatch '(?m)^UART_DIAG_JTAG_CHECKS_PASS\s*$' -or $log -match '(?m)^E:') {
        throw 'JTAG validation did not complete; inspect program.log.'
    }
    if ($log -notmatch '(?m)^UART_DIAG_VERIFY_RETURN=1\s*$') { throw 'Readback verification did not return success.' }
    $receipt.status = 'PASS'
} catch {
    $receipt.status = 'FAIL'; $receipt.reason = $_.Exception.Message
    if ($null -ne $job) { $job.Stop() }
    throw
} finally {
    if ($null -ne $job) { $job.Dispose() }
    $receipt.elapsed_seconds = [math]::Round($timer.Elapsed.TotalSeconds, 3)
    [IO.File]::WriteAllText((Join-Path $run 'program-receipt.json'), (($receipt | ConvertTo-Json -Depth 5)+[Environment]::NewLine))
}
Write-Output 'PASS volatile JTAG configuration, readback, usercode and DONE/error status.'
