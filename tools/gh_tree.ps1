param(
  [Parameter(Mandatory=$true)][string]$Owner,
  [Parameter(Mandatory=$true)][string]$Repo,
  [Parameter(Mandatory=$true)][string]$Token,
  [Parameter(Mandatory=$true)][string]$Root,
  [Parameter(Mandatory=$true)][string]$LogFile,
  [string]$ParentSha = "",
  [int]$LeafMax = 250
)

$ErrorActionPreference = "Stop"

function Log($msg) { Add-Content -Path $LogFile -Value ("[" + (Get-Date).ToString("HH:mm:ss") + "] $msg") -Encoding UTF8 }

Add-Type -AssemblyName System.Net.Http | Out-Null
[System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

$client = New-Object System.Net.Http.HttpClient
$client.DefaultRequestHeaders.Add("User-Agent", "tsp-uploader")
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

function Patch-Json([string]$url, [string]$json) {
  $content = New-Object System.Net.Http.StringContent($json, [System.Text.Encoding]::UTF8, "application/json")
  $req = New-Object System.Net.Http.HttpRequestMessage
  $req.Method = [System.Net.Http.HttpMethod]::Patch
  $req.RequestUri = $url
  $req.Content = $content
  $resp = $client.SendAsync($req).Result
  $body = $resp.Content.ReadAsStringAsync().Result
  return @{ ok = ([int]$resp.StatusCode -lt 400); status = [int]$resp.StatusCode; body = $body }
}

# ---------- 收集 + 本地算 SHA ----------
Log "collecting + hashing files"
$all = Get-ChildItem -Path $Root -Recurse -File -Force | Where-Object { $_.FullName -notmatch "\\\.git\\" }
$items = New-Object System.Collections.Generic.List[object]
foreach ($f in $all) {
  $rel = $f.FullName.Substring($Root.Length).TrimStart('\', '/').Replace('\', '/')
  $mode = "100644"
  if ($f.Extension.ToLower() -eq ".sh") { $mode = "100755" }
  $items.Add([pscustomobject]@{ Path = $rel; Mode = $mode; Sha = (Get-BlobSha $f.FullName) })
}
Log "hashed $($items.Count) files"

# ---------- 增量建树 (base_tree 分批追加) ----------
$entries = New-Object System.Collections.Generic.List[string]
foreach ($it in $items) {
  $entries.Add('{"path":"' + (Escape-Json $it.Path) + '","mode":"' + $it.Mode + '","type":"blob","sha":"' + $it.Sha + '"}')
}
Log "entries prepared: $($entries.Count)"

$treeSha = ""
$batchNo = 0
try {
  for ($i = 0; $i -lt $entries.Count; $i += $LeafMax) {
    $end = [Math]::Min($i + $LeafMax - 1, $entries.Count - 1)
    $slice = @()
    for ($j = $i; $j -le $end; $j++) { $slice += $entries[$j] }
    if ($treeSha -eq "") {
      $json = '{"tree":[' + ($slice -join ',') + ']}'
    } else {
      $json = '{"base_tree":"' + $treeSha + '","tree":[' + ($slice -join ',') + ']}'
    }
    $r = Post-Json "$apiBase/git/trees" $json
    if (-not $r.ok) { throw "TREE BATCH FAIL: status=$($r.status) body=$($r.body)" }
    $treeSha = ($r.body | ConvertFrom-Json).sha
    $batchNo++
    Log "batch#$batchNo items=$($slice.Count) bytes=$($json.Length) -> $treeSha"
  }
} catch {
  Log "TREE ERROR: $($_.Exception.Message)"
  exit 1
}
Log "ROOT tree sha: $treeSha (batches: $batchNo)"

# ---------- commit ----------
$msg = @"
init: TickFlowStockPanel (TSP) with free data-source plugins

- Add AKShare plugin (zero-key, EastMoney public API): daily K / adj factors / realtime
- Add Tushare plugin (token-based, free credits): daily K / adj factors (amount x1000 -> CNY)
- Fix NameError in packaging/tickflow.spec: _safe_metadata() used before definition
- release.yml: trigger on push to main + install akshare/tushare deps before PyInstaller
- Add tiers.yaml for the no-key free tier
"@
$msgJson = $msg | ConvertTo-Json -Compress
if ($ParentSha -ne "") {
  $commitJson = '{"message":' + $msgJson + ',"tree":"' + $treeSha + '","parents":["' + $ParentSha + '"]}'
} else {
  $commitJson = '{"message":' + $msgJson + ',"tree":"' + $treeSha + '","parents":[]}'
}
$cr = Post-Json "$apiBase/git/commits" $commitJson
if (-not $cr.ok) { Log "COMMIT FAIL: status=$($cr.status) body=$($cr.body)"; exit 1 }
$commitSha = ($cr.body | ConvertFrom-Json).sha
Log "commit sha: $commitSha"

if ($ParentSha -ne "") {
  $rr2 = Patch-Json "$apiBase/git/refs/heads/main" ('{"sha":"' + $commitSha + '","force":true}')
  if (-not $rr2.ok) { Log "REF PATCH FAIL: status=$($rr2.status) :: $($rr2.body)"; exit 1 }
  Log "ref updated: main -> $commitSha"
} else {
  $rr2 = Post-Json "$apiBase/git/refs" ('{"ref":"refs/heads/main","sha":"' + $commitSha + '"}')
  if (-not $rr2.ok) { Log "REF FAIL: status=$($rr2.status) :: $($rr2.body)"; exit 1 }
  Log "ref created: main -> $commitSha"
}
Log "DONE"
exit 0
