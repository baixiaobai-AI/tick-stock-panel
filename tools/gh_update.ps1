param(
  [Parameter(Mandatory=$true)][string]$Owner,
  [Parameter(Mandatory=$true)][string]$Repo,
  [Parameter(Mandatory=$true)][string]$Token,
  [Parameter(Mandatory=$true)][string]$Root,
  [string]$Branch = "main",
  [Parameter(Mandatory=$true)][string]$Message,
  [string]$LogFile = "",
  [int]$LeafMax = 250,
  [switch]$DryRun
)

# 增量同步: 只上传本地相对远端发生变化的文件, 然后重建整棵树并提交。
# 删除的文件不需要特殊处理 —— 树由本地文件全集重建, 远端多余的自然消失。

if ($LogFile -eq "") { $LogFile = Join-Path $env:TEMP "gh_update.log" }
$ErrorActionPreference = "Stop"

function Log($msg) { Add-Content -Path $LogFile -Value ("[" + (Get-Date).ToString("HH:mm:ss") + "] $msg") -Encoding UTF8 }

Add-Type -AssemblyName System.Net.Http | Out-Null
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$client = New-Object System.Net.Http.HttpClient
$client.DefaultRequestHeaders.Add("User-Agent", "tsp-updater")
$client.DefaultRequestHeaders.Add("Authorization", "Bearer $Token")
$client.DefaultRequestHeaders.Add("Accept", "application/vnd.github+json")
$client.Timeout = [TimeSpan]::FromMinutes(15)

$apiBase = "https://api.github.com/repos/$Owner/$Repo"
$sha1 = [System.Security.Cryptography.SHA1]::Create()

function Get-BlobSha([string]$fullPath) {
  $bytes = [System.IO.File]::ReadAllBytes($fullPath)
  $header = [System.Text.Encoding]::ASCII.GetBytes("blob " + $bytes.Length + "`0")
  $ms = New-Object System.IO.MemoryStream
  $ms.Write($header, 0, $header.Length)
  $ms.Write($bytes, 0, $bytes.Length)
  $ms.Position = 0
  $hash = $sha1.ComputeHash($ms)
  $sb = New-Object System.Text.StringBuilder
  foreach ($b in $hash) { [void]$sb.Append($b.ToString("x2")) }
  $ms.Dispose()
  return $sb.ToString()
}

function Escape-Json([string]$s) { return $s.Replace('\', '\\').Replace('"', '\"') }

function Post-Json([string]$url, [string]$json) {
  $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, "application/json")
  for ($a = 1; $a -le 3; $a++) {
    try {
      $resp = $client.PostAsync($url, $content).Result
      $body = $resp.Content.ReadAsStringAsync().Result
      $code = [int]$resp.StatusCode
      if ($code -lt 400) { return @{ ok = $true; status = $code; body = $body } }
      if ($code -eq 502 -or $code -eq 503) { Start-Sleep -Seconds 3; continue }
      return @{ ok = $false; status = $code; body = $body }
    } catch {
      if ($a -eq 3) { return @{ ok = $false; status = -1; body = $_.Exception.Message } }
      Start-Sleep -Seconds 3
    }
  }
  return @{ ok = $false; status = -1; body = "exhausted" }
}

function Get-Json([string]$url) {
  for ($a = 1; $a -le 3; $a++) {
    try {
      $resp = $client.GetAsync($url).Result
      $body = $resp.Content.ReadAsStringAsync().Result
      $code = [int]$resp.StatusCode
      if ($code -lt 400) { return @{ ok = $true; status = $code; body = $body } }
      if ($code -eq 502 -or $code -eq 503) { Start-Sleep -Seconds 3; continue }
      return @{ ok = $false; status = $code; body = $body }
    } catch {
      if ($a -eq 3) { return @{ ok = $false; status = -1; body = $_.Exception.Message } }
      Start-Sleep -Seconds 3
    }
  }
  return @{ ok = $false; status = -1; body = "exhausted" }
}

# 注意: 用 Invoke-WebRequest 而不是 HttpClient.SendAsync(PATCH) ——
# 后者在本沙盒会静默失败(进程退出码 1 但不落日志), 已踩过两次。
function Patch-Json([string]$url, [string]$json) {
  $h = @{ "Authorization" = "Bearer $Token"; "Accept" = "application/vnd.github+json"; "User-Agent" = "tsp-updater" }
  $ok = $false
  $st = -1
  $bd = ""
  try {
    $resp = Invoke-WebRequest -Uri $url -Method Patch -Headers $h -Body ([System.Text.Encoding]::UTF8.GetBytes($json)) -ContentType "application/json; charset=utf-8" -TimeoutSec 60 -UseBasicParsing
    $ok = $true
    $st = [int]$resp.StatusCode
    $bd = $resp.Content
  } catch {
    $ok = $false
    $st = -1
    $bd = "PATCHERROR " + $Error[0].Exception.Message
  }
  return @{ ok = $ok; status = $st; body = $bd }
}

Log "===== sync start: $Owner/$Repo@$Branch ====="

# ---------- 1. 远端 HEAD 与文件树 ----------
$refR = Get-Json "$apiBase/git/refs/heads/$Branch"
if (-not $refR.ok) { Log "REF READ FAIL: status=$($refR.status) body=$($refR.body)"; exit 1 }
$parentSha = ($refR.body | ConvertFrom-Json).object.sha
Log "remote HEAD = $parentSha"

$treeR = Get-Json "$apiBase/git/trees/$Branch`?recursive=1"
if (-not $treeR.ok) { Log "TREE READ FAIL: status=$($treeR.status)"; exit 1 }
$treeObj = $treeR.body | ConvertFrom-Json
if ($treeObj.truncated) { Log "WARN: remote tree truncated"; exit 1 }
$remote = @{}
foreach ($n in $treeObj.tree) { if ($n.type -eq "blob") { $remote[$n.path] = $n.sha } }
Log "remote blobs = $($remote.Count)"

# ---------- 2. 本地文件 + SHA ----------
# 排除版本控制/构建产物/缓存目录: 它们既不该进仓库, 也会把上传量从个位数文件炸到
# 几万个 (装一次 node_modules 就是 3 万+ 文件, SHA 计算和上传都要几小时)。
# 规则对齐仓库 .gitignore 的前几节；被排除的远端残留文件会在重建树时自动消失。
$ignoreRe = @(
  '\\\.git\\', '\\node_modules\\', '\\dist\\', '\\build\\', '\\__pycache__\\',
  '\\\.pytest_cache\\', '\\\.mypy_cache\\', '\\\.ruff_cache\\', '\\\.uv\\',
  '\\\.venv\\', '\\venv\\', '\\\.pnpm-store\\', '\\\.vite\\',
  '\.pyc$', '\.pyo$', '\.tsbuildinfo$'
)
$all = Get-ChildItem -Path $Root -Recurse -File -Force | Where-Object {
  $full = $_.FullName
  -not ($ignoreRe | Where-Object { $full -match $_ })
}
$items = New-Object System.Collections.Generic.List[object]
foreach ($f in $all) {
  $rel = $f.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
  $mode = "100644"
  if ($f.Extension.ToLower() -eq ".sh") { $mode = "100755" }
  $items.Add([pscustomobject]@{ Path = $rel; Mode = $mode; Sha = (Get-BlobSha $f.FullName) })
}
Log "local files = $($items.Count)"

# ---------- 3. 找出变化的文件 ----------
$changed = New-Object System.Collections.Generic.List[object]
$localPaths = @{}
foreach ($it in $items) {
  $localPaths[$it.Path] = $true
  if (-not $remote.ContainsKey($it.Path)) { $changed.Add($it) }
  elseif ($remote[$it.Path] -ne $it.Sha) { $changed.Add($it) }
}
$deleted = @()
foreach ($p in $remote.Keys) { if (-not $localPaths.ContainsKey($p)) { $deleted += $p } }

Log "changed/new = $($changed.Count), deleted = $($deleted.Count)"
foreach ($c in ($changed | Select-Object -First 30)) { Log "  ~ $($c.Path)" }
foreach ($d in ($deleted | Select-Object -First 30)) { Log "  - $d" }

if (($changed.Count -eq 0) -and ($deleted.Count -eq 0)) {
  Log "NO CHANGES - nothing to push"
  exit 0
}

if ($DryRun) { Log "DRY RUN - stop before upload"; exit 0 }

# ---------- 4. 只上传变化的 blob ----------
$upOk = 0
foreach ($c in $changed) {
  $bytes = [System.IO.File]::ReadAllBytes((Join-Path $Root $c.Path))
  $b64 = [Convert]::ToBase64String($bytes)
  $r = Post-Json "$apiBase/git/blobs" ('{"content":"' + $b64 + '","encoding":"base64"}')
  if (-not $r.ok) { Log "BLOB FAIL [$($c.Path)]: status=$($r.status) body=$($r.body)"; exit 1 }
  $upOk++
}
Log "uploaded blobs = $upOk"

# ---------- 5. 增量建树 (base_tree 分批) ----------
$entries = New-Object System.Collections.Generic.List[string]
foreach ($it in $items) {
  $entries.Add('{"path":"' + (Escape-Json $it.Path) + '","mode":"' + $it.Mode + '","type":"blob","sha":"' + $it.Sha + '"}')
}
$treeSha = ""
$batchNo = 0
for ($i = 0; $i -lt $entries.Count; $i += $LeafMax) {
  $end = [Math]::Min($i + $LeafMax - 1, $entries.Count - 1)
  $slice = @()
  for ($j = $i; $j -le $end; $j++) { $slice += $entries[$j] }
  if ($treeSha -eq "") { $json = '{"tree":[' + ($slice -join ',') + ']}' }
  else { $json = '{"base_tree":"' + $treeSha + '","tree":[' + ($slice -join ',') + ']}' }
  $r = Post-Json "$apiBase/git/trees" $json
  if (-not $r.ok) { Log "TREE BATCH FAIL: status=$($r.status) body=$($r.body)"; exit 1 }
  $treeSha = ($r.body | ConvertFrom-Json).sha
  $batchNo++
}
Log "new tree = $treeSha (batches=$batchNo)"

# ---------- 6. 提交 + 更新 ref ----------
$msgJson = $Message | ConvertTo-Json -Compress
$commitJson = '{"message":' + $msgJson + ',"tree":"' + $treeSha + '","parents":["' + $parentSha + '"]}'
$cr = Post-Json "$apiBase/git/commits" $commitJson
if (-not $cr.ok) { Log "COMMIT FAIL: status=$($cr.status) body=$($cr.body)"; exit 1 }
$commitSha = ($cr.body | ConvertFrom-Json).sha
Log "commit = $commitSha"

$rr = Patch-Json "$apiBase/git/refs/heads/$Branch" ('{"sha":"' + $commitSha + '","force":true}')
if (-not $rr.ok) { Log "REF PATCH FAIL: status=$($rr.status) :: $($rr.body)"; exit 1 }
Log "ref updated: $Branch -> $commitSha"
Log "===== DONE ====="
exit 0
