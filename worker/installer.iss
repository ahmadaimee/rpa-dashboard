; RPA-Bot Setup — Inno Setup script
; Per-user install (no admin needed), Start Menu entry, proper uninstaller.
; Build: ISCC.exe installer.iss   (after PyInstaller has produced dist\RPA-Bot.exe)

#define AppVersion GetEnv("RPABOT_VERSION")
#if AppVersion == ""
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{7E9B2C41-52D3-4C1B-9E63-RPABOT000001}
AppName=RPA-Bot
AppVersion={#AppVersion}
AppPublisher=Orchard Medical Management
DefaultDirName={localappdata}\Programs\RPA-Bot
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=RPA-Bot-Setup
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\RPA-Bot.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Files]
Source: "dist\RPA-Bot.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\RPA-Bot"; Filename: "{app}\RPA-Bot.exe"; IconFilename: "{app}\RPA-Bot.exe"

[Run]
; Launch after install — shows the pairing / start menu in a console window
Filename: "{app}\RPA-Bot.exe"; Description: "Start RPA-Bot now (pairing on first run)"; Flags: postinstall nowait

[UninstallRun]
; Stop the worker, remove the scheduled task, clear registration — silently
Filename: "{app}\RPA-Bot.exe"; Parameters: "--uninstall --quiet"; Flags: runhidden waituntilterminated; RunOnceId: "RPABotCleanup"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\RPA-Bot"
