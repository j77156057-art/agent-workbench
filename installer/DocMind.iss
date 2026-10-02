; Optional Inno Setup installer for the PyInstaller directory build.
#ifndef AppName
  #define AppName "DocMind"
#endif
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif
#ifndef DistDir
  #define DistDir "..\dist\DocMind"
#endif
#ifndef AppIdValue
  #define AppIdValue "{8E3E8D6D-4D5E-4C85-9E9A-DOCMIND2026}"
#endif
#ifndef InstallerName
  #define InstallerName "DocMind-Setup"
#endif
#ifndef InstallerOutput
  #define InstallerOutput "..\dist\installer"
#endif

[Setup]
AppId={#AppIdValue}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputDir={#InstallerOutput}
OutputBaseFilename={#InstallerName}
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
Uninstallable=yes
CloseApplications=force
RestartApplications=no
CloseApplicationsFilter=DocMind.exe

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\DocMind.exe"
Name: "{group}\{#AppName}"; Filename: "{app}\DocMind.exe"
Name: "{group}\卸载 {#AppName}"; Filename: "{uninstallexe}"

[UninstallDelete]
Type: files; Name: "{app}\uninstall.bat"
Type: files; Name: "{app}\uninstall_portable.ps1"
Type: files; Name: "{app}\upgrade_portable.ps1"

[Run]
Filename: "{app}\DocMind.exe"; Description: "启动 DocMind"; Flags: nowait postinstall skipifsilent
