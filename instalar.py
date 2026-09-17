"""Instalador. Deja la aplicacion lista para usar en esta computadora.

    Windows:  doble clic en  instalar.bat
    macOS:    doble clic en  instalar.command

Es seguro correrlo de nuevo: detecta lo que ya esta hecho y saltea. Si algo
salio mal a mitad de camino, volve a ejecutarlo.

Toda la logica vive aca (y no en el .bat) para que sea la misma en Windows y
en Mac, y para poder probarla.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import venv
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
VENV = RAIZ / ".venv"
PY_MINIMO = (3, 10)

ES_WINDOWS = platform.system() == "Windows"
ES_MAC = platform.system() == "Darwin"


# ---------------------------------------------------------------- presentacion

class C:
    """Colores ANSI. En consolas viejas de Windows quedan como texto plano,
    asi que se apagan si no hay soporte."""
    ok = azul = amar = rojo = gris = fin = ""

    @classmethod
    def activar(cls):
        if ES_WINDOWS and not os.environ.get("WT_SESSION"):
            try:
                import ctypes
                ctypes.windll.kernel32.SetConsoleMode(
                    ctypes.windll.kernel32.GetStdHandle(-11), 7
                )
            except Exception:
                return
        cls.ok, cls.azul, cls.amar = "\033[32m", "\033[36m", "\033[33m"
        cls.rojo, cls.gris, cls.fin = "\033[31m", "\033[90m", "\033[0m"


_paso = 0


def paso(texto: str) -> None:
    global _paso
    _paso += 1
    print(f"\n{C.azul}[{_paso}]{C.fin} {texto}")


def bien(texto: str) -> None:
    print(f"    {C.ok}OK{C.fin}  {texto}")


def aviso(texto: str) -> None:
    print(f"    {C.amar}!{C.fin}   {texto}")


def morir(texto: str, ayuda: str = "") -> None:
    print(f"\n{C.rojo}No se pudo continuar:{C.fin} {texto}")
    if ayuda:
        print(f"\n{ayuda}")
    esperar_enter()
    sys.exit(1)


def esperar_enter() -> None:
    """Sin esto, al hacer doble clic la ventana se cierra antes de leer nada."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input(f"\n{C.gris}Enter para cerrar...{C.fin}")
        except (EOFError, KeyboardInterrupt):
            pass


# ------------------------------------------------------------------- utilidades

def py_venv() -> Path:
    return VENV / ("Scripts/python.exe" if ES_WINDOWS else "bin/python")


def correr(cmd: list, que: str, mostrar: bool = False) -> None:
    try:
        r = subprocess.run(cmd, capture_output=not mostrar, text=True)
    except FileNotFoundError:
        morir(f"no encontre el programa para {que}: {cmd[0]}")
    if r.returncode != 0:
        detalle = (r.stderr or r.stdout or "")[-1200:] if not mostrar else ""
        morir(f"falló {que}.", detalle)


# ----------------------------------------------------------------------- pasos

def verificar_python() -> None:
    paso("Verificando Python")
    if sys.version_info < PY_MINIMO:
        morir(
            f"Python {'.'.join(map(str, PY_MINIMO))} o superior; tenés "
            f"{platform.python_version()}.",
            "Bajalo de https://www.python.org/downloads/\n"
            + ("IMPORTANTE: tildá 'Add python.exe to PATH' en la primera pantalla."
               if ES_WINDOWS else ""),
        )
    bien(f"Python {platform.python_version()} en {platform.system()}")


def verificar_ruta() -> None:
    paso("Verificando dónde está instalado")
    largo = len(str(RAIZ))
    # Windows corta las rutas a 260 caracteres salvo que se active el soporte
    # de rutas largas, y .venv\Lib\site-packages\... agrega mas de 100 por su
    # cuenta. Sobre eso, algunas librerias tienen sus propios limites internos
    # mas bajos. Conviene avisar antes de que falle a mitad de la instalacion.
    if largo > 90:
        aviso(
            f"la ruta tiene {largo} caracteres y es bastante larga.\n"
            "        Si algo falla, mové la carpeta a un lugar corto "
            "(por ejemplo C:\\traductor)."
        )
    else:
        bien(f"ruta de {largo} caracteres")


def verificar_dependencias_sistema() -> None:
    paso("Verificando dependencias del sistema")
    if ES_MAC:
        if shutil.which("brew") is None:
            aviso("No tenés Homebrew. Si falla el audio: https://brew.sh")
        else:
            r = subprocess.run(["brew", "list", "portaudio"], capture_output=True)
            if r.returncode != 0:
                print("    instalando portaudio (necesario para el audio)...")
                correr(["brew", "install", "portaudio"], "instalar portaudio")
            bien("portaudio")
    else:
        # En Windows, sounddevice trae PortAudio incluido en la rueda.
        bien("no hace falta nada aparte en Windows")

    if shutil.which("ffmpeg") is None:
        aviso("Sin ffmpeg. Solo hace falta para tools/bajar_culto.py "
              "(probar con cultos de YouTube).")
    else:
        bien("ffmpeg")


def crear_venv() -> None:
    paso("Preparando el entorno de Python")
    if py_venv().exists():
        bien("ya existía, lo reutilizo")
        return
    print(f"    creando {VENV.name}/ ...")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    except Exception as e:
        morir(f"no pude crear el entorno virtual: {e}")
    bien("entorno creado")


def instalar_paquetes() -> None:
    paso("Instalando las librerías (tarda unos minutos la primera vez)")
    py = str(py_venv())
    correr([py, "-m", "pip", "install", "--upgrade", "pip", "-q"], "actualizar pip")
    correr([py, "-m", "pip", "install", "-r", str(RAIZ / "requirements.txt")],
           "instalar las librerías", mostrar=True)
    bien("librerías instaladas")


def voces_de_config() -> list[str]:
    """Lee config.yaml sin depender de PyYAML: todavia puede no estar instalado."""
    cfg = RAIZ / "config.yaml"
    if not cfg.exists():
        return ["en_US-lessac-medium"]
    voces = []
    for linea in cfg.read_text(encoding="utf-8").splitlines():
        limpia = linea.split("#")[0].strip()
        if limpia.startswith("voz:"):
            voz = limpia.split(":", 1)[1].strip().strip('"').strip("'")
            if voz:
                voces.append(voz)
    return voces or ["en_US-lessac-medium"]


def bajar_voces() -> None:
    paso("Bajando las voces de síntesis")
    carpeta = RAIZ / "voces"
    carpeta.mkdir(exist_ok=True)
    faltan = [v for v in voces_de_config() if not (carpeta / f"{v}.onnx").exists()]
    if not faltan:
        bien("ya estaban todas")
        return
    print(f"    faltan: {', '.join(faltan)}")
    # No se usa correr(): en macOS, con pywebview ya instalado en el mismo
    # entorno, el subproceso de piper a veces crashea con un error nativo de
    # Objective-C (recursive_mutex) AL SALIR, despues de bajar los archivos
    # sin problema. El codigo de salida miente en ese caso, asi que en vez de
    # confiar en el se verifica lo que de verdad importa: si el archivo esta.
    try:
        subprocess.run(
            [str(py_venv()), "-m", "piper.download_voices", *faltan,
             "--data-dir", str(carpeta)]
        )
    except FileNotFoundError:
        morir("no encontre el interprete de Python para bajar las voces")

    siguen_faltando = [v for v in faltan if not (carpeta / f"{v}.onnx").exists()]
    if siguen_faltando:
        morir(
            f"no pude bajar: {', '.join(siguen_faltando)}",
            "Revisá la conexión a internet y volvé a correr el instalador.",
        )
    bien(f"{len(faltan)} voz/voces lista(s)")


def generar_pin() -> str:
    import secrets

    return "".join(secrets.choice("0123456789") for _ in range(6))


def asegurar_pin_fijo() -> str | None:
    """Si .env no tiene un PANEL_PIN activo, le agrega uno generado ahora.

    La ventana de la aplicacion (ventana.py) no tiene terminal donde mostrar
    un PIN al azar por sesion, asi que necesita uno fijo desde el primer
    arranque. Generarlo aca evita un paso manual mas: sin esto, la primera
    vez que alguien abre la app se encontraria con una pantalla pidiendo
    configurar algo en un archivo que nunca supo que existia.

    Devuelve el PIN si lo genero (para mostrarlo al final), o None si ya
    habia uno.
    """
    env = RAIZ / ".env"
    texto = env.read_text(encoding="utf-8")
    if re.search(r"^PANEL_PIN=.+$", texto, re.M):
        return None
    pin = generar_pin()
    if not texto.endswith("\n"):
        texto += "\n"
    texto += f"\n# Generado por el instalador. Es lo que va a pedir la app la\n"
    texto += f"# primera vez que se cambia algo desde el panel (una vez por dispositivo).\n"
    texto += f"PANEL_PIN={pin}\n"
    env.write_text(texto, encoding="utf-8")
    return pin


def preparar_env() -> bool:
    """Devuelve True si hay que pedirle al usuario que cargue la clave."""
    paso("Configurando la clave del traductor")
    env, ejemplo = RAIZ / ".env", RAIZ / ".env.ejemplo"
    if not env.exists():
        shutil.copy(ejemplo, env)
        bien(f"creé {env.name} a partir del ejemplo")
        return True

    # Si el .env todavia tiene los marcadores, la clave no se cargo.
    texto = env.read_text(encoding="utf-8")
    sin_cargar = any(m in texto for m in ("aca-va-tu-clave", "aca-va-tu-clave-de-gemini"))
    if sin_cargar:
        aviso(f"{env.name} existe pero todavía tiene el texto de ejemplo")
        return True
    bien("ya tiene una clave cargada")
    return False


def crear_acceso_directo_windows(ruta_lnk: Path, objetivo: str, argumentos: str,
                                 carpeta: Path, icono: Path | None = None) -> bool:
    """Crea un .lnk de Windows, el unico tipo de acceso directo al que se le
    puede poner un icono propio (un .bat siempre se ve con el icono generico
    de engranaje, sin importar que icono tenga el programa que ejecuta).

    Se arma con PowerShell y el objeto COM WScript.Shell: no hace falta
    ninguna libreria de Python nueva (pywin32, etc.), PowerShell viene en
    cualquier Windows desde hace mas de una decada. Devuelve False si algo
    sale mal, para que quien llama pueda caer a un .bat comun en vez de
    quedarse sin ningun acceso directo.
    """
    script = f'''
$s = New-Object -ComObject WScript.Shell
$a = $s.CreateShortcut("{ruta_lnk}")
$a.TargetPath = "{objetivo}"
$a.Arguments = "{argumentos}"
$a.WorkingDirectory = "{carpeta}"
{f'$a.IconLocation = "{icono}"' if icono else ""}
$a.Save()
'''
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and ruta_lnk.exists()


def crear_lanzadores() -> None:
    paso("Creando los accesos directos")
    if ES_WINDOWS:
        # pythonw.exe no abre consola (a diferencia de python.exe): es lo que
        # hace que esto se sienta como una aplicacion y no como un script.
        # Viene con cualquier instalacion normal de Python; si por algun
        # motivo no esta, se cae a python.exe, que si muestra una consola
        # pero al menos arranca.
        py_ventana = VENV / "Scripts" / "pythonw.exe"
        if not py_ventana.exists():
            aviso("no encontré pythonw.exe, uso python.exe (va a mostrar una consola)")
            py_ventana = VENV / "Scripts" / "python.exe"
        # python.exe (con consola) para actualizar: conviene ver el progreso.
        py_consola = VENV / "Scripts" / "python.exe"
        icono = RAIZ / "traductor" / "paginas" / "estaticos" / "icono.ico"

        (RAIZ / "iniciar.bat").write_text(
            "@echo off\r\ncd /d \"%~dp0\"\r\n"
            f"start \"\" \"{py_ventana}\" ventana.py\r\n",
            encoding="utf-8",
        )
        bien("iniciar.bat")

        escritorio = Path(os.path.expandvars(r"%USERPROFILE%\Desktop"))
        if not escritorio.is_dir():
            return

        # Dos accesos directos separados, a proposito: el de todos los dias
        # (para quien esta de turno, sin saber nada de esto) y el de
        # actualizar (para vos, cuando te avise de un cambio). Nunca deberia
        # bajar codigo nuevo mientras la traduccion esta corriendo en vivo,
        # asi que actualizar es su propio icono, no un boton dentro de la app.
        ok_iniciar = crear_acceso_directo_windows(
            escritorio / "Traductor del culto.lnk",
            str(py_ventana), "ventana.py", str(RAIZ), icono if icono.exists() else None,
        )
        ok_actualizar = crear_acceso_directo_windows(
            escritorio / "Actualizar traductor.lnk",
            str(py_consola), "actualizar.py", str(RAIZ), icono if icono.exists() else None,
        )
        if ok_iniciar and ok_actualizar:
            bien("accesos directos en el Escritorio, con ícono")
        else:
            # PowerShell fallando aca seria muy raro, pero mejor dejar algo
            # utilizable (sin icono propio) que nada.
            aviso("no pude crear los accesos con ícono; dejo un .bat simple")
            (escritorio / "Traductor del culto.bat").write_text(
                f"@echo off\r\ncd /d \"{RAIZ}\"\r\n"
                f"start \"\" \"{py_ventana}\" ventana.py\r\n",
                encoding="utf-8",
            )
            (escritorio / "Actualizar traductor.bat").write_text(
                f"@echo off\r\ncd /d \"{RAIZ}\"\r\n"
                f"\"{py_consola}\" actualizar.py\r\npause\r\n",
                encoding="utf-8",
            )
    else:
        lanzador = RAIZ / "iniciar.command"
        lanzador.write_text(
            "#!/bin/bash\n"
            'cd "$(dirname "$0")"\n'
            ".venv/bin/python ventana.py\n",
            encoding="utf-8",
        )
        lanzador.chmod(0o755)
        bien("iniciar.command")


def verificar() -> None:
    paso("Verificando la instalación")
    r = subprocess.run(
        [str(py_venv()), "-c",
         "import sys; sys.path.insert(0,'.');"
         "import sounddevice, faster_whisper, piper, soxr, yaml;"
         "from traductor import config; c = config.cargar();"
         "print('|'.join([str(len(c.salidas)), c.traduccion.proveedor, c.stt.modelo]))"],
        capture_output=True, text=True, cwd=RAIZ,
    )
    if r.returncode != 0:
        morir("la instalación quedó incompleta.", r.stderr[-1200:])
    salidas, proveedor, modelo = r.stdout.strip().split("|")
    bien(f"{salidas} idioma(s) de salida · traductor: {proveedor} · whisper: {modelo}")


def resumen(falta_clave: bool, pin_generado: str | None) -> None:
    # El ".\" adelante es obligatorio en PowerShell, que no ejecuta rutas
    # relativas sin el; en cmd.exe tambien funciona, asi que sirve para los dos.
    py = ".\\.venv\\Scripts\\python" if ES_WINDOWS else ".venv/bin/python"
    arranque = ".\\iniciar.bat" if ES_WINDOWS else "./iniciar.command"
    print(f"\n{C.ok}{'=' * 62}{C.fin}")
    print(f"{C.ok}  Instalación terminada{C.fin}")
    print(f"{C.ok}{'=' * 62}{C.fin}\n")

    n = 1
    if falta_clave:
        editor = "notepad .env" if ES_WINDOWS else "open -e .env"
        print(f"  {n}. Cargá la clave del traductor en el archivo .env")
        print(f"     {C.gris}{editor}{C.fin}")
        print(f"     {C.gris}Gemini: https://aistudio.google.com/apikey{C.fin}\n")
        n += 1
    print(f"  {n}. Verificá que la clave funcione")
    print(f"     {C.gris}{py} -m tools.probar_traduccion{C.fin}\n")
    n += 1
    print(f"  {n}. Mirá qué placas de audio hay y ponelas en config.yaml")
    print(f"     {C.gris}{py} -m tools.dispositivos{C.fin}\n")
    n += 1
    print(f"  {n}. Probá sin hardware")
    print(f"     {C.gris}{py} -m tools.simulacro --demo{C.fin}\n")
    n += 1
    print(f"  {n}. Arrancá: {C.azul}{arranque}{C.fin}")
    print(f"     {C.gris}abre una ventana con el panel, sin terminal ni navegador —{C.fin}")
    print(f"     {C.gris}esto es lo que usa quien esté de turno en sonido{C.fin}\n")
    if pin_generado:
        print(f"  PIN del panel: {C.amar}{pin_generado}{C.fin}  {C.gris}(guardalo en .env, se generó ahora){C.fin}")
        print(f"     {C.gris}lo pide la primera vez que se cambia algo desde el panel,{C.fin}")
        print(f"     {C.gris}una sola vez por celular o laptop{C.fin}\n")
    if ES_WINDOWS:
        print(f"  En el Escritorio quedaron dos íconos:")
        print(f"     {C.azul}Traductor del culto{C.fin}   {C.gris}— para quien esté de turno{C.fin}")
        print(f"     {C.azul}Actualizar traductor{C.fin}  {C.gris}— para vos, cuando haya un cambio{C.fin}\n")
    print(f"  {C.gris}Para depurar o hacer un ensayo (--archivo, --verboso, etc.) se sigue{C.fin}")
    print(f"  {C.gris}usando  {py} main.py  desde una terminal, como siempre.{C.fin}\n")
    print(f"  {C.gris}Todo el detalle está en README.md{C.fin}")


def main() -> int:
    C.activar()
    print(f"\n{C.azul}{'=' * 62}{C.fin}")
    print(f"{C.azul}  Traductor automático de culto — instalación{C.fin}")
    print(f"{C.azul}{'=' * 62}{C.fin}")
    print(f"{C.gris}  {RAIZ}{C.fin}")

    verificar_python()
    verificar_ruta()
    verificar_dependencias_sistema()
    crear_venv()
    instalar_paquetes()
    bajar_voces()
    falta_clave = preparar_env()
    pin_generado = asegurar_pin_fijo()
    crear_lanzadores()
    verificar()
    resumen(falta_clave, pin_generado)
    esperar_enter()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nCancelado.")
        sys.exit(130)
