# cmd编码统一.ps1 —— 2026-11-27（K12 修复工具）
# 把仓库根全部 .cmd 统一为 GBK(936) + CRLF + 无 BOM；幂等，可反复跑
$enc936 = [System.Text.Encoding]::GetEncoding(936)
$utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
Get-ChildItem "$PSScriptRoot\*.cmd" | ForEach-Object {
    $bytes = [System.IO.File]::ReadAllBytes($_.FullName)
    $start = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $start = 3 }
    $text = $null
    try { $text = $utf8Strict.GetString($bytes, $start, $bytes.Length - $start) } catch { }
    if ($null -eq $text) {
        $text = $enc936.GetString($bytes, $start, $bytes.Length - $start)
        Write-Host ("[GBK  ] " + $_.Name)
    } else {
        Write-Host ("[UTF-8] " + $_.Name + "  -> GBK")
    }
    $text = $text -replace "`r`n", "`n" -replace "`n", "`r`n"
    [System.IO.File]::WriteAllBytes($_.FullName, $enc936.GetBytes($text))
}
Write-Host "完成：根目录 .cmd 已统一 GBK + CRLF + 无 BOM"
pause
