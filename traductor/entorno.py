"""Carga de la clave de Claude desde un archivo .env.

La PC de la iglesia la va a operar quien este de turno, no quien la configuro.
Pedirle que edite las variables de entorno de Windows es pedir demasiado: un
archivo de texto al lado de config.yaml se entiende solo.

Las variables que YA esten definidas en el entorno tienen prioridad: si alguien
exporto la clave a mano, el archivo no se la pisa.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

# Valor a proposito para desactivar el PIN del panel (ver resolver_pin), en
# vez de depender de que un .env con un valor vacio se comporte de una forma
# particular.
_SIN_PIN = "ninguno"


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


def resolver_pin() -> str:
    """PIN que protege los controles del panel (pausar, cambiar audio, etc.).

    Cualquiera en la red de la iglesia llega al panel, y sin esto cualquiera
    tambien podria pausar la traduccion, cambiar la salida de un idioma o
    borrar la transcripcion en medio de un culto con un solo POST, sin que
    nadie se entere. Los subtitulos de la congregacion y el panel en si NO
    piden PIN: solo lo que cambia algo.

    Si `PANEL_PIN` esta en .env, se usa ese valor siempre (no cambia entre
    reinicios). Si no esta, se genera uno al azar de 6 digitos por sesion y se
    muestra en la terminal: hay que escribirlo en el panel una vez por
    dispositivo, que despues lo recuerda solo.

    `PANEL_PIN=ninguno` desactiva la proteccion a proposito, para una red que
    ya se considera de confianza.
    """
    configurado = os.environ.get("PANEL_PIN")
    if configurado == _SIN_PIN:
        return ""
    if configurado:
        return configurado
    return "".join(secrets.choice("0123456789") for _ in range(6))
