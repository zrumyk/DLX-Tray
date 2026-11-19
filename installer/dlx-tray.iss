#define AppName "DLX Tray"
#define AppExe AppName + ".exe"
#define BundleDir "..\dist\" + AppName
#define AppVersion GetStringFileInfo(BundleDir + "\" + AppExe, "ProductVersion")

#if AppVersion == ""
  #error Run scripts/build.ps1 before compiling the installer.
#endif

[Setup]
AppId={{BCF71B3F-B7F7-4DF0-A121-9EC5CD3C6D5F}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\{#AppName}
PrivilegesRequired=lowest
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
SetupIconFile=..\src\dlx_tray\resources\dlx-tray.ico
UninstallDisplayIcon={app}\{#AppExe}
OutputDir=Output
OutputBaseFilename=DLX-Tray-Setup-{#AppVersion}
VersionInfoVersion={#AppVersion}
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "ukrainian"; MessagesFile: "compiler:Languages\Ukrainian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "{#BundleDir}\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\deeplx.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\DLX-LICENSE.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#BundleDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent unchecked

[CustomMessages]
english.CloseApp=Close DLX Tray and try uninstalling again.
ukrainian.CloseApp=Закрийте DLX Tray і повторіть спробу видалення.
english.RemoveData=Also remove DLX Tray settings, translation history, and logs from %1?
ukrainian.RemoveData=Також видалити налаштування, історію перекладів і журнали DLX Tray з %1?
english.RemoveDataFailed=Some DLX Tray data could not be removed from %1.
ukrainian.RemoveDataFailed=Не вдалося повністю видалити дані DLX Tray з %1.

[Code]
function CanRemoveExecutable(const FileName: String): Boolean;
var
  Stream: TFileStream;
begin
  Result := True;
  if not FileExists(FileName) then
    Exit;

  { Windows refuses a write handle while the executable image is loaded. }
  try
    Stream := TFileStream.Create(FileName, fmOpenWrite or fmShareDenyNone);
    Stream.Free;
  except
    Log(GetExceptionMessage);
    Result := False;
  end;
end;

function InitializeUninstall: Boolean;
begin
  Result := CanRemoveExecutable(ExpandConstant('{app}\{#AppExe}')) and
    CanRemoveExecutable(ExpandConstant('{app}\deeplx.exe'));
  if not Result then
    SuppressibleMsgBox(CustomMessage('CloseApp'), mbError, MB_OK, IDOK);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if (CurUninstallStep <> usPostUninstall) or UninstallSilent then
    Exit;

  DataDir := ExpandConstant('{userappdata}\{#AppName}');
  if DirExists(DataDir) and
    (MsgBox(FmtMessage(CustomMessage('RemoveData'), [DataDir]),
      mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES) then
  begin
    if not DelTree(DataDir, True, True, True) then
    begin
      Log('Could not remove all DLX Tray profile data.');
      MsgBox(FmtMessage(CustomMessage('RemoveDataFailed'), [DataDir]), mbError, MB_OK);
    end;
  end;
end;
