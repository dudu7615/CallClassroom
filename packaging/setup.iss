; CallClassroom 安装脚本（Inno Setup 6+）。
;
; 编译（Windows 上）：
;     ISCC.exe /DAppVersion=0.1.0 packaging\setup.iss
; 产物在 dist\installer\CallClassroom-Setup-<版本>.exe
;
; 安装范围只针对当前用户（PrivilegesRequired=lowest），全程不弹 UAC，装到
; %LOCALAPPDATA% 下。代价是**装不了防火墙规则**（netsh 要管理员），所以首次
; 运行时 Windows 会弹一次"是否允许访问网络"，教室里那台机器要有人点一次
; "允许"——不点的话局域网里其他设备连不上，服务本身却看起来正常。
;
; 注意：本文件里所有相对路径都是相对**本文件所在目录**（packaging/）解析的，
; 与 ISCC 的当前工作目录无关。所以往仓库根/上层走要用 "..\"。

#define AppName "CallClassroom"
#define AppExeName "CallClassroom.exe"

#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

; spec 生成的可运行目录，相对本文件所在目录
#define BuildDir "..\dist\CallClassroom"

[Setup]
; AppId 唯一标识本软件。**一旦发布就不要再改**，否则新版本会被当成另一个
; 程序、覆盖安装与卸载都会出问题。
AppId={{38BE87DD-002B-44F7-BEBC-979CBF2D3236}}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
; 单页安装：功能区不必单独建组
DisableProgramGroupPage=yes
; 当前用户安装，不请求管理员权限
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 安装器自己的图标（图标来自仓库根的 icon.png，见 packaging/README 的说明）。
; Inno 只认真正的 .ico，png 不行，所以用打包好的多尺寸 ico。
SetupIconFile=icon.ico
; 快捷方式都继承 exe 的图标，这里只管"添加或删除程序"里显示的那个
UninstallDisplayIcon={app}\{#AppExeName}

; 界面语言：Inno 官方不带简体中文。想要全中文向导，把社区的
; ChineseSimplified.isl 放进 Inno 的 Languages\ 目录，再启用下面这行；
; 否则向导是英文、我们自己的选项文案是中文（能用，只是不整齐）。
; [Languages]
; Name: "chinese"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："; Flags: checkedonce
; 默认勾选：这是装在一台固定教室机器上的常驻服务，本来就该跟着开机起来。
; 不想要的人在这里取消勾选即可（比先装上再去启动项里删掉容易）。
Name: "autostart"; Description: "开机自动启动（登录后后台运行）"; GroupDescription: "附加选项："; Flags: checkedonce

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon
; 开机自启走"启动"文件夹的快捷方式，而不是写 HKCU\...\Run 注册表：用户能直接
; 在"启动"里看到并删掉它，卸载时 Inno 也会自动清理。
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; Tasks: autostart

[Run]
Filename: "{app}\{#AppExeName}"; Description: "立即启动 {#AppName}"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { 覆盖安装时先结束正在跑的旧版本。它是无窗口的后台程序，Inno 的
    Restart Manager 认不出来，不主动杀就会因为 exe 被占用而装不上。
    没在跑时 taskkill 返回非零，忽略即可。 }
  Exec('taskkill.exe', '/f /im {#AppExeName}', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
