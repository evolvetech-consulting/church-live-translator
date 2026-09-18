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
;     --icon traductor\paginas\estaticos\icono.ico ventana.py
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
; Por ahora arma un build solo-CPU: no bundlea nvidia-cublas-cu12 /
; nvidia-cudnn-cu12 (~1GB), asi que Whisper no usa GPU aunque la maquina
; tenga una NVIDIA. instalar.py (el camino sin empaquetar) si detecta la
; placa e instala esas librerias solo cuando hace falta -- para sumar GPU
; aca habria que decidir entre bundlearlas siempre (mas pesado para todos)
; o armar dos builds distintos y elegir en el instalador segun la maquina.

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

[Dirs]
Name: "{app}\voces"
Name: "{app}\registros"

[Icons]
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
Filename: "notepad.exe"; Parameters: """{app}\.env"""; Description: "Abrir .env para cargar la clave de traducción"; Flags: postinstall unchecked shellexec

[Code]
var
  PinGenerado: String;

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
    GenerarPin();
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
