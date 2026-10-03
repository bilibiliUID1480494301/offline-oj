<#
.SYNOPSIS
    一键构建 Windows 发布产物。

.DESCRIPTION
    依次完成：
      1. 生成多尺寸应用图标（assets/oj_icon.ico）
      2. 检查依赖是否就绪
      3. 用 PyInstaller 打包 dist/OfflineOJ/（onedir）
      4. 可选：用 Inno Setup 生成安装程序

.PARAMETER Installer
    打包完成后继续编译安装程序（需要安装 Inno Setup 6）。

.PARAMETER Portable
    打包完成后生成便携版 ZIP（dist/portable/）。不需要 Inno Setup，
    给"装不了 Inno Setup / 想免安装拷贝即用"的场景用。

.PARAMETER Clean
    先清空 build/ 与 dist/。

.PARAMETER Python
    指定 Python 解释器路径，默认使用 PATH 中的 python。

.EXAMPLE
    pwsh packaging\build.ps1 -Clean -Installer

.EXAMPLE
    pwsh packaging\build.ps1 -Portable
#>
[CmdletBinding()]
param(
    [switch]$Installer,
    [switch]$Portable,
    [switch]$Clean,
    [string]$Python = ""
)

$ErrorActionPreference = "Continue"

# 为什么是 Continue 而不是 Stop
# -----------------------------
# PowerShell 5.1 会把"原生程序往 stderr 写东西"当成一条**错误记录**：这里若写
# `Stop`，脚本会当场中止。而 PyInstaller 的全部 INFO 日志恰好都走 stderr，
# 于是打包这一步必然崩，报的还是 `NativeCommandError` —— 看起来像 PyInstaller
# 失败了，其实它只是正常打印日志。（PowerShell 7.3 起可以用
# `$PSNativeCommandUseErrorActionPreference = $false` 只关掉这一条，5.1 没有。）
#
# 所以用 Continue，并**逐个检查原生命令的 `$LASTEXITCODE`** —— 本脚本对每处
# `& $Python ...` 都已经这么做了；cmdlet 的失败则按需用 `-ErrorAction Stop` 提级。

# 子进程（python / pyinstaller）的输出是 UTF-8，而 Windows PowerShell 5.1 默认按
# 系统 ANSI 代码页（中文 Windows 是 936）解码管道内容，中文一律变成乱码 ——
# 构建日志于是没法看（"依赖 OK" 显示成 "渚濊禆 OK"）。这里把输出编码固定在 UTF-8。
# 没有控制台的环境（某些 CI）设置会失败，忽略即可，不该因为这点事中断构建。
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    Write-Verbose "无法设置控制台输出编码：$_"
}
# 明确要求子进程按 UTF-8 输出，不去猜它的默认行为
$env:PYTHONIOENCODING = "utf-8"

$PackagingDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $PackagingDir
Push-Location $Root
try {
    function Write-Step($message) {
        Write-Host ""
        Write-Host "==> $message" -ForegroundColor Cyan
    }

    # ---- 定位解释器 ----
    if (-not $Python) {
        foreach ($candidate in @(".venv\Scripts\python.exe", "venv\Scripts\python.exe")) {
            if (Test-Path $candidate) { $Python = (Resolve-Path $candidate).Path; break }
        }
    }
    if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
    if (-not $Python) { throw "找不到 Python 解释器，请用 -Python 指定路径。" }
    Write-Step "使用解释器：$Python"

    # ---- 依赖检查 ----
    Write-Step "检查构建依赖"
    & $Python -c "import PySide6, psutil, PyInstaller, PIL; print('依赖 OK')"
    if ($LASTEXITCODE -ne 0) {
        throw "缺少依赖，请先执行：pip install -r requirements-build.txt"
    }

    # ---- 清理 ----
    if ($Clean) {
        Write-Step "清理 build/ 与 dist/"
        Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    }

    # ---- 图标 ----
    Write-Step "生成应用图标"
    & $Python "packaging\make_icon.py"
    if ($LASTEXITCODE -ne 0) { throw "图标生成失败。" }

    # ---- 版本一致性检查 ----
    Write-Step "校验版本号一致性"
    $version = (& $Python -c "import offline_oj; print(offline_oj.__version__)").Trim()
    # offline_oj.__version__ 是三段式（2.0.0），version_info.txt 里是四段式
    # （(2, 0, 0, 0)），所以补零到四段再比。
    # 注意：这里必须先算好字符串再传给 [regex]::Escape。写成
    #   [regex]::Escape("($version".Replace('.', ',') -replace '^\(', '(')
    # 会被解析成**两个参数**（逗号和 -replace 的优先级），直接抛
    # 「找不到 Escape 的重载，参数计数为 2」—— 这一步曾经因此必崩。
    $parts = @($version.Split('.')) + @('0', '0', '0')
    $expected = "(" + (($parts[0..3]) -join ", ") + ")"
    $specText = Get-Content "packaging\version_info.txt" -Raw
    if ($specText -notmatch [regex]::Escape($expected)) {
        Write-Warning "packaging\version_info.txt 与 offline_oj.__version__（$version）不一致：未找到 $expected"
    } else {
        Write-Host "版本号 $version 一致（$expected）。"
    }

    # ---- 打包 ----
    Write-Step "PyInstaller 打包（onedir）"
    & $Python -m PyInstaller "packaging\offline_oj.spec" --noconfirm --distpath dist --workpath build
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }

    $exe = Join-Path $Root "dist\OfflineOJ\OfflineOJ.exe"
    if (-not (Test-Path $exe)) { throw "未找到产物 $exe" }
    $sizeMb = [math]::Round((Get-ChildItem "dist\OfflineOJ" -Recurse |
        Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    Write-Host "产物：$exe（约 $sizeMb MB）" -ForegroundColor Green

    # ---- 便携版 ----
    if ($Portable) {
        Write-Step "生成便携版 ZIP"
        $portableDir = "dist\portable"
        New-Item -ItemType Directory -Force -Path $portableDir | Out-Null
        $zip = Join-Path $portableDir "OfflineOJ-$version-win64-portable.zip"
        if (Test-Path $zip) { Remove-Item $zip -Force }
        # 连同 README 一起打包，解压出来就是"能直接用的一个目录"
        # EAP 是 Continue，这里必须自己确认压缩真的成功了
        Compress-Archive -Path "dist\OfflineOJ\*", "README.md" -DestinationPath $zip -ErrorAction Stop
        if (-not (Test-Path $zip)) { throw "便携版 ZIP 未生成：$zip" }
        $zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
        Write-Host "便携版：$zip（约 $zipMb MB）" -ForegroundColor Green
        Write-Host "  提示：解压即用，数据仍写入 %LOCALAPPDATA%\OfflineOJ；" -ForegroundColor DarkGray
        Write-Host "        想做到「整个目录带走」，启动前把 OFFLINE_OJ_HOME 指向目录内的 data。" -ForegroundColor DarkGray
    }

    # ---- 安装程序 ----
    if ($Installer) {
        Write-Step "编译安装程序（Inno Setup）"
        # 注意 ${env:ProgramFiles(x86)} 必须带花括号：写成 $env:ProgramFiles(x86) 时
        # PowerShell 会把变量名截到 ProgramFiles，"(x86)" 变成普通文本，
        # 结果是 "C:\Program Files(x86)\..."（少一个空格），这一条永远匹配不上。
        $iscc = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            # Inno Setup 6 的安装器提供"仅为当前用户安装"，落在用户目录下
            "${env:LOCALAPPDATA}\Programs\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
        if (-not $iscc) {
            throw "未找到 ISCC.exe，请先安装 Inno Setup 6：https://jrsoftware.org/isdl.php"
        }
        & $iscc "packaging\installer.iss"
        if ($LASTEXITCODE -ne 0) { throw "安装程序编译失败。" }
        Get-ChildItem "dist\installer" | ForEach-Object {
            Write-Host "安装包：$($_.FullName)" -ForegroundColor Green
        }
    }

    Write-Step "构建完成"
}
finally {
    Pop-Location
}
