; Instalador de Windows para "Traductor del culto".
; No requiere Python ni terminal en la maquina de destino: todo lo que hace
; falta ya esta adentro del build de PyInstaller que este script empaqueta.
;
; Para reconstruir todo desde cero, en Windows (no Mac: PyInstaller arma un
; binario para el sistema donde corre):
;
;   .venv\Scripts\pyinstaller.exe --noconfirm --windowed ^
;     --name "Traductor del culto" --version-file version_info.txt ^
;     --add-data "traductor\paginas;traductor\paginas" ^
;     --add-data ".venv\Lib\site-packages\piper\espeak-ng-data;piper\espeak-ng-data" ^
;     --add-data ".venv\Lib\site-packages\faster_whisper\assets;faster_whisper\assets" ^
;     --runtime-hook pyi_rth_numpy_dlls.py ^
;     --icon traductor\paginas\estaticos\icono.ico ventana.py
;
; El --runtime-hook es necesario: sin el, numpy puede fallar con "DLL load
; failed" en maquinas reales (x64) aunque haya andado bien en una VM de
; prueba -- ver pyi_rth_numpy_dlls.py para el detalle completo.
;
; Eso deja dist\Traductor del culto\ (el .exe + todo lo que necesita), que es
; lo que este script empaqueta. OJO: esa carpeta tiene que estar limpia --
; sin config.yaml, .env ni voces/ propios (el build no los genera, pero si
; alguien los prueba ahi a mano quedan pegados al instalador de todo el
; mundo). Esos tres archivos los pone este script aparte, desde las
; plantillas del repo (config.4idiomas.yaml, .env.ejemplo, glosario.yaml).
;
; Despues, con Inno Setup instalado:
;
;   ISCC.exe instalador.iss
;
; Deja el instalador en salida\Instalar Traductor del culto.exe
;
; El build de PyInstaller es siempre CPU (no bundlea las libererias de CUDA:
; son ~1.3GB y la mayoria de las instalaciones no tiene placa NVIDIA). Si
; esta PC SI tiene una, el instalador las baja aparte -- ver instalar_cuda.ps1
; y HayNvidia() mas abajo -- directo de PyPI, y las deja donde
; traductor/stt.py ya sabe buscarlas. Sin esto Whisper corre en CPU aunque
; haya GPU, que sigue andando bien pero mas lento.

#define MyAppName "Traductor del culto"
#define MyAppVersion "1.0"
#define MyAppExeName "Traductor del culto.exe"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName=C:\traductor
DisableDirPage=no
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=salida
OutputBaseFilename=Instalar Traductor del culto
SetupIconFile=traductor\paginas\estaticos\icono.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
; Rutas cortas: espeak-ng-data trunca su propia ruta pasado cierto largo
; (ver traductor/tts.py). C:\traductor de entrada evita ese problema.
UsePreviousAppDir=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
Source: "dist\Traductor del culto\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion
Source: "config.4idiomas.yaml"; DestDir: "{app}"; DestName: "config.yaml"; Flags: onlyifdoesntexist
Source: ".env.ejemplo"; DestDir: "{app}"; DestName: ".env"; Flags: onlyifdoesntexist
Source: "glosario.yaml"; DestDir: "{app}"; Flags: onlyifdoesntexist
; dontcopy: no se instala en {app}, solo queda disponible para
; ExtractTemporaryFile() durante la instalacion (ver CurStepChanged).
Source: "instalar_cuda.ps1"; DestDir: "{tmp}"; Flags: dontcopy

[Dirs]
Name: "{app}\voces"
Name: "{app}\registros"

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; ffmpeg: solo hace falta para "ensayar con un video" (traductor/fuente.py).
; runasoriginaluser: winget resuelve mal su contexto corriendo como el
; usuario elevado del instalador; necesita correr como quien lo esta
; instalando de verdad.
Filename: "winget.exe"; \
    Parameters: "install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements"; \
    StatusMsg: "Instalando ffmpeg (para poder ensayar con un video)..."; \
    Check: not HayFfmpeg; Flags: runasoriginaluser runhidden

; CUDA: solo si esta PC tiene una placa NVIDIA Y todavia no las bajo (correr
; el instalador de nuevo sobre una instalacion existente no tiene por que
; volver a bajar 1.3GB que ya estan).
Filename: "powershell.exe"; \
    Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{tmp}\instalar_cuda.ps1"" -Destino ""{app}\_internal"""; \
    StatusMsg: "Instalando librerías de GPU (placa NVIDIA detectada, tarda varios minutos)..."; \
    Check: NecesitaCuda; Flags: runhidden

Filename: "notepad.exe"; Parameters: """{app}\.env"""; Description: "Abrir .env para cargar la clave de traducción"; Flags: postinstall unchecked shellexec

[Code]
var
  PinGenerado: String;

function HayFfmpeg(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('where.exe', 'ffmpeg', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)
    and (ResultCode = 0);
end;

function HayNvidia(): Boolean;
var
  ResultCode: Integer;
  Salida: AnsiString;
  ArchivoTmp: String;
begin
  Result := False;
  ArchivoTmp := ExpandConstant('{tmp}\gpu.txt');
  // Get-CimInstance en vez de wmic (deprecado / puede faltar en Windows
  // nuevos). El nombre de la placa alcanza: no hace falta que el driver
  // ya este instalado para detectarla.
  if Exec('powershell.exe',
      '-NoProfile -Command "(Get-CimInstance Win32_VideoController).Name ' +
      '| Out-File -Encoding utf8 ''' + ArchivoTmp + '''"',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    if LoadStringFromFile(ArchivoTmp, Salida) then
      Result := Pos('NVIDIA', Uppercase(Salida)) > 0;
  end;
end;

function NecesitaCuda(): Boolean;
var
  Base: String;
begin
  // Si ya estan las subcarpetas que dejan los wheels (cublas/, cudnn/), no
  // hay nada que bajar de nuevo -- correr el instalador otra vez (una
  // actualizacion, por ejemplo) no tiene por que repetir una descarga de
  // 1.3GB. No alcanza con que exista la carpeta "nvidia": instalar_cuda.ps1
  // la crea siempre antes de bajar nada, asi que por si sola no confirma
  // que la descarga anterior haya llegado a buen puerto.
  Base := ExpandConstant('{app}\_internal\nvidia');
  Result := HayNvidia() and not (DirExists(Base + '\cublas') or DirExists(Base + '\cudnn'));
end;

function TienePinActivo(RutaEnv: String): Boolean;
var
  Lineas: TStringList;
  i: Integer;
begin
  Result := False;
  Lineas := TStringList.Create;
  try
    Lineas.LoadFromFile(RutaEnv);
    for i := 0 to Lineas.Count - 1 do
      // Trim() saca espacios; esto NO tiene que engancharse con la linea de
      // ejemplo comentada ("# PANEL_PIN=..."), que Pos() sobre el archivo
      // entero confundia con un PIN ya activo.
      if Copy(Trim(Lineas[i]), 1, 10) = 'PANEL_PIN=' then
      begin
        Result := True;
        Break;
      end;
  finally
    Lineas.Free;
  end;
end;

procedure GenerarPin();
var
  RutaEnv: String;
  Contenido: AnsiString;
  Pin: String;
  i: Integer;
begin
  RutaEnv := ExpandConstant('{app}\.env');
  if TienePinActivo(RutaEnv) then
    exit;
  if not LoadStringFromFile(RutaEnv, Contenido) then
    exit;

  Pin := '';
  for i := 1 to 6 do
    Pin := Pin + IntToStr(Random(10));

  SaveStringToFile(
    RutaEnv,
    Contenido + #13#10 +
    '# Generado por el instalador. Lo pide el panel la primera vez que se' + #13#10 +
    '# cambia algo (una vez por dispositivo).' + #13#10 +
    'PANEL_PIN=' + Pin + #13#10,
    False
  );
  PinGenerado := Pin;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    GenerarPin();
    // Tiene que estar disponible ANTES de que corran los [Run], que se
    // ejecutan despues de ssPostInstall.
    ExtractTemporaryFile('instalar_cuda.ps1');
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpFinished then
  begin
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'Antes de arrancar:' + #13#10 +
      '1. Cargá tu clave de traducción en el archivo .env (se abre solo si dejaste tildado "Abrir .env...").' + #13#10 +
      '2. Revisá los nombres de placa de audio en config.yaml.';
    if PinGenerado <> '' then
      WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
        'PIN del panel (una sola vez por dispositivo): ' + PinGenerado;
  end;
end;
