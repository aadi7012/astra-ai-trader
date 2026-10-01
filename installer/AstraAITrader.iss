#define AppName "Astra AI Trader"
#define AppVersion "1.1.0"
#define AppPublisher "Astra AI Trader"
#define AppExeName "AstraAITrader.exe"
#ifndef AppBinaryDir
  #define AppBinaryDir "..\dist"
#endif

[Setup]
AppId={{2B5BAEDC-6418-4502-A954-18D1C30A3F11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\AstraAITrader
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release
OutputBaseFilename=AstraAITrader-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExeName}
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "{#AppBinaryDir}\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "Setup-AI.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{autoprograms}\{#AppName}\Install or repair AI model"; Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Setup-AI.ps1"""; WorkingDir: "{app}"

[Run]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\Setup-AI.ps1"""; Description: "Install Ollama and download the qwen2.5:3b AI model (first-time setup)"; Flags: postinstall shellexec waituntilterminated runasoriginaluser
Filename: "{app}\{#AppExeName}"; Description: "Launch Astra AI Trader"; Flags: postinstall nowait skipifsilent
