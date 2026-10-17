<#
.SYNOPSIS
  端到端验收测试：单图矢量化 + 批量生成，可选 300 条完整套件。

.DESCRIPTION
  默认跑两个场景（按钮点一下即自动开始，全程无需人工输入）：

    1. 单图矢量化   「念奴娇」走完整链路：提示词合成 → 文生图 → 矢量化
    2. 批量生成     3 条输入，分别命中 Qwen / Flux / Z-Image 三条路由链

  加 -Full 再追加一段 300 条的批量套件（调用 gen2vec_cli.exe batch，
  核验 batch_summary.csv 与每条任务的产物）。

  生成类场景设有 ComfyUI 门禁：ComfyUI 未就绪时直接判失败，而不是让后端
  静默回落到 Pillow 占位图——否则会出现「测试全绿但什么都没生成」。

  退出码：0 全部通过 / 1 有失败 / 2 环境不可用 / 3 参数错误

.PARAMETER SkipBatch
  只跑单图场景，跳过批量生成（省约 5-15 分钟）。

.PARAMETER Full
  追加 300 条完整批量套件（串行执行，耗时以小时计）。

.PARAMETER DryRun
  只做前置检查并打印将要执行的请求，不真正调用后端。

.PARAMETER VerifyOnly
  跳过所有场景，只核验一份已有的 batch_summary.csv。

.PARAMETER OutputRoot
  产物根目录。桌面端按钮会传入安装目录下的 outputs\，脚本把报告也写在这里，
  并登记进 tasks-index.json，使验收记录出现在桌面端「历史任务」面板。
#>
[CmdletBinding()]
param(
  # ── 场景选择 ──
  [switch]$SkipBatch,
  [switch]$Full,

  # ── 运行控制 ──
  [switch]$DryRun,
  [string]$VerifyOnly = "",
  [int]$Wait = 30,
  [switch]$SkipHealth,

  # ── 产物 ──
  [string]$OutputRoot = "",
  [string]$ReportDir = "",

  # ── 后端地址 ──
  [string]$Txt2ImgBase = "http://127.0.0.1:9001",
  [string]$VectorizerBase = "http://127.0.0.1:8000",
  [string]$ComfyUiHost = "http://127.0.0.1:8188",
  [int]$GenTimeoutSec = 1800,
  [int]$VectorTimeoutSec = 600,
  [int]$ComfyUiWaitSec = 300,

  # ── 300 条套件参数（沿用旧脚本语义）──
  [string]$CliPath = "",
  [string]$Resolution = "1664 x 928",
  [ValidateSet("clean", "balanced", "detailed", "ultra")]
  [string]$VectorPreset = "balanced",
  [int]$Seed = 0,
  [int]$SeedStep = 1,
  [string]$Negative = "low quality, blurry, wrong text, missing character, extra character, garbled letters, busy background, human figure, watermark",
  [int]$MaxFailures = 0,
  [int]$MaxE2eMs = 0,
  [int]$MaxVectorMs = 0,
  [switch]$NoVectorize,
  [switch]$AllowResolutionMismatch,
  [switch]$StrictSvgGroups
)

# 不要改 [Console]::OutputEncoding。老 conhost 在 UTF-8(65001) 代码页下对中文
# 宽字符的宽度记账有 bug，会把每个汉字显示成两遍；保持系统默认代码页（中文 Windows
# 通常是 936）显示才正常。报告文件用显式 UTF-8 读写，与控制台编码无关。
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

# ══════════════════════════════════════════════════════════════════════
# 常量：复刻自 services/txt2img-api/app/generator.py，由 Test-ContractDrift 守卫
# ══════════════════════════════════════════════════════════════════════

# generator.py 里本地兜底（占位图）的 engine 名，出现即代表「没真跑 ComfyUI」
$script:StubEngine = 'local-studio'

# 场景一的固定输入
$script:SingleText = '念奴娇'
$script:SingleStyle = '三个中文艺术字，霸气狂草，黑白泼墨风格，红色飞扬线条勾勒，金戈铁马之感'

# ComfyUI 工作流里产出 latent 的节点类型（generator.py 未导出，属实现细节约定）
$script:LatentClassTypes = @('EmptyLatentImage', 'EmptySD3LatentImage')

function Get-ExpectedRouteChains {
  # generator.py: _ROUTE_CHAINS（值取自 _CHINESE/_ENGLISH/_MIXED_FALLBACK）
  return @{
    chinese = @('qwen_image_2512_gguf', 'test_z_image_turbo')
    english = @('flux_schnell', 'test_z_image_turbo')
    mixed   = @('test_z_image_turbo')
  }
}

function Get-ExpectedQwenResolutions {
  # generator.py: _QWEN_RESOLUTIONS
  return @('1328x1328', '1664x928', '928x1664', '1472x1104', '1104x1472', '1584x1056', '1056x1584')
}

function Get-ExpectedFluxResolutions {
  # generator.py: _FLUX_RESOLUTIONS（所有边长均为 64 的整数倍）
  return @('1024x1024', '1344x768', '768x1344', '1152x896', '896x1152', '1216x832', '832x1216')
}

# ══════════════════════════════════════════════════════════════════════
# 通用工具
# ══════════════════════════════════════════════════════════════════════

function ConvertTo-DeepHashtable {
  <#
    把 Invoke-RestMethod 产出的 PSCustomObject 递归转成 hashtable，
    让下游取值逻辑只需处理一种形态。
  #>
  param($InputObject)

  if ($null -eq $InputObject) { return $null }

  if ($InputObject -is [System.Collections.IDictionary]) {
    $map = @{}
    foreach ($key in $InputObject.Keys) { $map[$key] = ConvertTo-DeepHashtable $InputObject[$key] }
    return $map
  }

  if ($InputObject -is [System.Management.Automation.PSCustomObject]) {
    $map = @{}
    foreach ($prop in $InputObject.PSObject.Properties) { $map[$prop.Name] = ConvertTo-DeepHashtable $prop.Value }
    return $map
  }

  if ($InputObject -is [System.Collections.IEnumerable] -and $InputObject -isnot [string]) {
    $list = @()
    foreach ($item in $InputObject) { $list += , (ConvertTo-DeepHashtable $item) }
    return $list
  }

  return $InputObject
}

function Get-DeepValue {
  <# 从 hashtable 或 PSCustomObject 取键；不存在时返回 $null，不抛错。 #>
  param($Object, [string]$Key)

  if ($null -eq $Object) { return $null }

  if ($Object -is [System.Collections.IDictionary]) {
    if ($Object.Contains($Key)) { return $Object[$Key] }
    return $null
  }

  $prop = $Object.PSObject.Properties[$Key]
  if ($prop) { return $prop.Value }
  return $null
}

function Get-Snippet {
  <# 截取响应正文的一小段用于报错详情，容忍 null 与非字符串。 #>
  param($Text, [int]$MaxLength = 120)

  $value = [string]$Text
  if ([string]::IsNullOrEmpty($value)) { return '' }
  if ($value.Length -le $MaxLength) { return ($value -replace '\s+', ' ') }
  return (($value.Substring(0, $MaxLength) -replace '\s+', ' ') + '…')
}

function ConvertTo-ResolutionKey {
  <# 把 '1664 x 928' 或 @(1664,928) 统一成 '1664x928'，便于逐项比对。 #>
  param($Value)

  if ($null -eq $Value) { return '' }
  if ($Value -is [string]) { return (($Value -replace '\s', '') -replace '\*', 'x') }
  if ($Value -is [System.Collections.IEnumerable]) {
    $parts = @($Value)
    if ($parts.Count -ge 2) { return "$($parts[0])x$($parts[1])" }
  }
  return [string]$Value
}

# ══════════════════════════════════════════════════════════════════════
# fixture 解析
# ══════════════════════════════════════════════════════════════════════

function ConvertFrom-AcceptanceLine {
  <#
    解析一行 `文本 | 风格提示词 | 负面提示词 | seed | resolution [| 更多列]`。

    空行与 `#` 注释返回 $null。兼容全角竖线、Tab 分隔与 BOM。
    第 6 列起的字段落在 Extras（routes.txt 用它携带期望工作流与文本分类）。
  #>
  param([string]$Line)

  if ($null -eq $Line) { return $null }

  $text = $Line.Replace([string][char]0xFEFF, '')
  $text = $text.Replace([string][char]0xFF5C, '|')
  $text = $text.Replace("`t|", '|').Replace("|`t", '|')
  $text = $text.Trim()

  if ([string]::IsNullOrWhiteSpace($text)) { return $null }
  if ($text.StartsWith('#')) { return $null }

  if ($text.Contains('|')) {
    $parts = @($text -split '\|')
  } elseif ($text.Contains("`t")) {
    $parts = @($text -split "`t")
  } else {
    $parts = @($text)
  }

  $parts = @($parts | ForEach-Object { $_.Trim() })
  while ($parts.Count -lt 5) { $parts += '' }

  $extras = @()
  if ($parts.Count -gt 5) { $extras = @($parts[5..($parts.Count - 1)]) }

  return [PSCustomObject]@{
    Text       = $parts[0]
    Prompt     = $parts[1]
    Negative   = $parts[2]
    Seed       = $parts[3]
    Resolution = $parts[4]
    Extras     = $extras
  }
}

function Resolve-FixturePath {
  <#
    定位 fixture 文件。

    打包后 fixture 可能落在 tests\fixtures\，也可能被摊平到 tests\（见
    apps/desktop/package.json 的 extraResources），两处都找，避免出现
    「文件明明在安装目录里，脚本却说找不到」。
  #>
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [string]$BaseDir = $ScriptDir
  )

  $candidates = @(
    (Join-Path $BaseDir "fixtures\$Name"),
    (Join-Path $BaseDir $Name)
  )

  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }
  }

  throw "找不到 fixture $Name，已查找：$($candidates -join '；')"
}

function Read-RoutesFixture {
  <#
    读 routes.txt：每行 = 5 列输入 + 期望工作流 + 期望文本分类。
    seed 缺省 '0'，resolution 缺省 '1024 x 1024'。
  #>
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) { throw "routes fixture 不存在：$Path" }

  $rows = @()
  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    $row = ConvertFrom-AcceptanceLine -Line $line
    if ($null -eq $row) { continue }
    if ([string]::IsNullOrWhiteSpace($row.Text)) { continue }

    $rows += [PSCustomObject]@{
      Text             = $row.Text
      Prompt           = $row.Prompt
      Negative         = $row.Negative
      Seed             = if ([string]::IsNullOrWhiteSpace($row.Seed)) { '0' } else { $row.Seed }
      Resolution       = if ([string]::IsNullOrWhiteSpace($row.Resolution)) { '1024 x 1024' } else { $row.Resolution }
      ExpectedWorkflow = if ($row.Extras.Count -ge 1) { $row.Extras[0] } else { '' }
      ExpectedProfile  = if ($row.Extras.Count -ge 2) { $row.Extras[1] } else { '' }
    }
  }

  return $rows
}

function Read-GeneratorConstants {
  <# 从 generator.py 源码解析出路由链与分辨率表，供漂移比对。 #>
  param([Parameter(Mandatory = $true)][string]$Path)

  if (-not (Test-Path -LiteralPath $Path)) { throw "generator.py 不存在：$Path" }

  $src = Get-Content -LiteralPath $Path -Raw -Encoding UTF8

  # _CHINESE_FALLBACK = ["a", "b"] 等三条降级链
  $fallbacks = @{}
  foreach ($m in [regex]::Matches($src, '_(CHINESE|ENGLISH|MIXED)_FALLBACK\s*=\s*\[(.*?)\]', 'Singleline')) {
    $chainName = $m.Groups[1].Value.ToLowerInvariant()
    $names = @([regex]::Matches($m.Groups[2].Value, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value })
    $fallbacks[$chainName] = $names
  }

  # _ROUTE_CHAINS = { "chinese": _CHINESE_FALLBACK, ... }
  $routeChains = @{}
  $chainBlock = [regex]::Match($src, '_ROUTE_CHAINS[^=]*=\s*\{(.*?)\}', 'Singleline')
  if ($chainBlock.Success) {
    foreach ($m in [regex]::Matches($chainBlock.Groups[1].Value, '"(\w+)"\s*:\s*_(\w+)_FALLBACK')) {
      $textProfile = $m.Groups[1].Value
      $source = $m.Groups[2].Value.ToLowerInvariant()
      if ($fallbacks.ContainsKey($source)) { $routeChains[$textProfile] = $fallbacks[$source] }
    }
  }

  $resolutions = @{}
  foreach ($name in @('QWEN', 'FLUX')) {
    $block = [regex]::Match($src, "_${name}_RESOLUTIONS\s*=\s*\[(.*?)\]", 'Singleline')
    $pairs = @()
    if ($block.Success) {
      $pairs = @([regex]::Matches($block.Groups[1].Value, '\(\s*(\d+)\s*,\s*(\d+)\s*\)') |
        ForEach-Object { "$($_.Groups[1].Value)x$($_.Groups[2].Value)" })
    }
    $resolutions[$name] = ($pairs -join ',')
  }

  # stub engine 只在 _local_stub_generate 里取。
  # 全文件搜 `"engine": "..."` 会命中真实 ComfyUI 路径（generator.py 里出现更早）。
  $stubFn = [regex]::Match($src, 'def _local_stub_generate\(.*?(?=\ndef |\Z)', 'Singleline')
  $stubMatch = if ($stubFn.Success) { [regex]::Match($stubFn.Value, '"engine"\s*:\s*"([^"]+)"') } else { $null }

  return [PSCustomObject]@{
    RouteChains      = $routeChains
    QwenResolutions  = $resolutions['QWEN']
    FluxResolutions  = $resolutions['FLUX']
    StubEngine       = if ($stubMatch -and $stubMatch.Success) { $stubMatch.Groups[1].Value } else { '' }
  }
}

function Test-ContractDrift {
  <#
    比对脚本内复刻的常量与 generator.py 实际值，返回漂移描述数组（空 = 无漂移）。

    漂移只在报告里记 WARN，不判失败——真正的判定靠运行期的
    workflow_used 断言，那是更强的信号。
  #>
  param(
    [Parameter(Mandatory = $true)][string]$GeneratorPath,
    [Parameter(Mandatory = $true)][hashtable]$RouteChains,
    [Parameter(Mandatory = $true)][array]$QwenResolutions,
    [Parameter(Mandatory = $true)][array]$FluxResolutions
  )

  if (-not (Test-Path -LiteralPath $GeneratorPath)) { throw "generator.py 不存在：$GeneratorPath" }

  $actual = Read-GeneratorConstants -Path $GeneratorPath
  $drift = New-Object System.Collections.ArrayList

  foreach ($textProfile in @('chinese', 'english', 'mixed')) {
    $want = if ($RouteChains.ContainsKey($textProfile)) { (@($RouteChains[$textProfile]) -join '|') } else { '<缺失>' }
    $got = if ($actual.RouteChains.ContainsKey($textProfile)) { (@($actual.RouteChains[$textProfile]) -join '|') } else { '<缺失>' }
    if ($want -ne $got) {
      [void]$drift.Add("路由链 $textProfile 漂移：脚本=$want  generator.py=$got")
    }
  }

  $wantQwen = (@($QwenResolutions) | ForEach-Object { ConvertTo-ResolutionKey $_ }) -join ','
  if ($wantQwen -ne $actual.QwenResolutions) {
    [void]$drift.Add("Qwen 分辨率表漂移：脚本=$wantQwen  generator.py=$($actual.QwenResolutions)")
  }

  $wantFlux = (@($FluxResolutions) | ForEach-Object { ConvertTo-ResolutionKey $_ }) -join ','
  if ($wantFlux -ne $actual.FluxResolutions) {
    [void]$drift.Add("Flux 分辨率表漂移：脚本=$wantFlux  generator.py=$($actual.FluxResolutions)")
  }

  if ($actual.StubEngine -ne $script:StubEngine) {
    [void]$drift.Add("stub engine 漂移：脚本=$script:StubEngine  generator.py=$($actual.StubEngine)")
  }

  return $drift.ToArray()
}

# ══════════════════════════════════════════════════════════════════════
# 响应与产物断言
# ══════════════════════════════════════════════════════════════════════

function Get-StubReasons {
  <#
    返回「这是占位图产物」的证据列表；空列表表示确实走了 ComfyUI。

    这是整套验收最关键的一道防线：ComfyUI 不可用时后端会静默回落到 Pillow
    占位图并照常返回 200，不拦就会「所有断言全绿但什么都没生成」。
  #>
  param(
    # 不设 Mandatory：空 hashtable 也是合法输入（会被判为 stub）
    [hashtable]$Metadata,
    [hashtable]$Payload = $null
  )

  $reasons = New-Object System.Collections.ArrayList
  $engine = [string](Get-DeepValue $Metadata 'engine')

  if ($engine -eq $script:StubEngine) {
    [void]$reasons.Add("engine == `"$script:StubEngine`"")
  }
  if ($engine -ne 'comfyui') {
    [void]$reasons.Add("engine != `"comfyui`"（实际 '$engine'）")
  }
  if (-not $Metadata.ContainsKey('comfyui_prompt_id')) {
    [void]$reasons.Add('缺 comfyui_prompt_id')
  }
  if ((Get-DeepValue $Metadata 'fallback_tier') -eq -1) {
    [void]$reasons.Add('fallback_tier == -1')
  }
  if ([string]::IsNullOrEmpty([string](Get-DeepValue $Metadata 'workflow_used'))) {
    [void]$reasons.Add('workflow_used 为空')
  }

  $syn = Get-DeepValue $Metadata 'prompt_synthesis'
  if ($null -ne $syn -and (Get-DeepValue $syn 'applied') -eq $false) {
    [void]$reasons.Add('prompt_synthesis.applied == False')
  }

  if ($null -ne $Payload) {
    $api = Get-DeepValue $Payload 'workflow_api'
    # 注意：@($api).Count 对空 hashtable 会算出 1（把一个空表包成了单元素数组），
    # 必须按类型分别判空。
    $apiEmpty = $null -eq $api
    if (-not $apiEmpty -and $api -is [System.Collections.IDictionary]) { $apiEmpty = ($api.Count -eq 0) }
    if (-not $apiEmpty -and $api -is [System.Collections.IEnumerable] -and $api -isnot [string]) {
      $apiEmpty = (@($api).Count -eq 0)
    }
    if ($apiEmpty) { [void]$reasons.Add('响应顶层 workflow_api 为空') }
  }

  return $reasons.ToArray()
}

function Get-LatentSize {
  <# 从 workflow_api 里找 latent 节点，返回 @(宽,高)；找不到返回 $null。 #>
  param($WorkflowApi)

  if ($null -eq $WorkflowApi) { return $null }

  $nodes = if ($WorkflowApi -is [System.Collections.IDictionary]) { @($WorkflowApi.Values) } else { @($WorkflowApi) }

  foreach ($node in $nodes) {
    $classType = [string](Get-DeepValue $node 'class_type')
    if ($script:LatentClassTypes -notcontains $classType) { continue }

    $inputs = Get-DeepValue $node 'inputs'
    $width = Get-DeepValue $inputs 'width'
    $height = Get-DeepValue $inputs 'height'
    if ($width -is [int] -and $height -is [int]) { return @($width, $height) }
  }

  return $null
}

function Get-DataUrlPngSize {
  <# 从 data URL（或裸 base64）读 PNG 宽高，只解文件头，不依赖图像库。 #>
  param([Parameter(Mandatory = $true)][string]$DataUrl)

  $payload = $DataUrl
  if ($DataUrl.StartsWith('data:')) {
    $comma = $DataUrl.IndexOf(',')
    if ($comma -ge 0) { $payload = $DataUrl.Substring($comma + 1) }
  }

  $bytes = [Convert]::FromBase64String($payload)
  if ($bytes.Length -lt 24) { throw "不是 PNG：字节数不足（$($bytes.Length) < 24）" }

  $signature = [byte[]](0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
  for ($i = 0; $i -lt 8; $i++) {
    if ($bytes[$i] -ne $signature[$i]) { throw "不是 PNG：文件头签名不匹配" }
  }

  $width = ([int]$bytes[16] -shl 24) -bor ([int]$bytes[17] -shl 16) -bor ([int]$bytes[18] -shl 8) -bor [int]$bytes[19]
  $height = ([int]$bytes[20] -shl 24) -bor ([int]$bytes[21] -shl 16) -bor ([int]$bytes[22] -shl 8) -bor [int]$bytes[23]
  return @($width, $height)
}

function Test-SvgStructure {
  <#
    纯结构校验（不渲染）：XML 可解析、有 <svg> 根、viewBox 有效、
    含真实矢量元素、且不含 <image> 或 base64 位图伪装。
  #>
  param([Parameter(Mandatory = $true)][string]$SvgText)

  $fail = {
    param([string]$Reason)
    return [PSCustomObject]@{ Ok = $false; Reason = $Reason; PathCount = 0 }
  }

  $doc = $null
  try {
    $doc = [xml]$SvgText
  } catch {
    return (& $fail "XML 解析失败：$($_.Exception.Message)")
  }

  $root = $doc.DocumentElement
  if ($null -eq $root -or -not $root.Name.EndsWith('svg')) {
    return (& $fail '根元素不是 <svg>')
  }

  $viewBox = $root.GetAttribute('viewBox')
  if ([string]::IsNullOrWhiteSpace($viewBox)) { return (& $fail '缺 viewBox') }

  $numbers = @()
  try {
    $numbers = @($viewBox -split '[\s,]+' | Where-Object { $_ -ne '' } | ForEach-Object { [double]$_ })
  } catch {
    return (& $fail "viewBox 不可解析：$viewBox")
  }
  if ($numbers.Count -ne 4 -or $numbers[2] -le 0 -or $numbers[3] -le 0) {
    return (& $fail "viewBox 非法（应为 4 个正数）：$viewBox")
  }

  if ($SvgText -match '<image[\s>]') { return (& $fail '含 <image>（位图伪装）') }
  if ($SvgText -match 'data:image' -or $SvgText -match 'base64') {
    return (& $fail '含 base64 位图数据')
  }

  $paths = $doc.SelectNodes("//*[local-name()='path']")
  if ($paths.Count -eq 0) { return (& $fail '没有任何 <path>') }

  return [PSCustomObject]@{ Ok = $true; Reason = 'ok'; PathCount = $paths.Count }
}

# ══════════════════════════════════════════════════════════════════════
# 结果收集
# ══════════════════════════════════════════════════════════════════════

function Test-DetailedOutput {
  <# -Verbose 时才输出技术细节；默认只给面向用户的信息。 #>
  return ($VerbosePreference -ne 'SilentlyContinue')
}

function Format-Duration {
  <# 秒数转成人能读的时长：42 秒 / 1 分 35 秒 / 1 小时 12 分。 #>
  param([double]$Seconds)

  $total = [int][Math]::Round($Seconds)
  if ($total -lt 60) { return "$total 秒" }

  $minutes = [int][Math]::Floor($total / 60)
  $rest = $total % 60
  if ($minutes -lt 60) {
    if ($rest -eq 0) { return "$minutes 分" }
    return "$minutes 分 $rest 秒"
  }

  $hours = [int][Math]::Floor($minutes / 60)
  $minutes = $minutes % 60
  if ($minutes -eq 0) { return "$hours 小时" }
  return "$hours 小时 $minutes 分"
}

function Format-ServiceName {
  <# 内部服务名转成用户能理解的名字。 #>
  param([string]$Name)

  switch -Regex ($Name) {
    'txt2img' { return '图像生成服务' }
    'vectorizer' { return '矢量化服务' }
    'ComfyUI' { return '绘图引擎' }
    default { return $Name }
  }
}

function Write-Stage {
  param([string]$Text)
  Write-Host ''
  Write-Host $Text -ForegroundColor Cyan
}

function Write-Note {
  <# 缩进的进度提示，例如「正在生成图片…（已等待 15 秒）」。 #>
  param([string]$Text)
  Write-Host "      $Text" -ForegroundColor DarkGray
}

function Write-Outcome {
  <# 面向用户的结论行：通过 / 失败 / 警告 / 跳过。 #>
  param(
    [ValidateSet('通过', '失败', '警告', '跳过')][string]$Status,
    [string]$Text,
    [string]$Detail = ''
  )

  $color = switch ($Status) {
    '通过' { 'Green' }
    '警告' { 'Yellow' }
    '跳过' { 'DarkGray' }
    default { 'Red' }
  }
  $suffix = if ([string]::IsNullOrWhiteSpace($Detail)) { '' } else { "  ($Detail)" }
  Write-Host "  [$Status] $Text$suffix" -ForegroundColor $color
}

function New-ScenarioResult {
  param([string]$Name)
  return [PSCustomObject]@{
    Name      = $Name
    Status    = 'PASS'
    Checks    = (New-Object System.Collections.ArrayList)
    Artifacts = (New-Object System.Collections.ArrayList)
    Elapsed   = 0.0
    Error     = ''
    # 供历史索引使用的结构化信息（产物路径、输入、耗时），由场景函数填充
    Data      = @{}
  }
}

function Add-Check {
  <#
    记录一条断言。

    默认只在出问题时打印（FAIL / WARN），避免几十行绿色 PASS 淹没进度；
    -Verbose 时逐条打印，保留完整技术细节。报告文件始终记录全部检查项。
  #>
  param(
    [Parameter(Mandatory = $true)]$Result,
    [Parameter(Mandatory = $true)][bool]$Ok,
    [Parameter(Mandatory = $true)][string]$Label,
    [string]$Detail = '',
    [switch]$WarnOnly,
    # 面向用户的说法；默认输出失败时用它替代 Label + Detail（技术原因仍进报告）
    [string]$DisplayText = ''
  )

  $status = if ($Ok) { 'PASS' } elseif ($WarnOnly) { 'WARN' } else { 'FAIL' }
  if (-not $Ok -and -not $WarnOnly) { $Result.Status = 'FAIL' }
  if (-not $Ok -and $WarnOnly -and $Result.Status -eq 'PASS') { $Result.Status = 'WARN' }

  [void]$Result.Checks.Add([PSCustomObject]@{ Label = $Label; Status = $status; Detail = $Detail })

  if ($status -ne 'PASS' -or (Test-DetailedOutput)) {
    $statusText = switch ($status) { 'PASS' { '通过' } 'WARN' { '警告' } default { '失败' } }
    $color = switch ($status) { 'PASS' { 'Green' } 'WARN' { 'Yellow' } default { 'Red' } }
    if (-not (Test-DetailedOutput) -and -not [string]::IsNullOrWhiteSpace($DisplayText)) {
      Write-Host "  [$statusText] $DisplayText" -ForegroundColor $color
    } else {
      $suffix = if ([string]::IsNullOrWhiteSpace($Detail)) { '' } else { "  ($Detail)" }
      Write-Host "  [$statusText] $Label$suffix" -ForegroundColor $color
    }
  }

  return $Ok
}

# ══════════════════════════════════════════════════════════════════════
# HTTP
# ══════════════════════════════════════════════════════════════════════

function Invoke-JsonPost {
  <#
    POST JSON 并返回 @{ Status; Json; Text; Ok; ElapsedSec }。

    单次生图要 91-285 秒，安静等待会让用户以为脚本卡死，所以把请求丢到
    独立 runspace 执行，主线程每 15 秒打印一次已等待秒数。
  #>
  param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][hashtable]$Body,
    [int]$TimeoutSec = 1800,
    [string]$Label = '请求中'
  )

  $json = $Body | ConvertTo-Json -Depth 20 -Compress

  $ps = [PowerShell]::Create()
  $ps.Runspace = [RunspaceFactory]::CreateRunspace()
  $ps.Runspace.Open()
  [void]$ps.AddScript({
      param($targetUrl, $payload, $timeoutSec)
      $params = @{
        Uri             = $targetUrl
        Method          = 'POST'
        Body            = [System.Text.Encoding]::UTF8.GetBytes($payload)
        ContentType     = 'application/json; charset=utf-8'
        TimeoutSec      = $timeoutSec
        UseBasicParsing = $true
      }
      try {
        $resp = Invoke-WebRequest @params
        [PSCustomObject]@{ Status = [int]$resp.StatusCode; Text = $resp.Content }
      } catch {
        $webResp = $_.Exception.Response
        if ($null -ne $webResp) {
          $reader = New-Object System.IO.StreamReader($webResp.GetResponseStream())
          [PSCustomObject]@{ Status = [int]$webResp.StatusCode; Text = $reader.ReadToEnd() }
        } else {
          [PSCustomObject]@{ Status = 0; Text = $_.Exception.Message }
        }
      }
    }).AddArgument($Url).AddArgument($json).AddArgument($TimeoutSec)

  $handle = $ps.BeginInvoke()
  $watch = [System.Diagnostics.Stopwatch]::StartNew()
  $nextReport = 15

  try {
    while (-not $handle.IsCompleted) {
      Start-Sleep -Milliseconds 500
      $elapsed = $watch.Elapsed.TotalSeconds
      if ($elapsed -ge $nextReport) {
        Write-Note ("{0}…（已等待 {1} 秒）" -f $Label, [int]$elapsed)
        $nextReport += 15
      }
    }
    $output = $ps.EndInvoke($handle)
  } finally {
    $ps.Dispose()
  }

  $elapsedSec = $watch.Elapsed.TotalSeconds
  $result = if ($output) { $output[0] } else { $null }
  if ($null -eq $result) {
    return [PSCustomObject]@{ Status = 0; Json = $null; Text = '空响应'; Ok = $false; ElapsedSec = $elapsedSec }
  }

  $parsed = $null
  try { $parsed = ConvertTo-DeepHashtable ($result.Text | ConvertFrom-Json) } catch { $parsed = $null }

  return [PSCustomObject]@{
    Status     = [int]$result.Status
    Json       = $parsed
    Text       = $result.Text
    Ok         = ([int]$result.Status -ge 200 -and [int]$result.Status -lt 300)
    ElapsedSec = $elapsedSec
  }
}

function Test-HttpReady {
  <# 探测 GET 是否返回 200；用于后端 /healthz 与 ComfyUI /system_stats。 #>
  param([Parameter(Mandatory = $true)][string]$Url, [int]$TimeoutSec = 3)

  # 脚本自己的 -Verbose 不应把 Invoke-WebRequest 的内部日志一并带出来
  $VerbosePreference = 'SilentlyContinue'
  try {
    $resp = Invoke-WebRequest -Uri $Url -Method GET -TimeoutSec $TimeoutSec -UseBasicParsing
    return ([int]$resp.StatusCode -eq 200)
  } catch {
    return $false
  }
}

# ══════════════════════════════════════════════════════════════════════
# 环境门禁
# ══════════════════════════════════════════════════════════════════════

function Wait-BackendReady {
  <#
    等后端 /healthz 就绪。桌面端启动时会自动拉起后端，按钮路径通常秒过。

    只在「正在等待」时输出进度；成功与否由调用方统一打印面向用户的结论。
  #>
  param([string]$Base, [string]$Name, [int]$Seconds)

  $display = Format-ServiceName $Name
  $startedAt = Get-Date
  $deadline = $startedAt.AddSeconds($Seconds)
  $lastReport = -1

  do {
    if (Test-HttpReady -Url "$Base/healthz" -TimeoutSec 3) {
      Write-Verbose "[env] $Name ready at $Base"
      return $true
    }
    $waited = [int]((Get-Date) - $startedAt).TotalSeconds
    if ([int]($waited / 5) -ne $lastReport) {
      Write-Note "正在等待${display}启动…（已等待 ${waited} 秒）"
      $lastReport = [int]($waited / 5)
    }
    Start-Sleep -Seconds 1
  } while ((Get-Date) -lt $deadline)

  Write-Verbose "[env] $Name not ready within ${Seconds}s at $Base/healthz"
  return $false
}

function Test-ComfyUiGate {
  <#
    生成场景的前置门禁。ComfyUI 没起就拒绝跑——否则后端静默回落占位图，
    验收会假通过。冷启动可能 1-3 分钟，这里等满 ComfyUiWaitSec 秒。
  #>
  param($Result, [string]$Host_, [int]$TimeoutSec)

  if (Test-HttpReady -Url "$Host_/system_stats" -TimeoutSec 5) {
    Write-Verbose "[gate] ComfyUI ready at $Host_"
    return $true
  }

  Write-Note '绘图引擎正在启动…（冷启动可能需要 1-3 分钟）'
  $startedAt = Get-Date
  $deadline = $startedAt.AddSeconds($TimeoutSec)
  $lastReport = -1
  while ((Get-Date) -lt $deadline) {
    if (Test-HttpReady -Url "$Host_/system_stats" -TimeoutSec 5) {
      Write-Verbose "[gate] ComfyUI ready"
      return $true
    }
    $waited = [int]((Get-Date) - $startedAt).TotalSeconds
    if ([int]($waited / 10) -ne $lastReport) {
      Write-Note "正在等待绘图引擎…（已等待 ${waited} 秒）"
      $lastReport = [int]($waited / 10)
    }
    Start-Sleep -Seconds 2
  }

  Add-Check $Result $false '绘图引擎未就绪' '请打开桌面端（会自动启动绘图引擎）后重试'
  return $false
}

# ══════════════════════════════════════════════════════════════════════
# 场景一：单图矢量化
# ══════════════════════════════════════════════════════════════════════

function Invoke-SingleScenario {
  param($Ctx)

  $result = New-ScenarioResult -Name '单图矢量化（念奴娇 完整链路）'
  $watch = [System.Diagnostics.Stopwatch]::StartNew()
  $outDir = Join-Path $Ctx.ReportDir 'scenarios\01_single'

  if (-not (Test-ComfyUiGate -Result $result -Host_ $Ctx.ComfyUiHost -TimeoutSec $Ctx.ComfyUiWaitSec)) {
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  $body = @{
    text            = $script:SingleText
    prompt          = $script:SingleStyle
    negative_prompt = ''
    resolution      = '1024 x 1024'
    seed            = 0
    format          = 'PNG'
    workflow        = ''
  }

  Write-Note '正在生成图片…'
  Write-Verbose "  -> POST $($Ctx.Txt2ImgBase)/api/v1/txt2img  text=$script:SingleText"
  $resp = Invoke-JsonPost -Url "$($Ctx.Txt2ImgBase)/api/v1/txt2img" -Body $body `
    -TimeoutSec $Ctx.GenTimeoutSec -Label '正在生成图片'
  $generationMs = [int]($resp.ElapsedSec * 1000)

  if (-not (Add-Check $result ($resp.Status -eq 200) "生图返回 200（text=$script:SingleText）" `
        "HTTP $($resp.Status) $(Get-Snippet $resp.Text)" -DisplayText '图片生成失败')) {
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  $payload = $resp.Json
  $meta = Get-DeepValue $payload 'metadata'
  if ($null -eq $meta) { $meta = @{} }

  # 必须真走 ComfyUI —— 这是防「静默回落占位图导致假通过」的核心
  $reasons = @(Get-StubReasons -Metadata $meta -Payload $payload)
  $evidence = if ($reasons.Count -eq 0) {
    "engine=$(Get-DeepValue $meta 'engine') tier=$(Get-DeepValue $meta 'fallback_tier') workflow=$(Get-DeepValue $meta 'workflow_used')"
  } else {
    $reasons -join '；'
  }
  if (-not (Add-Check $result ($reasons.Count -eq 0) '真走 ComfyUI（非 stub）' $evidence -DisplayText '没有真正生成图片（服务回退了）')) {
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  Add-Check $result ((Get-DeepValue $meta 'workflow_used') -eq 'qwen_image_2512_gguf') `
    '路由命中 qwen（念奴娇是纯中文）' "workflow_used=$(Get-DeepValue $meta 'workflow_used')" `
    -DisplayText '生成引擎选择异常'

  $syn = Get-DeepValue $meta 'prompt_synthesis'
  Add-Check $result ((Get-DeepValue $syn 'text_profile') -eq 'chinese') `
    '文本分类为 chinese' "text_profile=$(Get-DeepValue $syn 'text_profile')" `
    -DisplayText '文本分类异常'

  # 请求 1024x1024，Qwen 会按宽高比吸附到官方尺寸
  $latent = Get-LatentSize -WorkflowApi (Get-DeepValue $payload 'workflow_api')
  $latentText = if ($null -eq $latent) { '<未找到 latent 节点>' } else { "$($latent[0])x$($latent[1])" }
  Add-Check $result ($null -ne $latent -and $latent[0] -eq 1328 -and $latent[1] -eq 1328) `
    'qwen 分辨率映射 1024x1024 -> 1328x1328' $latentText -DisplayText '生成尺寸与预期不符'

  # ── 产物落盘 ──
  $pngPath = Join-Path $outDir 'original.png'
  $imageBase64 = [string](Get-DeepValue $payload 'image_base64')
  if ([string]::IsNullOrWhiteSpace($imageBase64)) {
    Add-Check $result $false '响应含 image_base64' '' -DisplayText '服务没有返回图片'
  } else {
    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    try {
      $rawBytes = [Convert]::FromBase64String(($imageBase64 -replace '^data:[^,]*,', ''))
      [System.IO.File]::WriteAllBytes($pngPath, $rawBytes)
      $pngSize = Get-DataUrlPngSize -DataUrl $imageBase64
      $sizeText = "$($pngSize[0])x$($pngSize[1])"
      $matchLatent = ($null -ne $latent -and $pngSize[0] -eq $latent[0] -and $pngSize[1] -eq $latent[1])
      Add-Check $result $matchLatent 'original.png 像素尺寸 == latent 尺寸' "$sizeText vs $latentText" `
        -DisplayText '图片尺寸与预期不符'
      [void]$result.Artifacts.Add($pngPath)
    } catch {
      Add-Check $result $false 'original.png 落盘失败' $_.Exception.Message -DisplayText '图片保存失败'
    }
  }

  $vectorMs = 0
  $svgFidelity = $null
  $pngTransparency = $null
  if (Test-Path -LiteralPath $pngPath) {
    $vec = Invoke-Vectorize -Ctx $Ctx -PngPath $pngPath -ImageName 'single-niannujiao.png' -OutDir $outDir -Tag '单图'
    foreach ($check in $vec.Checks) {
      Add-Check $result ($check.Status -eq 'PASS') $check.Label $check.Detail -WarnOnly:($check.Status -eq 'WARN') -DisplayText $check.Display
    }
    foreach ($artifact in $vec.Artifacts) { [void]$result.Artifacts.Add($artifact) }
    $vectorMs = [int]($vec.ElapsedSec * 1000)
    $svgFidelity = $vec.SvgFidelity
    $pngTransparency = $vec.PngTransparency
  }

  # ── 写 metadata.json / run.log，并准备历史索引数据 ──
  # 历史面板恢复记录时按 paths 重新读盘，所以这里必须落齐四件套 + metadata。
  if (Test-Path -LiteralPath $pngPath) {
    $itemStatus = if ($result.Status -eq 'FAIL') { 'failed' } else { 'success' }
    Write-TaskMetadataFile -OutDir $outDir -GenerationMeta $meta `
      -Generation @{
        text        = $script:SingleText
        prompt      = $script:SingleStyle
        seed        = 0
        resolution  = '1024 x 1024'
        duration_ms = $generationMs
      } `
      -VectorMs $vectorMs -SvgFidelity $svgFidelity -PngTransparency $pngTransparency -Status $itemStatus

    $result.Data = @{
      Kind            = 'single'
      TaskDir         = $outDir
      Title           = "验收 · 单图（$($script:SingleText)）"
      Text            = $script:SingleText
      Prompt          = $script:SingleStyle
      Negative        = ''
      Seed            = 0
      Resolution      = '1024 x 1024'
      GenerationMs    = $generationMs
      VectorMs        = $vectorMs
      SvgFidelity     = $svgFidelity
      PngTransparency = $pngTransparency
      Status          = $itemStatus
      Paths           = @{
        original    = Join-Path $outDir 'original.png'
        transparent = Join-Path $outDir 'transparent.png'
        preview     = Join-Path $outDir 'preview.png'
        svg         = Join-Path $outDir 'result.svg'
        metadata    = Join-Path $outDir 'metadata.json'
        log         = Join-Path $outDir 'run.log'
      }
    }
  }

  $result.Elapsed = $watch.Elapsed.TotalSeconds
  return $result
}

# ══════════════════════════════════════════════════════════════════════
# 矢量化（单图与批量共用）
# ══════════════════════════════════════════════════════════════════════

function Write-TaskMetadataFile {
  <#
    写任务目录的 metadata.json 与 run.log。

    桌面端「历史任务」恢复一条记录时会读这两个文件：metadata 提供输入、
    耗时与质量分，run.log 提供分阶段耗时。字段形状对齐正常任务写出的结构，
    这样验收产物在历史面板里和普通结果表现一致。
  #>
  param(
    [string]$OutDir,
    [hashtable]$GenerationMeta,
    [hashtable]$Generation,
    [int]$VectorMs,
    $SvgFidelity,
    $PngTransparency,
    [string]$Status = 'success'
  )

  $doc = @{}
  if ($GenerationMeta) {
    foreach ($key in $GenerationMeta.Keys) { $doc[$key] = $GenerationMeta[$key] }
  }
  $doc['generation'] = $Generation
  $doc['stats'] = @{ elapsed_ms = $VectorMs }
  $doc['quality'] = @{ svg_fidelity = $SvgFidelity }
  $doc['preprocess'] = @{ png_transparency = $PngTransparency }

  New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
  [System.IO.File]::WriteAllText((Join-Path $OutDir 'metadata.json'),
    ($doc | ConvertTo-Json -Depth 20), (New-Object System.Text.UTF8Encoding($false)))

  $runLog = @(
    "status=$Status",
    "generation_ms=$([int]$Generation['duration_ms'])",
    "vector_ms=$VectorMs"
  )
  [System.IO.File]::WriteAllLines((Join-Path $OutDir 'run.log'), $runLog, (New-Object System.Text.UTF8Encoding($false)))
}

function Invoke-Vectorize {
  <#
    调用 vectorizer 并把 transparent.png / preview.png / result.svg 落盘。
    返回一个只含 Checks/Artifacts 的轻量结果，由调用方并入自己的场景结果。
  #>
  param($Ctx, [string]$PngPath, [string]$ImageName, [string]$OutDir, [string]$Tag)

  $sub = [PSCustomObject]@{
    Checks          = (New-Object System.Collections.ArrayList)
    Artifacts       = (New-Object System.Collections.ArrayList)
    # 写历史索引时需要：耗时与质量分
    ElapsedSec      = 0.0
    SvgFidelity     = $null
    PngTransparency = $null
  }

  $body = @{
    source_type = 'upload'
    image_path  = $PngPath
    image_name  = $ImageName
    vector      = @{ preset = $Ctx.VectorPreset }
  }

  Write-Note '正在矢量化…'
  Write-Verbose "  -> POST $($Ctx.VectorizerBase)/api/v1/vectorize  $ImageName"
  $resp = Invoke-JsonPost -Url "$($Ctx.VectorizerBase)/api/v1/vectorize" -Body $body `
    -TimeoutSec $Ctx.VectorTimeoutSec -Label '正在矢量化'

  $ok = ($resp.Status -eq 200)
  [void]$sub.Checks.Add([PSCustomObject]@{
      Label   = "$Tag · 矢量化返回 200"
      Status  = if ($ok) { 'PASS' } else { 'FAIL' }
      Detail  = "HTTP $($resp.Status)"
      Display = "$Tag · 矢量化失败"
    })
  if (-not $ok) { return $sub }

  $sub.ElapsedSec = $resp.ElapsedSec
  $payload = $resp.Json
  foreach ($field in @('transparent_png', 'preview_png', 'png')) {
    $value = [string](Get-DeepValue $payload $field)
    $valid = $value.StartsWith('data:image/png;base64,')
    [void]$sub.Checks.Add([PSCustomObject]@{
        Label   = "$Tag · $field 是 data:image/png;base64"
        Status  = if ($valid) { 'PASS' } else { 'FAIL' }
        Detail  = ''
        Display = "$Tag · 矢量化结果缺少图片"
      })
  }

  $svg = [string](Get-DeepValue $payload 'svg')
  [void]$sub.Checks.Add([PSCustomObject]@{
      Label   = "$Tag · svg 非空"
      Status  = if ($svg.StartsWith('<')) { 'PASS' } else { 'FAIL' }
      Detail  = if ($svg) { "{0:N1} KB" -f ($svg.Length / 1024) } else { '空' }
      Display = "$Tag · 没有生成 SVG"
    })
  if (-not $svg.StartsWith('<')) { return $sub }

  New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
  try {
    $transparentBytes = [Convert]::FromBase64String((([string](Get-DeepValue $payload 'transparent_png')) -replace '^data:[^,]*,', ''))
    [System.IO.File]::WriteAllBytes((Join-Path $OutDir 'transparent.png'), $transparentBytes)
    $previewBytes = [Convert]::FromBase64String((([string](Get-DeepValue $payload 'preview_png')) -replace '^data:[^,]*,', ''))
    [System.IO.File]::WriteAllBytes((Join-Path $OutDir 'preview.png'), $previewBytes)
    [System.IO.File]::WriteAllText((Join-Path $OutDir 'result.svg'), $svg, (New-Object System.Text.UTF8Encoding($false)))
    [void]$sub.Artifacts.Add((Join-Path $OutDir 'result.svg'))

    $transparentSize = Get-DataUrlPngSize -DataUrl ([string](Get-DeepValue $payload 'transparent_png'))
    [void]$sub.Checks.Add([PSCustomObject]@{
        Label   = "$Tag · 透明图可解码"
        Status  = if ($transparentSize[0] -gt 0 -and $transparentSize[1] -gt 0) { 'PASS' } else { 'FAIL' }
        Detail  = "$($transparentSize[0])x$($transparentSize[1])"
        Display = "$Tag · 透明图无法读取"
      })
  } catch {
    [void]$sub.Checks.Add([PSCustomObject]@{
        Label = "$Tag · 矢量化解码失败"; Status = 'FAIL'; Detail = $_.Exception.Message
        Display = "$Tag · 矢量化结果无法保存"
      })
  }

  # SVG 结构校验：SVG 场景不单独跑，并入这里
  $structure = Test-SvgStructure -SvgText $svg
  [void]$sub.Checks.Add([PSCustomObject]@{
      Label   = "$Tag · SVG 结构合法"
      Status  = if ($structure.Ok) { 'PASS' } else { 'FAIL' }
      Detail  = if ($structure.Ok) { "$($structure.PathCount) 条 path" } else { $structure.Reason }
      Display = "$Tag · SVG 内容不完整"
    })

  $vecMetadata = Get-DeepValue $payload 'metadata'
  $quality = Get-DeepValue $vecMetadata 'quality'
  $fidelity = Get-DeepValue $quality 'svg_fidelity'
  $sub.SvgFidelity = $fidelity
  $sub.PngTransparency = Get-DeepValue (Get-DeepValue $vecMetadata 'preprocess') 'png_transparency'
  if ($null -eq $fidelity) {
    [void]$sub.Checks.Add([PSCustomObject]@{
        Label = "$Tag · svg_fidelity"; Status = 'WARN'; Detail = '为 null（允许）'
        Display = "$Tag · 质量评分缺失（不影响结果）"
      })
  } else {
    $inRange = ($fidelity -is [double] -or $fidelity -is [int] -or $fidelity -is [decimal]) -and $fidelity -ge 0 -and $fidelity -le 100
    [void]$sub.Checks.Add([PSCustomObject]@{
        Label   = "$Tag · svg_fidelity 在 [0,100]"
        Status  = if ($inRange) { 'PASS' } else { 'FAIL' }
        Detail  = "$fidelity"
        Display = "$Tag · 质量评分异常"
      })
  }

  return $sub
}

# ══════════════════════════════════════════════════════════════════════
# 场景二：批量生成（3 条，三条路由链全命中）
# ══════════════════════════════════════════════════════════════════════

function Invoke-BatchScenario {
  param($Ctx)

  $result = New-ScenarioResult -Name '批量生成（3 条，覆盖三条路由链）'
  $watch = [System.Diagnostics.Stopwatch]::StartNew()
  $fixture = Resolve-FixturePath -Name 'routes.txt'
  $root = Join-Path $Ctx.ReportDir 'scenarios\02_batch'

  $rows = @(Read-RoutesFixture -Path $fixture)
  if (-not (Add-Check $result ($rows.Count -eq 3) 'fixture 解析出 3 条' "$($rows.Count) 条" -DisplayText '测试输入不完整')) {
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  if (-not (Test-ComfyUiGate -Result $result -Host_ $Ctx.ComfyUiHost -TimeoutSec $Ctx.ComfyUiWaitSec)) {
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  # 逐条产物记录：供桌面端「历史任务」的批量条目使用
  $itemRecords = New-Object System.Collections.ArrayList
  # 回填到表单的批量输入（文本 | 提示词 | 负面 | seed | 分辨率）
  $batchInputLines = @()

  for ($i = 0; $i -lt $rows.Count; $i++) {
    $row = $rows[$i]
    $index = $i + 1
    $text = $row.Text
    $expectedWorkflow = $row.ExpectedWorkflow
    Write-Host ''
    Write-Note "第 $index/3 条：$text"
    Write-Verbose "  --- 第 $index/3 条：$text（期望 $expectedWorkflow）"
    $itemWatch = [System.Diagnostics.Stopwatch]::StartNew()

    $outDir = Join-Path $root ("{0}_{1}" -f $index, ($text -replace '\s', '_'))

    $body = @{
      text            = $text
      prompt          = $row.Prompt
      negative_prompt = $row.Negative
      resolution      = $row.Resolution
      seed            = [int]$row.Seed
      format          = 'PNG'
      workflow        = ''
    }

    Write-Note '正在生成图片…'
    $resp = Invoke-JsonPost -Url "$($Ctx.Txt2ImgBase)/api/v1/txt2img" -Body $body `
      -TimeoutSec $Ctx.GenTimeoutSec -Label '正在生成图片'
    $generationMs = [int]($resp.ElapsedSec * 1000)

    $batchInputLines += ("{0} | {1} | {2} | {3} | {4}" -f $text, $row.Prompt, $row.Negative, $row.Seed, $row.Resolution)

    if (-not (Add-Check $result ($resp.Status -eq 200) "#$index $text · 生图返回 200" "HTTP $($resp.Status)" -DisplayText "#$index $text · 图片生成失败")) { continue }

    $payload = $resp.Json
    $meta = Get-DeepValue $payload 'metadata'
    if ($null -eq $meta) { $meta = @{} }

    $reasons = @(Get-StubReasons -Metadata $meta -Payload $payload)
    if (-not (Add-Check $result ($reasons.Count -eq 0) "#$index $text · 真走 ComfyUI" ($reasons -join '；') `
          -DisplayText "#$index $text · 没有真正生成图片（服务回退了）")) { continue }

    $tier = Get-DeepValue $meta 'fallback_tier'
    Add-Check $result ($tier -eq 0) "#$index $text · 首选工作流即命中（tier=0）" "tier=$tier" `
      -DisplayText "#$index $text · 生成引擎选择异常"

    $got = [string](Get-DeepValue $meta 'workflow_used')
    Add-Check $result ($got -eq $expectedWorkflow) "#$index $text · workflow_used == $expectedWorkflow" "实际 $got" `
      -DisplayText "#$index $text · 生成引擎选择异常"

    # text_profile 是「文本分类」（chinese/english/mixed），不是渲染模板名
    $textProfile = Get-DeepValue (Get-DeepValue $meta 'prompt_synthesis') 'text_profile'
    Add-Check $result ($textProfile -eq $row.ExpectedProfile) "#$index $text · 文本分类 == $($row.ExpectedProfile)" "实际 $textProfile" `
      -DisplayText "#$index $text · 文本分类异常"

    $wantLatent = if ($expectedWorkflow.StartsWith('qwen')) { @(1328, 1328) } else { @(1024, 1024) }
    $latent = Get-LatentSize -WorkflowApi (Get-DeepValue $payload 'workflow_api')
    $latentText = if ($null -eq $latent) { '<未找到 latent 节点>' } else { "$($latent[0])x$($latent[1])" }
    Add-Check $result ($null -ne $latent -and $latent[0] -eq $wantLatent[0] -and $latent[1] -eq $wantLatent[1]) `
      "#$index $text · latent == $($wantLatent[0])x$($wantLatent[1])" "实际 $latentText" `
      -DisplayText "#$index $text · 生成尺寸与预期不符"

    $pngPath = Join-Path $outDir 'original.png'
    $imageBase64 = [string](Get-DeepValue $payload 'image_base64')
    if ([string]::IsNullOrWhiteSpace($imageBase64)) {
      Add-Check $result $false "#$index $text · 响应含 image_base64" '' -DisplayText "#$index $text · 服务没有返回图片"
      continue
    }

    New-Item -ItemType Directory -Path $outDir -Force | Out-Null
    try {
      [System.IO.File]::WriteAllBytes($pngPath, [Convert]::FromBase64String(($imageBase64 -replace '^data:[^,]*,', '')))
      $pngSize = Get-DataUrlPngSize -DataUrl $imageBase64
      $sizeMatch = ($null -ne $latent -and $pngSize[0] -eq $latent[0] -and $pngSize[1] -eq $latent[1])
      Add-Check $result $sizeMatch "#$index $text · original.png 尺寸 == latent" "$($pngSize[0])x$($pngSize[1])" `
        -DisplayText "#$index $text · 图片尺寸与预期不符"
      [void]$result.Artifacts.Add($pngPath)
    } catch {
      Add-Check $result $false "#$index $text · original.png 落盘失败" $_.Exception.Message `
        -DisplayText "#$index $text · 图片保存失败"
      continue
    }

    $vec = Invoke-Vectorize -Ctx $Ctx -PngPath $pngPath -ImageName "batch-$index.png" -OutDir $outDir -Tag "#$index $text"
    foreach ($check in $vec.Checks) {
      Add-Check $result ($check.Status -eq 'PASS') $check.Label $check.Detail -WarnOnly:($check.Status -eq 'WARN') -DisplayText $check.Display
    }
    foreach ($artifact in $vec.Artifacts) { [void]$result.Artifacts.Add($artifact) }

    # ── 落 metadata / run.log，并登记这条产物 ──
    $vectorMs = [int]($vec.ElapsedSec * 1000)
    $hasSvg = Test-Path -LiteralPath (Join-Path $outDir 'result.svg')
    $itemStatus = if ($hasSvg) { 'success' } else { 'failed' }
    $itemError = if ($itemStatus -eq 'failed') {
      (@($result.Checks | Where-Object { $_.Status -eq 'FAIL' -and $_.Label -like "#$index *" } |
          ForEach-Object { $_.Label }) -join '；')
    } else { '' }

    Write-TaskMetadataFile -OutDir $outDir -GenerationMeta $meta `
      -Generation @{
        text        = $text
        prompt      = $row.Prompt
        seed        = [int]$row.Seed
        resolution  = $row.Resolution
        duration_ms = $generationMs
      } `
      -VectorMs $vectorMs -SvgFidelity $vec.SvgFidelity -PngTransparency $vec.PngTransparency -Status $itemStatus

    [void]$itemRecords.Add(@{
        index           = $i
        taskDir         = $outDir
        taskName        = (Split-Path -Leaf $outDir)
        outputRoot      = $Ctx.OutputRoot
        paths           = @{
          original    = Join-Path $outDir 'original.png'
          transparent = Join-Path $outDir 'transparent.png'
          preview     = Join-Path $outDir 'preview.png'
          svg         = Join-Path $outDir 'result.svg'
          metadata    = Join-Path $outDir 'metadata.json'
          log         = Join-Path $outDir 'run.log'
        }
        text            = $text
        prompt          = $row.Prompt
        seed            = [int]$row.Seed
        generationMs    = $generationMs
        vectorMs        = $vectorMs
        status          = $itemStatus
        error           = $itemError
        rawStatus       = $itemStatus
        pngTransparency = $vec.PngTransparency
        svgFidelity     = $vec.SvgFidelity
      })

    $itemSeconds = $itemWatch.Elapsed.TotalSeconds
    Write-Verbose ("  [done] item finished in {0:N1}s" -f $itemSeconds)
    if ($Ctx.MaxE2eSeconds -gt 0 -and $itemSeconds -gt $Ctx.MaxE2eSeconds) {
      Add-Check $result $false "#$index $text · 耗时超阈值" ("{0:N1}s > {1}s" -f $itemSeconds, $Ctx.MaxE2eSeconds) `
        -DisplayText "#$index $text · 生成耗时过长"
    }
  }

  # 批量条目：历史面板点开后可在结果区逐条切换查看
  if ($itemRecords.Count -gt 0) {
    $result.Data = @{
      Kind       = 'batch'
      TaskDir    = $root
      Title      = "验收 · 批量（$($itemRecords.Count) 条）"
      BatchInput = ($batchInputLines -join "`n")
      Items      = @($itemRecords)
      Negative   = ''
      Resolution = '1024 x 1024'
      Seed       = 0
    }
  }

  $result.Elapsed = $watch.Elapsed.TotalSeconds
  return $result
}

# ══════════════════════════════════════════════════════════════════════
# 300 条完整套件（-Full 时追加；以下为原 run-acceptance.ps1 的核验机器）
# ══════════════════════════════════════════════════════════════════════

function Resolve-AbsolutePath {
  param([string]$Value)
  if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
  if ([System.IO.Path]::IsPathRooted($Value)) { return $Value }
  return (Join-Path $RepoRoot $Value)
}

function Resolve-CliExecutable {
  param([string]$ExplicitPath)

  if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
    $resolved = Resolve-AbsolutePath $ExplicitPath
    if (Test-Path -LiteralPath $resolved) { return (Resolve-Path -LiteralPath $resolved).Path }
    throw "CLI not found: $ExplicitPath"
  }

  $candidates = @(
    (Join-Path $ScriptDir "..\gen2vec_cli.exe"),
    (Join-Path $ScriptDir "..\gen2vec-cli.exe"),
    (Join-Path $ScriptDir "gen2vec_cli.exe"),
    (Join-Path $ScriptDir "gen2vec-cli.exe"),
    (Join-Path $RepoRoot "gen2vec_cli.exe"),
    (Join-Path $RepoRoot "gen2vec-cli.exe"),
    (Join-Path $RepoRoot "apps\cli\dist\gen2vec_cli.exe")
  )

  foreach ($candidate in $candidates) {
    if (Test-Path -LiteralPath $candidate) { return (Resolve-Path -LiteralPath $candidate).Path }
  }

  foreach ($name in @("gen2vec_cli.exe", "gen2vec-cli.exe")) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
  }

  throw "gen2vec_cli.exe was not found. Run this script from the install directory, or pass -CliPath."
}

function Test-LiteralPathExists {
  param([string]$Path)
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  return Test-Path -LiteralPath $Path
}

function New-PreparedFixture {
  <# 把 fixture 规范化成 CLI 支持的 `文本 | 提示词 | 负面 | seed | resolution` 五列文本。 #>
  param(
    [string]$FixturePath,
    [string]$PreparedPath,
    [hashtable]$Config
  )

  if (-not (Test-LiteralPathExists $FixturePath)) { throw "Fixture not found: $FixturePath" }

  $rows = @()
  foreach ($line in Get-Content -LiteralPath $FixturePath -Encoding UTF8) {
    $row = ConvertFrom-AcceptanceLine -Line $line
    if ($null -eq $row) { continue }
    if ([string]::IsNullOrWhiteSpace($row.Text) -and [string]::IsNullOrWhiteSpace($row.Prompt)) { continue }
    $rows += $row
  }

  if ($Config.ExpectedRows -gt 0 -and $rows.Count -ne $Config.ExpectedRows) {
    throw "$($Config.Label) expected $($Config.ExpectedRows) rows, got $($rows.Count)."
  }
  if ($rows.Count -ge $Config.MinRows) {
    Write-Verbose "  $($Config.Label) row count ok: $($rows.Count) rows, minimum is $($Config.MinRows)."
  }

  New-Item -ItemType Directory -Path (Split-Path -Parent $PreparedPath) -Force | Out-Null
  $outLines = foreach ($row in $rows) {
    $parts = @($row.Text, $row.Prompt, $row.Negative, $row.Seed, $row.Resolution)
    while ($parts.Count -gt 2 -and [string]::IsNullOrWhiteSpace($parts[$parts.Count - 1])) {
      $parts = $parts[0..($parts.Count - 2)]
    }
    $parts -join " | "
  }
  [System.IO.File]::WriteAllLines($PreparedPath, $outLines, [System.Text.UTF8Encoding]::new($false))
  return $rows
}

function Format-CommandLine {
  param([string]$Exe, [string[]]$CommandArgs)
  $quotedArgs = $CommandArgs | ForEach-Object {
    if ($_ -match "\s") { '"' + ($_ -replace '"', '\"') + '"' } else { $_ }
  }
  return '"' + $Exe + '" ' + ($quotedArgs -join " ")
}

function Get-LatestSummary {
  param([string]$Root)
  $summary = Get-ChildItem -LiteralPath $Root -Recurse -Filter "batch_summary.csv" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  if (-not $summary) { throw "batch_summary.csv not found: $Root" }
  return $summary.FullName
}

function Read-UInt32BE {
  param([byte[]]$Bytes, [int]$Offset)
  return (([uint32]$Bytes[$Offset] -shl 24) -bor ([uint32]$Bytes[$Offset + 1] -shl 16) -bor ([uint32]$Bytes[$Offset + 2] -shl 8) -bor [uint32]$Bytes[$Offset + 3])
}

function Read-PngInfo {
  param([string]$Path)
  $bytes = [System.IO.File]::ReadAllBytes($Path)
  $signature = [byte[]](0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
  if ($bytes.Length -lt 33) { throw "PNG file is too small: $Path" }
  for ($i = 0; $i -lt $signature.Length; $i++) {
    if ($bytes[$i] -ne $signature[$i]) { throw "Invalid PNG: $Path" }
  }

  $hasTrns = $false
  $offset = 8
  while ($offset + 12 -le $bytes.Length) {
    $length = Read-UInt32BE $bytes $offset
    $type = [System.Text.Encoding]::ASCII.GetString($bytes, $offset + 4, 4)
    if ($type -eq "tRNS") { $hasTrns = $true }
    $next = $offset + 12 + [int]$length
    if ($next -le $offset -or $next -gt $bytes.Length + 12) { break }
    $offset = $next
  }

  $width = Read-UInt32BE $bytes 16
  $height = Read-UInt32BE $bytes 20
  $colorType = [int]$bytes[25]
  return [PSCustomObject]@{
    Width    = $width
    Height   = $height
    HasAlpha = ($colorType -eq 4 -or $colorType -eq 6 -or $hasTrns)
  }
}

function Get-ResolutionObject {
  param([string]$Value)
  if ($Value -notmatch "(\d+)\D+(\d+)") { return $null }
  return [PSCustomObject]@{ Width = [int]$Matches[1]; Height = [int]$Matches[2] }
}

function Get-QwenMappedResolution {
  param([object]$Resolution)
  if (-not $Resolution -or $Resolution.Height -le 0) { return $null }
  $official = @(
    [PSCustomObject]@{ Width = 1328; Height = 1328 },
    [PSCustomObject]@{ Width = 1664; Height = 928 },
    [PSCustomObject]@{ Width = 928; Height = 1664 },
    [PSCustomObject]@{ Width = 1472; Height = 1104 },
    [PSCustomObject]@{ Width = 1104; Height = 1472 },
    [PSCustomObject]@{ Width = 1584; Height = 1056 },
    [PSCustomObject]@{ Width = 1056; Height = 1584 }
  )
  $ratio = [double]$Resolution.Width / [double]$Resolution.Height
  return $official |
    Sort-Object @{ Expression = { [Math]::Abs(([double]$_.Width / [double]$_.Height) - $ratio) } } |
    Select-Object -First 1
}

function Test-SizeEquals {
  param([object]$Png, [object]$Resolution)
  return $Resolution -and $Png.Width -eq $Resolution.Width -and $Png.Height -eq $Resolution.Height
}

function Add-ResolutionFinding {
  param(
    [System.Collections.Generic.List[string]]$Failures,
    [string]$Label,
    [string]$FileName,
    [object]$Png,
    [object]$Resolution
  )

  if (-not $Resolution -or (Test-SizeEquals $Png $Resolution)) { return }

  # Qwen 会把请求尺寸吸附到官方尺寸，属预期行为，记通过
  $mapped = Get-QwenMappedResolution $Resolution
  if ($mapped -and (Test-SizeEquals $Png $mapped)) { return }
  if ($AllowResolutionMismatch) { return }

  Add-Failure $Failures "$Label $FileName size $($Png.Width)x$($Png.Height) does not match requested $($Resolution.Width)x$($Resolution.Height)"
}

function Resolve-ArtifactPath {
  param([string]$Value, [string]$SummaryPath)
  if ([string]::IsNullOrWhiteSpace($Value)) { return "" }
  if ([System.IO.Path]::IsPathRooted($Value)) { return $Value }
  return Join-Path (Split-Path -Parent $SummaryPath) $Value
}

function Read-RunLog {
  param([string]$Path)
  $map = @{}
  foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
    $idx = $line.IndexOf("=")
    if ($idx -gt 0) {
      $map[$line.Substring(0, $idx)] = $line.Substring($idx + 1)
    }
  }
  return $map
}

function Add-Failure {
  param([System.Collections.Generic.List[string]]$Failures, [string]$Message)
  $Failures.Add($Message) | Out-Null
}

function Test-AcceptanceArtifacts {
  <# 核验 300 条套件的 batch_summary.csv 与每条任务的产物。 #>
  param(
    [string]$SummaryPath,
    [array]$ExpectedRows,
    [hashtable]$Config
  )

  $failures = [System.Collections.Generic.List[string]]::new()

  $rows = @(Import-Csv -LiteralPath $SummaryPath -Encoding UTF8)
  $resolution = Get-ResolutionObject $Resolution

  if ($rows.Count -ne $ExpectedRows.Count) {
    Add-Failure $failures "Summary CSV row count should be $($ExpectedRows.Count), got $($rows.Count): $SummaryPath"
  }

  $failedRows = @($rows | Where-Object { $_.status -eq "failed" -or -not [string]::IsNullOrWhiteSpace($_.error) })
  if ($failedRows.Count -gt $MaxFailures) {
    Add-Failure $failures "Failed row count $($failedRows.Count) exceeds threshold $MaxFailures"
  }

  $allowedStatuses = @("success", "degraded")
  for ($i = 0; $i -lt $rows.Count; $i++) {
    $row = $rows[$i]
    $expected = if ($i -lt $ExpectedRows.Count) { $ExpectedRows[$i] } else { $null }
    if ($row.status -ne "failed" -and $allowedStatuses -notcontains $row.status) {
      Add-Failure $failures "[$($i + 1)] status $($row.status) is not allowed: success,degraded"
    }
    if ($expected -and $row.text -ne $expected.Text) {
      Add-Failure $failures "[$($i + 1)] summary text mismatch: $($row.text) != $($expected.Text)"
    }
  }

  for ($i = 0; $i -lt $rows.Count; $i++) {
    $row = $rows[$i]
    $label = "[$($i + 1)] $($row.text)"
    $taskDir = Resolve-ArtifactPath $row.task_dir $SummaryPath
    $paths = @{
      original          = if ($row.original_path) { Resolve-ArtifactPath $row.original_path $SummaryPath } else { Join-Path $taskDir "original.png" }
      transparent       = if ($row.transparent_path) { Resolve-ArtifactPath $row.transparent_path $SummaryPath } else { Join-Path $taskDir "transparent.png" }
      svg               = if ($row.result_svg_path) { Resolve-ArtifactPath $row.result_svg_path $SummaryPath } else { Join-Path $taskDir "result.svg" }
      preview           = if ($row.preview_path) { Resolve-ArtifactPath $row.preview_path $SummaryPath } else { Join-Path $taskDir "preview.png" }
      metadata          = if ($row.metadata_path) { Resolve-ArtifactPath $row.metadata_path $SummaryPath } else { Join-Path $taskDir "metadata.json" }
      log               = Resolve-ArtifactPath $row.run_log_path $SummaryPath
      workflowApi       = Join-Path $taskDir "workflows\workflow_api.json"
      modelDependencies = Join-Path $taskDir "workflows\model_dependencies.json"
    }

    $required = if ($NoVectorize) {
      @("original", "metadata", "log", "workflowApi", "modelDependencies")
    } else {
      @("original", "transparent", "svg", "preview", "metadata", "log", "workflowApi", "modelDependencies")
    }

    foreach ($key in $required) {
      $pathValue = $paths[$key]
      if ([string]::IsNullOrWhiteSpace($pathValue) -or -not (Test-LiteralPathExists $pathValue)) {
        Add-Failure $failures "$label missing artifact: $key ($pathValue)"
      } elseif ((Get-Item -LiteralPath $pathValue).Length -le 0) {
        Add-Failure $failures "$label empty file: $key ($pathValue)"
      }
    }

    if (Test-LiteralPathExists $paths.original) {
      try {
        $png = Read-PngInfo $paths.original
        Add-ResolutionFinding $failures $label "original.png" $png $resolution
      } catch {
        Add-Failure $failures "$label original.png validation failed: $($_.Exception.Message)"
      }
    }

    if (-not $NoVectorize -and (Test-LiteralPathExists $paths.transparent)) {
      try {
        $png = Read-PngInfo $paths.transparent
        if (-not $png.HasAlpha) { Add-Failure $failures "$label transparent.png missing alpha channel" }
        Add-ResolutionFinding $failures $label "transparent.png" $png $resolution
      } catch {
        Add-Failure $failures "$label transparent.png validation failed: $($_.Exception.Message)"
      }
    }

    if (-not $NoVectorize -and (Test-LiteralPathExists $paths.preview)) {
      try {
        [void](Read-PngInfo $paths.preview)
      } catch {
        Add-Failure $failures "$label preview.png validation failed: $($_.Exception.Message)"
      }
    }

    if (-not $NoVectorize -and (Test-LiteralPathExists $paths.svg)) {
      $svg = Get-Content -LiteralPath $paths.svg -Raw -Encoding UTF8
      try {
        $svgDocument = [xml]$svg
        [void]$svgDocument
      } catch {
        Add-Failure $failures "$label SVG XML validation failed: $($_.Exception.Message)"
      }
      if ($svg -notmatch "<svg[\s>]") { Add-Failure $failures "$label result.svg missing <svg> root element" }

      if ($svg -match "<image[\s>]" -or $svg -match "data:image/[^;]+;base64") {
        Add-Failure $failures "$label result.svg contains bitmap <image> or base64 data"
      }
      if ($svg -notmatch "<(path|polygon|polyline|rect|circle|ellipse)\b") {
        Add-Failure $failures "$label result.svg missing editable vector elements"
      }
      if ($svg -notmatch "<g[\s>]") {
        if ($StrictSvgGroups) { Add-Failure $failures "$label result.svg has no <g> group" }
      }
    }

    $metadata = $null
    if (Test-LiteralPathExists $paths.metadata) {
      try {
        $metadata = Get-Content -LiteralPath $paths.metadata -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not $metadata.schema_version) { Add-Failure $failures "$label metadata.json missing schema_version" }
        if ($metadata.generation.text -and $row.text -and $metadata.generation.text -ne $row.text) {
          Add-Failure $failures "$label metadata.generation.text mismatch"
        }
      } catch {
        Add-Failure $failures "$label metadata.json is not valid JSON: $($_.Exception.Message)"
      }
    }

    if (Test-LiteralPathExists $paths.log) {
      $runLog = Read-RunLog $paths.log
      if (-not $runLog.ContainsKey("status")) { Add-Failure $failures "$label run.log missing status" }
      $stage1 = 0
      $stage2 = 0
      [void][int]::TryParse([string]$runLog["generation_ms"], [ref]$stage1)
      [void][int]::TryParse([string]$runLog["vector_ms"], [ref]$stage2)
      if ($MaxE2eMs -gt 0 -and ($stage1 + $stage2) -gt $MaxE2eMs) {
        Add-Failure $failures "$label end-to-end time $($stage1 + $stage2)ms exceeds $MaxE2eMs ms"
      }
      if (-not $NoVectorize -and $MaxVectorMs -gt 0 -and $stage2 -gt $MaxVectorMs) {
        Add-Failure $failures "$label vectorization time $stage2 ms exceeds $MaxVectorMs ms"
      }
    }

    if (-not $NoVectorize -and $metadata -and $metadata.stats -and $metadata.stats.elapsed_ms) {
      $elapsed = [double]$metadata.stats.elapsed_ms
      if ($MaxVectorMs -gt 0 -and $elapsed -gt $MaxVectorMs) {
        Add-Failure $failures "$label metadata.stats.elapsed_ms $elapsed ms exceeds $MaxVectorMs ms"
      }
    }
  }

  return [PSCustomObject]@{
    Total      = $rows.Count
    FailedRows = $failedRows.Count
    Failures   = $failures
  }
}

# ══════════════════════════════════════════════════════════════════════
# 历史索引（让验收记录出现在桌面端「历史任务」面板）
# ══════════════════════════════════════════════════════════════════════

function Read-JsonArrayFile {
  param([string]$Path)
  if (-not (Test-LiteralPathExists $Path)) { return @() }
  try {
    $value = Get-Content -Raw -LiteralPath $Path -Encoding UTF8 | ConvertFrom-Json
    if ($null -eq $value) { return @() }
    if ($value -is [array]) { return @($value) }
    return @($value)
  } catch {
    return @()
  }
}

function Write-JsonArrayFile {
  param([string]$Path, [array]$Items)
  New-Item -ItemType Directory -Path (Split-Path -Parent $Path) -Force | Out-Null
  $jsonItems = @($Items | ForEach-Object { $_ | ConvertTo-Json -Depth 12 })
  $json = if ($jsonItems.Count -eq 0) { "[]" } else { "[`n$($jsonItems -join ",`n")`n]" }
  [System.IO.File]::WriteAllText($Path, $json, [System.Text.UTF8Encoding]::new($false))
}

function Get-HistoryId {
  param([string]$Source)
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $hashBytes = $sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Source))
    return [BitConverter]::ToString($hashBytes, 0, 6).Replace("-", "")
  } finally {
    $sha.Dispose()
  }
}

function New-SingleHistoryEntry {
  <#
    为单图场景构造一条桌面端可恢复的历史条目。

    mode='single' 时，桌面端按 paths.original/transparent/preview/svg/metadata
    直接读盘展示，所以 paths 必须指向真实产物，而不是报告文件。
  #>
  param($Data, [string]$Root)

  $ok = ($Data.Status -eq 'success')

  return [PSCustomObject]@{
    id          = Get-HistoryId "$($Data.TaskDir)|single"
    mode        = 'single'
    title       = $Data.Title
    time        = (Get-Date).ToString('o')
    status      = if ($ok) { 'completed' } else { 'failed' }
    taskDir     = $Data.TaskDir
    outputRoot  = $Root
    paths       = [PSCustomObject]$Data.Paths
    inputParams = [PSCustomObject]@{
      mode       = 'single'
      text       = $Data.Text
      prompt     = $Data.Prompt
      negative   = $Data.Negative
      resolution = $Data.Resolution
      format     = 'PNG'
      seed       = $Data.Seed
      vector     = @{ preset = $VectorPreset }
    }
    itemsCount  = 1
    succeeded   = if ($ok) { 1 } else { 0 }
    failed      = if ($ok) { 0 } else { 1 }
    thumb       = ''
    batchItems  = @()
  }
}

function New-BatchHistoryEntry {
  <#
    为批量场景构造一条历史条目。

    每条产物放进 batchItems，桌面端点开后可逐条切换查看——与普通批量任务一致。
  #>
  param($Data, [string]$Root)

  $items = @($Data.Items)
  $succeeded = @($items | Where-Object { $_.status -eq 'success' }).Count
  $failed = @($items | Where-Object { $_.status -ne 'success' }).Count
  $status = if ($failed -gt 0) {
    if ($succeeded -gt 0) { "partial ($succeeded/$($items.Count))" } else { 'failed' }
  } else { 'completed' }

  return [PSCustomObject]@{
    id          = Get-HistoryId "$($Data.TaskDir)|batch"
    mode        = 'batch'
    title       = $Data.Title
    time        = (Get-Date).ToString('o')
    status      = $status
    taskDir     = $Data.TaskDir
    outputRoot  = $Root
    paths       = [PSCustomObject]@{ summaryDir = $Data.TaskDir }
    inputParams = [PSCustomObject]@{
      mode       = 'batch'
      batch      = $Data.BatchInput
      negative   = $Data.Negative
      resolution = $Data.Resolution
      format     = 'PNG'
      seed       = $Data.Seed
      vector     = @{ preset = $VectorPreset }
    }
    itemsCount  = $items.Count
    succeeded   = $succeeded
    failed      = $failed
    thumb       = ''
    batchItems  = $items
  }
}

function Save-HistoryEntries {
  <#
    把若干条目合并进 outputs\tasks-index.json。

    按 id 或 taskDir 覆盖同一条记录；同时清掉旧版本写入的 mode='acceptance'
    聚合条目——那些条目的 paths 指向报告文件，点开恢复不出任何图形。
  #>
  param([string]$Root, [array]$Entries)

  if ($Entries.Count -eq 0) { return }

  $rootIndexPath = Join-Path $Root 'tasks-index.json'
  $rootEntries = @(Read-JsonArrayFile $rootIndexPath | Where-Object { [string]$_.mode -ne 'acceptance' })

  foreach ($entry in $Entries) {
    $found = -1
    for ($i = 0; $i -lt $rootEntries.Count; $i++) {
      if ([string]$rootEntries[$i].id -eq [string]$entry.id -or
          [string]$rootEntries[$i].taskDir -eq [string]$entry.taskDir) {
        $found = $i
        break
      }
    }
    if ($found -ge 0) { $rootEntries[$found] = $entry } else { $rootEntries += $entry }
  }

  if ($rootEntries.Count -gt 200) { $rootEntries = @($rootEntries | Select-Object -Last 200) }

  Write-JsonArrayFile -Path $rootIndexPath -Items $rootEntries
  Write-Verbose "history index: $rootIndexPath（本次 $($Entries.Count) 条）"
}

function Get-SummaryHistoryEntry {
  param([string]$SummaryPath, [string]$OutputRoot, [string]$RunDir, [array]$Rows)

  $taskDir = Split-Path -Parent $SummaryPath
  $failedRows = @($Rows | Where-Object { $_.status -eq "failed" -or -not [string]::IsNullOrWhiteSpace($_.error) })
  $succeeded = [Math]::Max(0, $Rows.Count - $failedRows.Count)
  $time = (Get-Item -LiteralPath $SummaryPath).LastWriteTime.ToString("o")

  return [PSCustomObject]@{
    id          = Get-HistoryId "$SummaryPath|$time"
    mode        = "batch"
    title       = "Acceptance batch ($($Rows.Count) items)"
    time        = $time
    status      = if ($failedRows.Count -gt 0) { if ($succeeded -gt 0) { "partial ($succeeded/$($Rows.Count))" } else { "failed" } } else { "completed" }
    taskDir     = $taskDir
    outputRoot  = $OutputRoot
    paths       = [PSCustomObject]@{ summary = $SummaryPath; summaryDir = $taskDir }
    inputParams = [PSCustomObject]@{ mode = "batch"; batch = "acceptance"; resolution = $Resolution; seed = $Seed; seedStep = $SeedStep }
    itemsCount  = $Rows.Count
    succeeded   = $succeeded
    failed      = $failedRows.Count
    thumb       = ""
    runDir      = $RunDir
    batchItems  = @(Get-BatchHistoryItems -Rows $Rows -SummaryPath $SummaryPath -OutputRoot $OutputRoot)
  }
}

function Get-BatchHistoryItems {
  param([array]$Rows, [string]$SummaryPath, [string]$OutputRoot)

  $items = @()
  for ($i = 0; $i -lt $Rows.Count; $i++) {
    $row = $Rows[$i]
    $taskDir = Resolve-ArtifactPath $row.task_dir $SummaryPath
    $logPath = Resolve-ArtifactPath $row.run_log_path $SummaryPath
    $items += [PSCustomObject]@{
      index           = $i
      text            = $row.text
      prompt          = $row.prompt
      status          = if ($row.status -eq "failed" -or -not [string]::IsNullOrWhiteSpace($row.error)) { "failed" } else { "success" }
      rawStatus       = $row.status
      error           = $row.error
      seed            = $row.seed
      generationMs    = $row.generation_ms
      vectorMs        = $row.vector_ms
      pngTransparency = $row.png_transparency
      svgFidelity     = $row.svg_fidelity
      taskDir         = $taskDir
      outputRoot      = $OutputRoot
      taskName        = if ($row.task_name) { $row.task_name } else { Split-Path -Leaf $taskDir }
      paths           = [PSCustomObject]@{
        original    = if ($row.original_path) { Resolve-ArtifactPath $row.original_path $SummaryPath } else { Join-Path $taskDir "original.png" }
        transparent = if ($row.transparent_path) { Resolve-ArtifactPath $row.transparent_path $SummaryPath } else { Join-Path $taskDir "transparent.png" }
        svg         = if ($row.result_svg_path) { Resolve-ArtifactPath $row.result_svg_path $SummaryPath } else { Join-Path $taskDir "result.svg" }
        preview     = if ($row.preview_path) { Resolve-ArtifactPath $row.preview_path $SummaryPath } else { Join-Path $taskDir "preview.png" }
        metadata    = if ($row.metadata_path) { Resolve-ArtifactPath $row.metadata_path $SummaryPath } else { Join-Path $taskDir "metadata.json" }
        log         = $logPath
      }
    }
  }
  return $items
}

function Sync-AcceptanceHistoryIndex {
  param([string]$OutputRoot_, [string]$SummaryPath)

  $rootIndexPath = Join-Path $OutputRoot_ "tasks-index.json"
  $rows = @(Import-Csv -LiteralPath $SummaryPath -Encoding UTF8)
  $summaryDir = Split-Path -Parent $SummaryPath
  $rootEntries = @(Read-JsonArrayFile $rootIndexPath)

  $existingIndex = -1
  for ($i = 0; $i -lt $rootEntries.Count; $i++) {
    if ([string]$rootEntries[$i].taskDir -eq [string]$summaryDir -or [string]$rootEntries[$i].paths.summary -eq [string]$SummaryPath) {
      $existingIndex = $i
      break
    }
  }

  $entry = if ($existingIndex -ge 0) {
    $rootEntries[$existingIndex]
  } else {
    Get-SummaryHistoryEntry -SummaryPath $SummaryPath -OutputRoot $OutputRoot_ -RunDir $summaryDir -Rows $rows
  }

  $failedRows = @($rows | Where-Object { $_.status -eq "failed" -or -not [string]::IsNullOrWhiteSpace($_.error) })
  $succeeded = [Math]::Max(0, $rows.Count - $failedRows.Count)
  $entry | Add-Member -NotePropertyName outputRoot -NotePropertyValue $OutputRoot_ -Force
  $entry | Add-Member -NotePropertyName mode -NotePropertyValue "batch" -Force
  $entry | Add-Member -NotePropertyName taskDir -NotePropertyValue $summaryDir -Force
  $entry | Add-Member -NotePropertyName batchItems -NotePropertyValue @(Get-BatchHistoryItems -Rows $rows -SummaryPath $SummaryPath -OutputRoot $OutputRoot_) -Force
  $entry | Add-Member -NotePropertyName itemsCount -NotePropertyValue $rows.Count -Force
  $entry | Add-Member -NotePropertyName succeeded -NotePropertyValue $succeeded -Force
  $entry | Add-Member -NotePropertyName failed -NotePropertyValue $failedRows.Count -Force
  $entry | Add-Member -NotePropertyName title -NotePropertyValue "Acceptance batch ($($rows.Count) items)" -Force
  $entry | Add-Member -NotePropertyName status -NotePropertyValue $(if ($failedRows.Count -gt 0) { if ($succeeded -gt 0) { "partial ($succeeded/$($rows.Count))" } else { "failed" } } else { "completed" }) -Force
  if (-not $entry.paths) {
    $entry | Add-Member -NotePropertyName paths -NotePropertyValue ([PSCustomObject]@{}) -Force
  }
  $entry.paths | Add-Member -NotePropertyName summary -NotePropertyValue $SummaryPath -Force
  $entry.paths | Add-Member -NotePropertyName summaryDir -NotePropertyValue $summaryDir -Force
  if (-not $entry.inputParams) {
    $entry | Add-Member -NotePropertyName inputParams -NotePropertyValue ([PSCustomObject]@{ mode = "batch" }) -Force
  }

  if ($existingIndex -ge 0) {
    $rootEntries[$existingIndex] = $entry
  } else {
    $rootEntries += $entry
  }

  if ($rootEntries.Count -gt 200) {
    $rootEntries = @($rootEntries | Select-Object -Last 200)
  }
  Write-JsonArrayFile -Path $rootIndexPath -Items $rootEntries
  Write-Verbose "history index: $rootIndexPath"
}

# ══════════════════════════════════════════════════════════════════════
# 300 条套件的编排
# ══════════════════════════════════════════════════════════════════════

function Get-AcceptanceSuiteConfig {
  $fixture = Join-Path $ScriptDir "acceptance.txt"
  if (-not (Test-Path -LiteralPath $fixture)) {
    $fixture = Join-Path $ScriptDir "fixtures\acceptance.txt"
  }
  return @{
    Label          = "acceptance"
    Fixture        = $fixture
    ExpectedRows   = 0
    MinRows        = 100
    Seed           = 2026061201
    TimeoutMinutes = 270
    OutputRoot     = "outputs"
  }
}

function Invoke-FullSuite {
  <# 跑 300 条 CLI 批量套件并核验产物；返回 PSCustomObject(场景结果)。 #>
  param($Ctx, [string]$OutputRoot_)

  $result = New-ScenarioResult -Name "300 条完整套件（$($Ctx.SuiteFixture)）"
  $watch = [System.Diagnostics.Stopwatch]::StartNew()
  $config = $Ctx.SuiteConfig

  $runStamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $workDir = Join-Path ([System.IO.Path]::GetTempPath()) "gen2vec-acceptance\acceptance-$runStamp"
  $preparedFixture = Join-Path $workDir "acceptance.txt"

  try {
    $expectedRows = @(New-PreparedFixture -FixturePath $config.Fixture -PreparedPath $preparedFixture -Config $config)
  } catch {
    Add-Check $result $false '准备 300 条输入' $_.Exception.Message -DisplayText '无法准备测试输入'
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }
  Write-Host "  正在批量生成 $($expectedRows.Count) 条，请耐心等待…" -ForegroundColor Cyan
  Write-Verbose "  prepared input: $preparedFixture"
  Write-Verbose "  rows: $($expectedRows.Count)"

  try {
    $cliExe = Resolve-CliExecutable $CliPath
  } catch {
    Add-Check $result $false '定位 gen2vec_cli.exe' $_.Exception.Message -DisplayText '找不到批量生成程序'
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  $batchSeed = if ($Seed -eq 0) { $config.Seed } else { $Seed }
  $batchArgs = @(
    "batch",
    "--input-file", $preparedFixture,
    "--output-dir", $OutputRoot_,
    "--seed", [string]$batchSeed,
    "--seed-step", [string]$SeedStep,
    "--resolution", $Resolution,
    "--vector-preset", $VectorPreset,
    "--negative", $Negative,
    "--wait", [string]$Wait
  )
  if ($NoVectorize) { $batchArgs += "--no-vectorize" }

  Write-Verbose "  command: $(Format-CommandLine $cliExe $batchArgs)"
  if ($DryRun) {
    Add-Check $result $true 'dry-run：不实际调用 CLI'
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  & $cliExe @batchArgs
  if ($LASTEXITCODE -ne 0) {
    Add-Check $result $false 'CLI batch 退出码' "exit=$LASTEXITCODE" -DisplayText '批量生成异常退出'
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  try {
    $summaryPath = Get-LatestSummary $OutputRoot_
  } catch {
    Add-Check $result $false '定位 batch_summary.csv' $_.Exception.Message -DisplayText '找不到批量结果文件'
    $result.Elapsed = $watch.Elapsed.TotalSeconds
    return $result
  }

  $report = Test-AcceptanceArtifacts -SummaryPath $summaryPath -ExpectedRows $expectedRows -Config $config
  Add-Check $result ($report.Failures.Count -eq 0) '完整套件产物核验' `
    "总 $($report.Total) 条，失败行 $($report.FailedRows)，问题 $($report.Failures.Count) 条" `
    -DisplayText '完整套件有产物缺失'
  $shown = if (Test-DetailedOutput) { $report.Failures } else { $report.Failures | Select-Object -First 5 }
  foreach ($failure in $shown) { Write-Host "  - $failure" -ForegroundColor Red }
  if (-not (Test-DetailedOutput) -and $report.Failures.Count -gt 5) {
    Write-Host "  …其余 $($report.Failures.Count - 5) 项略。" -ForegroundColor DarkGray
  }
  [void]$result.Artifacts.Add($summaryPath)

  Sync-AcceptanceHistoryIndex -OutputRoot_ $OutputRoot_ -SummaryPath $summaryPath

  $result.Elapsed = $watch.Elapsed.TotalSeconds
  return $result
}

# ══════════════════════════════════════════════════════════════════════
# 报告
# ══════════════════════════════════════════════════════════════════════

function Write-AcceptanceReport {
  param(
    [string]$ReportDir_,
    [array]$Results,
    [array]$Drift,
    [double]$TotalSeconds,
    [string]$Status
  )

  New-Item -ItemType Directory -Path $ReportDir_ -Force | Out-Null

  $report = [PSCustomObject]@{
    script         = 'run-acceptance.ps1'
    generated_at   = (Get-Date).ToString("o")
    status         = $Status
    elapsed_seconds = [Math]::Round($TotalSeconds, 1)
    powershell     = $PSVersionTable.PSVersion.ToString()
    contract_drift = @($Drift)
    scenarios      = @($Results | ForEach-Object {
        [PSCustomObject]@{
          name      = $_.Name
          status    = $_.Status
          elapsed   = [Math]::Round($_.Elapsed, 1)
          artifacts = @($_.Artifacts)
          checks    = @($_.Checks | ForEach-Object { [PSCustomObject]@{ label = $_.Label; status = $_.Status; detail = $_.Detail } })
        }
      })
  }

  $jsonPath = Join-Path $ReportDir_ 'report.json'
  [System.IO.File]::WriteAllText($jsonPath, ($report | ConvertTo-Json -Depth 12), (New-Object System.Text.UTF8Encoding($false)))

  $lines = New-Object System.Collections.ArrayList
  [void]$lines.Add('验收测试报告')
  [void]$lines.Add("生成时间: $($report.generated_at)")
  [void]$lines.Add("结论:     $Status")
  [void]$lines.Add("累计耗时: $([Math]::Round($TotalSeconds, 1))s")
  [void]$lines.Add('')
  foreach ($driftItem in @($Drift)) { [void]$lines.Add("WARN 常量漂移：$driftItem") }
  if (@($Drift).Count -gt 0) { [void]$lines.Add('') }

  foreach ($r in $Results) {
    [void]$lines.Add("=== $($r.Name) ===")
    [void]$lines.Add("状态: $($r.Status)  耗时: $([Math]::Round($r.Elapsed, 1))s")
    foreach ($c in $r.Checks) {
      $suffix = if ([string]::IsNullOrWhiteSpace($c.Detail)) { '' } else { "  ($($c.Detail))" }
      [void]$lines.Add("  [$($c.Status)] $($c.Label)$suffix")
    }
    [void]$lines.Add('')
  }

  # report.txt 是给人双击打开看的，带 BOM 以免中文 Windows 记事本猜成 GBK 显示乱码。
  # report.json / tasks-index.json 保持无 BOM，与 CLI 既有写法一致（桌面端按 UTF-8 读）。
  $txtPath = Join-Path $ReportDir_ 'report.txt'
  [System.IO.File]::WriteAllLines($txtPath, $lines, (New-Object System.Text.UTF8Encoding($true)))

  return [PSCustomObject]@{ Json = $jsonPath; Text = $txtPath }
}

# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

function Invoke-AcceptanceMain {
  param(
    [string]$OutputRoot_,
    [string]$ReportDir_,
    [string]$Txt2ImgBase_,
    [string]$VectorizerBase_,
    [string]$ComfyUiHost_
  )

  Write-Host ''
  Write-Host '============================================================' -ForegroundColor Cyan
  Write-Host ' 验收测试' -ForegroundColor Cyan
  Write-Host '============================================================' -ForegroundColor Cyan

  # ── 契约漂移守卫（只 WARN，不判失败）──
  $drift = @()
  try {
    $drift = @(Test-ContractDrift -GeneratorPath (Join-Path $RepoRoot 'services\txt2img-api\app\generator.py') `
        -RouteChains (Get-ExpectedRouteChains) `
        -QwenResolutions (Get-ExpectedQwenResolutions) `
        -FluxResolutions (Get-ExpectedFluxResolutions))
  } catch {
    $drift = @("跳过常量漂移检查：$($_.Exception.Message)")
  }
  # 常量漂移属于内部校验，默认不打扰用户（仍完整写入报告）
  if (Test-DetailedOutput) {
    foreach ($item in $drift) { Write-Host "  [警告] $item" -ForegroundColor Yellow }
  }

  # ── VerifyOnly：只核验已存在的汇总表 ──
  if (-not [string]::IsNullOrWhiteSpace($VerifyOnly)) {
    $summaryPath = Resolve-AbsolutePath $VerifyOnly
    if (-not (Test-LiteralPathExists $summaryPath)) { throw "batch_summary.csv does not exist: $summaryPath" }
    $config = Get-AcceptanceSuiteConfig
    $rows = @(Import-Csv -LiteralPath $summaryPath -Encoding UTF8)
    $report = Test-AcceptanceArtifacts -SummaryPath $summaryPath -ExpectedRows $rows -Config $config
    if ($report.Failures.Count -eq 0) {
      Write-Outcome 通过 "汇总表核验通过（$($report.Total) 条）"
      return 0
    }

    Write-Outcome 失败 "汇总表核验未通过（$($report.Failures.Count) 个问题）" `
      "总 $($report.Total) 条，失败行 $($report.FailedRows)"
    $shown = if (Test-DetailedOutput) { $report.Failures } else { $report.Failures | Select-Object -First 5 }
    $shown | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    if (-not (Test-DetailedOutput) -and $report.Failures.Count -gt 5) {
      Write-Host "  …其余 $($report.Failures.Count - 5) 项略。" -ForegroundColor DarkGray
    }
    return 1
  }

  # ── 环境门禁 ──
  # dry-run 只做一次快速探测并如实报告，不会为了等后端而空转，也不因未就绪而退出。
  if (-not $SkipHealth) {
    Write-Host ''
    Write-Host '检查运行环境…' -ForegroundColor Cyan
    if ($DryRun) {
      # dry-run 只做一次快速探测，不为等后端而空转
      $txtOk = Test-HttpReady -Url "$Txt2ImgBase_/healthz" -TimeoutSec 3
      $vecOk = Test-HttpReady -Url "$VectorizerBase_/healthz" -TimeoutSec 3
      $comfyOk = Test-HttpReady -Url "$ComfyUiHost_/system_stats" -TimeoutSec 5
      Write-Outcome $(if ($txtOk) { '通过' } else { '失败' }) $(if ($txtOk) { '图像生成服务已就绪' } else { '图像生成服务未启动' })
      Write-Outcome $(if ($vecOk) { '通过' } else { '失败' }) $(if ($vecOk) { '矢量化服务已就绪' } else { '矢量化服务未启动' })
      Write-Outcome $(if ($comfyOk) { '通过' } else { '警告' }) $(if ($comfyOk) { '绘图引擎已就绪' } else { '绘图引擎尚未启动' })
    } else {
      $txtOk = Wait-BackendReady -Base $Txt2ImgBase_ -Name 'txt2img-api' -Seconds $Wait
      if ($txtOk) { Write-Outcome 通过 '图像生成服务已就绪' } else { Write-Outcome 失败 '图像生成服务未启动' }

      $vecOk = Wait-BackendReady -Base $VectorizerBase_ -Name 'vectorizer-api' -Seconds $Wait
      if ($vecOk) { Write-Outcome 通过 '矢量化服务已就绪' } else { Write-Outcome 失败 '矢量化服务未启动' }

      # 绘图引擎由图像生成服务按需拉起，这里只报告状态，不阻塞
      $comfyOk = Test-HttpReady -Url "$ComfyUiHost_/system_stats" -TimeoutSec 5
      if ($comfyOk) { Write-Outcome 通过 '绘图引擎已就绪' }
      else { Write-Host '  [提示] 绘图引擎将在生成时启动' -ForegroundColor DarkGray }

      if (-not ($txtOk -and $vecOk)) {
        Write-Host ''
        Write-Host '  请打开桌面端（会自动启动所需服务）后重试。' -ForegroundColor Red
        if (Test-DetailedOutput) {
          Write-Host "  txt2img-api     ->  $Txt2ImgBase_" -ForegroundColor Red
          Write-Host "  vectorizer-api  ->  $VectorizerBase_" -ForegroundColor Red
        }
        return 2
      }
    }
  }

  if ($DryRun) {
    Write-Host ''
    Write-Host '将依次执行：' -ForegroundColor Cyan
    $planStep = 0
    $planStep++
    Write-Host "  $planStep. 单图生成（$script:SingleText）"
    if (-not $SkipBatch) {
      $planStep++
      Write-Host "  $planStep. 批量生成（3 条）"
    }
    if ($Full) {
      $planStep++
      Write-Host "  $planStep. 完整批量套件（安装包内置测试集）"
    }
    Write-Host '结果将保存到：'
    Write-Host "  $ReportDir_"

    if (Test-DetailedOutput) {
      Write-Host ''
      Write-Host '  [verbose] 请求地址：'
      Write-Host "    $Txt2ImgBase_/api/v1/txt2img"
      Write-Host "    $VectorizerBase_/api/v1/vectorize"
      if ($Full) { Write-Host '    gen2vec_cli.exe batch --input-file <fixtures\acceptance.txt>' }
    }

    Write-Host ''
    Write-Host '[dry-run] 未发出任何请求，也未核验产物。' -ForegroundColor Yellow
    if (-not $SkipHealth -and -not ($txtOk -and $vecOk)) {
      Write-Host '[dry-run] 注意：服务当前未就绪，实跑前需先打开桌面端。' -ForegroundColor Yellow
    }
    return 0
  }

  # ── 跑场景 ──
  $ctx = [PSCustomObject]@{
    Txt2ImgBase     = $Txt2ImgBase_
    VectorizerBase  = $VectorizerBase_
    ComfyUiHost     = $ComfyUiHost_
    ReportDir       = $ReportDir_
    OutputRoot      = $OutputRoot_
    ComfyUiWaitSec  = $ComfyUiWaitSec
    GenTimeoutSec   = $GenTimeoutSec
    VectorTimeoutSec = $VectorTimeoutSec
    VectorPreset    = $VectorPreset
    MaxE2eSeconds   = 0.0
    SuiteConfig     = (Get-AcceptanceSuiteConfig)
    SuiteFixture    = ''
  }
  $ctx.SuiteFixture = $ctx.SuiteConfig.Fixture

  $results = New-Object System.Collections.ArrayList
  $started = [System.Diagnostics.Stopwatch]::StartNew()

  $totalScenarios = if ($SkipBatch) { 1 } else { 2 }
  if ($Full) { $totalScenarios += 1 }

  $step = 0
  $batch = $null

  $step++
  Write-Stage "[$step/$totalScenarios] 单图生成：$script:SingleText"
  $single = Invoke-SingleScenario -Ctx $ctx
  [void]$results.Add($single)
  if ($single.Status -eq 'FAIL') {
    Write-Outcome 失败 "单图未完成（$(Format-Duration $single.Elapsed)）"
  } elseif ($single.Status -eq 'WARN') {
    Write-Outcome 警告 "单图完成，但有提示（$(Format-Duration $single.Elapsed)）"
  } else {
    Write-Outcome 通过 "单图完成（$(Format-Duration $single.Elapsed)）"
  }

  if ($SkipBatch) {
    Write-Stage '批量生成 —— 已跳过（-SkipBatch）'
  } else {
    $step++
    Write-Stage "[$step/$totalScenarios] 批量生成：3 条"
    $batch = Invoke-BatchScenario -Ctx $ctx
    [void]$results.Add($batch)

    $batchItems = @(if ($batch.Data -and $batch.Data.Items) { $batch.Data.Items } else { @() })
    $batchSucceeded = @($batchItems | Where-Object { $_.status -eq 'success' }).Count
    $batchTotal = if ($batchItems.Count -gt 0) { $batchItems.Count } else { 3 }
    $batchDetail = "$batchSucceeded/$batchTotal 条成功，$(Format-Duration $batch.Elapsed)"
    if ($batch.Status -eq 'FAIL') {
      Write-Outcome 失败 "批量未完成（$batchDetail）"
    } elseif ($batch.Status -eq 'WARN') {
      Write-Outcome 警告 "批量完成，但有提示（$batchDetail）"
    } else {
      Write-Outcome 通过 "批量完成（$batchDetail）"
    }
  }

  if ($Full) {
    $step++
    Write-Stage "[$step/$totalScenarios] 完整批量套件"
    $full = Invoke-FullSuite -Ctx $ctx -OutputRoot_ $OutputRoot_
    [void]$results.Add($full)
    if ($full.Status -eq 'FAIL') {
      Write-Outcome 失败 "完整套件未通过（$(Format-Duration $full.Elapsed)）"
    } else {
      Write-Outcome 通过 "完整套件通过（$(Format-Duration $full.Elapsed)）"
    }
  }

  # ── 汇总 ──
  $totalSeconds = $started.Elapsed.TotalSeconds
  $failed = @($results | Where-Object { $_.Status -eq 'FAIL' })
  $warned = @($results | Where-Object { $_.Status -eq 'WARN' })
  $status = if ($failed.Count -gt 0) { 'FAIL' } elseif ($warned.Count -gt 0 -or $drift.Count -gt 0) { 'WARN' } else { 'PASS' }

  $summaryTitle = if ($failed.Count -gt 0) { ' 验收结果：未通过' }
  elseif ($warned.Count -gt 0 -or $drift.Count -gt 0) { ' 验收结果：通过（有提示）' }
  else { ' 验收结果：通过' }

  Write-Host ''
  Write-Host '============================================================' -ForegroundColor Cyan
  Write-Host $summaryTitle -ForegroundColor $(if ($failed.Count -gt 0) { 'Red' } else { 'Green' })
  Write-Host '============================================================' -ForegroundColor Cyan

  foreach ($r in $results) {
    $displayName = switch -Regex ($r.Name) {
      '^单图' { '单图生成' }
      '^批量' { '批量生成' }
      '套件' { '完整套件' }
      default { $r.Name }
    }
    $displayStatus = if ($r.Status -eq 'FAIL') { '未通过' } elseif ($r.Status -eq 'WARN') { '通过（有提示）' } else { '通过' }
    $countSuffix = ''
    if ($r.Name -match '^批量' -and $r.Data -and $r.Data.Items) {
      $okCount = @($r.Data.Items | Where-Object { $_.status -eq 'success' }).Count
      $countSuffix = "（$okCount/$(@($r.Data.Items).Count)）"
    }
    Write-Host "  $displayName：$displayStatus$countSuffix"
  }
  Write-Host "  总用时：$(Format-Duration $totalSeconds)"
  if (Test-DetailedOutput) {
    foreach ($item in $drift) { Write-Host "  [警告] $item" -ForegroundColor Yellow }
  }

  # ── 报告 + 历史索引 ──
  $report = Write-AcceptanceReport -ReportDir_ $ReportDir_ -Results $results -Drift $drift -TotalSeconds $totalSeconds -Status $status
  Write-Host ''
  Write-Host '  结果已保存到：'
  Write-Host "  $ReportDir_"
  if (Test-DetailedOutput) {
    Write-Host "  report: $($report.Text)"
    Write-Host "  report: $($report.Json)"
  } else {
    Write-Host '  可在桌面端「历史任务」中查看。'
  }

  # 每个场景单独登记一条，桌面端「历史任务」点开后按 paths 读盘展示真实产物。
  # 不再写聚合条目：那种条目只能指向报告文件，点开恢复不出任何图形。
  $historyEntries = @()
  if ($single.Data -and $single.Data.TaskDir -and $single.Data.Paths) {
    $historyEntries += New-SingleHistoryEntry -Data $single.Data -Root $OutputRoot_
  }
  if ($batch -and $batch.Data -and $batch.Data.TaskDir -and @($batch.Data.Items).Count -gt 0) {
    $historyEntries += New-BatchHistoryEntry -Data $batch.Data -Root $OutputRoot_
  }
  if ($historyEntries.Count -gt 0) {
    Save-HistoryEntries -Root $OutputRoot_ -Entries $historyEntries
  }

  Write-Host ''
  if ($failed.Count -gt 0) {
    Write-Host '  有场景未通过，详细原因见上方提示与报告文件。' -ForegroundColor Red
    return 1
  }
  Write-Host '  验收通过。' -ForegroundColor Green
  return 0
}

# 被测试脚本 dot-source 时只加载上面的函数，不触发主流程。
if ($MyInvocation.InvocationName -eq '.') { return }

# ── 入口：解析路径后交给主流程 ──
$effectiveOutputRoot = if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
  Join-Path $RepoRoot 'outputs'
} else {
  Resolve-AbsolutePath $OutputRoot
}

$effectiveReportDir = if ([string]::IsNullOrWhiteSpace($ReportDir)) {
  Join-Path $effectiveOutputRoot ("acceptance_{0}" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))
} else {
  Resolve-AbsolutePath $ReportDir
}

try {
  $exitCode = Invoke-AcceptanceMain -OutputRoot_ $effectiveOutputRoot -ReportDir_ $effectiveReportDir `
    -Txt2ImgBase_ $Txt2ImgBase -VectorizerBase_ $VectorizerBase -ComfyUiHost_ $ComfyUiHost
} catch {
  Write-Host ''
  Write-Host "验收脚本异常退出：$($_.Exception.Message)" -ForegroundColor Red
  Write-Verbose $_.ScriptStackTrace
  $exitCode = 1
}

exit $exitCode
