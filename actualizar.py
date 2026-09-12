"""Traer la ultima version del proyecto, sin git.

    Windows:  doble clic en  actualizar.bat
    macOS:    .venv/bin/python actualizar.py

Baja el codigo de GitHub y reemplaza los archivos del programa. NO toca nada
de lo que configuraste en esta computadora: .env, config.yaml, glosario.yaml,
las voces bajadas ni los registros de los cultos quedan como estan.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import urlopen

RAIZ = Path(__file__).resolve().parent
ZIP = "https://github.com/evolvetech-consulting/church-live-translator/archive/refs/heads/main.zip"

# Lo que se actualiza: codigo del programa.
CODIGO = ("main.py", "instalar.py", "actualizar.py", "requirements.txt", "README.md")
CARPETAS = ("traductor", "tools")

# Lo que NUNCA se pisa: es la configuracion de esta iglesia y su historial.
INTOCABLES = (".env", "config.yaml", "glosario.yaml", "voces", "registros",
              "pruebas", "simulacro", ".venv")


def main() -> int:
    print(f"\n  Actualizando {RAIZ}\n")
    print("  Bajando la última versión...")
    try:
        with urlopen(ZIP, timeout=60) as r:
            datos = r.read()
    except Exception as e:
        print(f"\n  No pude bajar la actualización: {e}")
        print("  ¿Hay internet en esta computadora?")
        return esperar(1)

    with zipfile.ZipFile(io.BytesIO(datos)) as z:
        tmp = Path(tempfile.mkdtemp())
        z.extractall(tmp)
    origen = next(tmp.iterdir())  # el zip trae todo dentro de una carpeta

    cambios = []
    for nombre in CODIGO:
        nuevo = origen / nombre
        if not nuevo.exists():
            continue
        viejo = RAIZ / nombre
        if not viejo.exists() or viejo.read_bytes() != nuevo.read_bytes():
            shutil.copy2(nuevo, viejo)
            cambios.append(nombre)

    for carpeta in CARPETAS:
        nueva = origen / carpeta
        if not nueva.exists():
            continue
        for archivo in nueva.rglob("*"):
            if archivo.is_dir() or "__pycache__" in archivo.parts:
                continue
            rel = archivo.relative_to(origen)
            destino = RAIZ / rel
            if destino.exists() and destino.read_bytes() == archivo.read_bytes():
                continue
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archivo, destino)
            cambios.append(str(rel).replace("\\", "/"))

    shutil.rmtree(tmp, ignore_errors=True)

    if cambios:
        print(f"\n  Se actualizaron {len(cambios)} archivo(s):")
        for c in sorted(cambios):
            print(f"    {c}")
    else:
        print("\n  Ya estaba al día, no hubo cambios.")

    print(f"\n  Sin tocar: {', '.join(INTOCABLES[:4])} y el resto de tu configuración.")
    print("\n  Si la aplicación estaba abierta, cerrala y volvé a abrirla.")
    return esperar(0)


def esperar(codigo: int) -> int:
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\n  Enter para cerrar...")
        except (EOFError, KeyboardInterrupt):
            pass
    return codigo


if __name__ == "__main__":
    sys.exit(main())
