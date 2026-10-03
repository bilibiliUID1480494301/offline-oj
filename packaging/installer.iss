; Inno Setup 安装脚本
; ============================================================================
; 设计取向：**按用户安装（per-user）**，默认装到 %LOCALAPPDATA%\Programs。
;
; 为什么不用传统的 Program Files + 管理员权限？
;   * 本程序运行时完全不写程序目录（数据在 %LOCALAPPDATA%\OfflineOJ），
;     因此不需要管理员权限；
;   * 免 UAC 意味着双击安装即可完成，学校机房、受限账号下也能装；
;   * 需要装给全机用户时，用户可以在安装向导里右键"以管理员身份运行"，
;     PrivilegesRequiredOverridesAllowed 已经放开这个口子。
;
; 编译：ISCC.exe packaging\installer.iss
; ============================================================================

#define AppName        "离线 OJ 系统"
#define AppNameEn      "Offline OJ System"
#define AppVersion     "2.0.0"
#define AppPublisher   "Offline OJ Project"
#define AppExeName     "OfflineOJ.exe"
#define AppFolder      "OfflineOJ"
#define AppMutexName   "Local\OfflineOJ.SingleInstance.v2"

[Setup]
AppId={{7C1E4B52-9A3D-4F1E-9E77-2B6E5C4A1001}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppNameEn} 安装程序
DefaultDirName={autopf}\{#AppFolder}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist\installer
OutputBaseFilename=OfflineOJ-{#AppVersion}-setup
SetupIconFile=..\assets\oj_icon.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
DisableWelcomePage=no
; 如果程序正在运行，先提示用户关闭，避免覆盖正在使用的 DLL
AppMutex={#AppMutexName}
CloseApplications=yes
RestartApplications=no
; 最低系统版本。**这里不是保守取值，是硬下限**：Qt 6 / PySide6 官方不支持
; Windows 7 与 8.x（8.1 上连窗口都起不来），而界面就是 Qt 6 写的。
; 想让 8.1 有得跑，得把界面层换回 PySide2 / Qt 5.15，那是另一个量级的改动。
; 详见 README「运行环境」一节。Qt 6 自身的要求是 Windows 10 1809 以上。
MinVersion=10.0

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "purgedata";   Description: "卸载时同时删除题库与提交记录（%LOCALAPPDATA%\OfflineOJ）"; GroupDescription: "卸载选项："; Flags: unchecked

[Files]
Source: "..\dist\OfflineOJ\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\使用说明"; Filename: "{app}\README.md"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只删除安装目录下的残留（用户数据在 %LOCALAPPDATA%，默认保留）
Type: filesandordirs; Name: "{app}\_internal"

[UninstallRun]
; 卸载前确保进程已退出，否则文件可能被占用
Filename: "{cmd}"; Parameters: "/C taskkill /IM {#AppExeName} /F"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if WizardIsTaskSelected('purgedata') then
    begin
      DataDir := ExpandConstant('{localappdata}\{#AppFolder}');
      if DirExists(DataDir) then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
