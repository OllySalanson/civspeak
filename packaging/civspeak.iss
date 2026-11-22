; Inno Setup script for Civspeak-Setup.exe: installs dist\Civspeak for this Windows user only, into
; %LOCALAPPDATA%\Programs\Civspeak, so it never asks for administrator rights.
; Build (after PyInstaller):  iscc /DAppVersion=1.2.3 /DIconFile=build\civspeak\civspeak.ico packaging\civspeak.iss
; Upgrades install over the top: the player's settings, index and downloaded voice live in
; %LOCALAPPDATA%\Civspeak, outside the program folder, and are kept.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=1.2.3 ...
#endif
#ifndef IconFile
  #define IconFile "..\build\civspeak\civspeak.ico"
#endif

[Setup]
; Never change AppId: Windows knows an installed Civspeak (and its upgrades) by it.
AppId={{6C1E57B2-3F4A-4E39-9A0B-7D2C5B9E8F41}
AppName=Civspeak
AppVersion={#AppVersion}
AppVerName=Civspeak {#AppVersion}
AppPublisher=Civspeak
AppPublisherURL=https://github.com/OllySalanson/civspeak
AppSupportURL=https://github.com/OllySalanson/civspeak/issues
AppUpdatesURL=https://github.com/OllySalanson/civspeak/releases
AppComments=Ask the Civ VI Civilopedia out loud. An unofficial fan tool, not made by Firaxis or 2K.
VersionInfoVersion={#AppVersion}
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Civspeak
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
UsePreviousTasks=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; The running app (or the WSL helper) holds this; Setup asks for it to be closed first.
AppMutex=Local\CivspeakHelper
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename=Civspeak-Setup
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\Civspeak.exe
UninstallDisplayName=Civspeak
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Put a Civspeak shortcut on the desktop"
Name: "startatlogin"; Description: "Start Civspeak when I log in to Windows"

[Files]
Source: "..\dist\Civspeak\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
; An upgrade replaces the whole program folder, so no file from an older version is left behind.
Type: filesandordirs; Name: "{app}\_internal"

[Icons]
Name: "{autoprograms}\Civspeak"; Filename: "{app}\Civspeak.exe"; Comment: "Ask the Civ VI Civilopedia out loud"
Name: "{autodesktop}\Civspeak"; Filename: "{app}\Civspeak.exe"; Comment: "Ask the Civ VI Civilopedia out loud"; Tasks: desktopicon

[Registry]
; The same per-user Run value the app's own "Start when I log in" sets (winhelper.set_start_at_login).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "Civspeak"; \
  ValueData: "{code:RunCommand}"; Tasks: startatlogin; Flags: uninsdeletevalue

[Run]
Filename: "{app}\Civspeak.exe"; Description: "Start Civspeak now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; Forget the Claude key too, when the player chose to remove everything (see below).
Filename: "{sys}\cmdkey.exe"; Parameters: "/delete:""Civspeak/Claude API key"""; Flags: runhidden; \
  RunOnceId: "ForgetKey"; Check: RemoveEverything

[Code]
var
  RemoveAll: Boolean;

{ The command line exactly as the app writes it (subprocess.list2cmdline), so its tray menu shows the
  tick: quoted only when the path has a space. }
function RunCommand(Param: String): String;
begin
  Result := ExpandConstant('{app}\Civspeak.exe');
  if Pos(' ', Result) > 0 then
    Result := '"' + Result + '"';
end;

function RemoveEverything(): Boolean;
begin
  Result := RemoveAll;
end;

function InitializeUninstall(): Boolean;
begin
  RemoveAll := (not UninstallSilent) and (MsgBox(
    'Also remove your Civspeak settings, your Claude key, the Civilopedia it built and George''s downloaded voice?'
    + #13#10#13#10 + 'Choose No to keep them for when you install Civspeak again.',
    mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES);
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and RemoveAll then
    DelTree(ExpandConstant('{localappdata}\Civspeak'), True, True, True);
end;
