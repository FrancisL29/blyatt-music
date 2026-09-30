; Instalador de Blyatt (Inno Setup 6). Lo compila packaging/build.ps1:  ISCC /DAppVer=1.2.3 blyatt.iss
; Instalacion por usuario (sin admin) en %LOCALAPPDATA%\Programs\Blyatt: el auto-update puede reemplazar
; los archivos sin pedir permisos. Los datos (sesiones, cache) viven aparte en %LOCALAPPDATA%\Blyatt.

#ifndef AppVer
  #define AppVer "0.0.0"
#endif

[Setup]
AppId={{7CA462B4-C230-4A92-81BD-31E57A84AA92}
AppName=Blyatt
AppVersion={#AppVer}
AppVerName=Blyatt {#AppVer}
AppPublisher=FrancisL29
AppPublisherURL=https://github.com/FrancisL29/blyatt-music
AppUpdatesURL=https://github.com/FrancisL29/blyatt-music/releases
DefaultDirName={localappdata}\Programs\Blyatt
DisableDirPage=auto
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\build\installer
OutputBaseFilename=Blyatt-Setup-{#AppVer}
SetupIconFile=..\assets\blyatt.ico
UninstallDisplayIcon={app}\Blyatt.exe
UninstallDisplayName=Blyatt
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "es"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; una actualizacion no debe dejar librerias de la version anterior mezcladas con las nuevas
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\build\dist\Blyatt\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "bin\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: NeedsWebView2

[Icons]
Name: "{autoprograms}\Blyatt"; Filename: "{app}\Blyatt.exe"
Name: "{autodesktop}\Blyatt"; Filename: "{app}\Blyatt.exe"; Tasks: desktopicon

[Run]
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; StatusMsg: "Instalando Microsoft Edge WebView2..."; Check: NeedsWebView2; Flags: waituntilterminated
Filename: "{app}\Blyatt.exe"; Description: "{cm:LaunchProgram,Blyatt}"; Flags: nowait postinstall skipifsilent
; actualizacion automatica (instalador silencioso lanzado por la app): reabrir Blyatt al terminar
Filename: "{app}\Blyatt.exe"; Flags: nowait; Check: IsRelaunch

[Code]
const
  AppMutex = 'BlyattAppMutex';
  WV2Key = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasParam(const P: String): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), P) = 0 then
      Result := True;
end;

function IsRelaunch(): Boolean;
begin
  Result := WizardSilent and HasParam('/RELAUNCH');
end;

function NeedsWebView2(): Boolean;
var
  V: String;
begin
  // Windows 11 y la mayoria de Windows 10 ya lo traen (Edge); sin el la ventana de Blyatt no abre
  if not RegQueryStringValue(HKLM32, WV2Key, 'pv', V) then
    if not RegQueryStringValue(HKCU, WV2Key, 'pv', V) then
      V := '';
  Result := (V = '') or (V = '0.0.0.0');
end;

function InitializeSetup(): Boolean;
var
  I: Integer;
begin
  Result := True;
  if WizardSilent then
  begin
    // auto-update: la app lanza este instalador y se cierra; esperar a que termine de salir
    I := 0;
    while CheckForMutexes(AppMutex) and (I < 120) do
    begin
      Sleep(250);
      I := I + 1;
    end;
    Result := not CheckForMutexes(AppMutex);
  end
  else
    while CheckForMutexes(AppMutex) do
      if MsgBox('Blyatt está abierto. Ciérralo y pulsa Reintentar.', mbError, MB_RETRYCANCEL) = IDCANCEL then
      begin
        Result := False;
        Exit;
      end;
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  while CheckForMutexes(AppMutex) do
    if UninstallSilent or (MsgBox('Blyatt está abierto. Ciérralo y pulsa Reintentar.', mbError, MB_RETRYCANCEL) = IDCANCEL) then
    begin
      Result := False;
      Exit;
    end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if (CurUninstallStep = usPostUninstall) and not UninstallSilent then
    if MsgBox('¿Borrar también tus datos de Blyatt (sesiones iniciadas y caché)?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
      DelTree(ExpandConstant('{localappdata}\Blyatt'), True, True, True);
end;
