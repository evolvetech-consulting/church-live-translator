"""Carga de la clave de Claude desde un archivo .env.

La PC de la iglesia la va a operar quien este de turno, no quien la configuro.
Pedirle que edite las variables de entorno de Windows es pedir demasiado: un
archivo de texto al lado de config.yaml se entiende solo.

Las variables que YA esten definidas en el entorno tienen prioridad: si alguien
exporto la clave a mano, el archivo no se la pisa.
"""

from __future__ import annotations

import os
from pathlib import Path


def cargar_env(ruta: str | Path = ".env") -> list[str]:
    """Lee un .env sencillo (CLAVE=valor). Devuelve las claves que definio."""
    ruta = Path(ruta)
    if not ruta.exists():
        return []

    definidas = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        nombre, _, valor = linea.partition("=")
        nombre = nombre.strip()
        valor = valor.strip().strip('"').strip("'")
        if nombre and valor and nombre not in os.environ:
            os.environ[nombre] = valor
            definidas.append(nombre)
    return definidas
