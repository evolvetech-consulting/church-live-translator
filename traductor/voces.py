"""Catalogo de voces de Piper: cuales hay instaladas y cuales se pueden bajar."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from urllib.request import urlopen

log = logging.getLogger(__name__)

CARPETA = Path("voces")
CATALOGO = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json?download=true"

# El catalogo se baja una sola vez por sesion: son cientos de voces y no
# cambia durante un culto.
_catalogo: dict | None = None
_lock = threading.Lock()

# Piper nombra las voces  <idioma>_<REGION>-<nombre>-<calidad>.
#
# La calidad decide el peso del modelo y, sobre todo, cuanto tarda en hablar.
# Medido en una MacBook M1 Pro sobre una frase de sermon:
#
#   low     0.16s   43x tiempo real
#   medium  0.19s   33x tiempo real
#   high    1.11s    5x tiempo real   <- casi 1 segundo mas por frase
#
# Por eso `medium` es la recomendada: la mejora de `high` es modesta y el
# costo en latencia es de casi un segundo por frase, que en vivo se nota
# mucho mas que el timbre de la voz.
CALIDADES = {
    "x_low": ("muy baja", "la mas rapida, suena robotica"),
    "low": ("baja", "muy rapida"),
    "medium": ("media", "recomendada: buen equilibrio"),
    "high": ("alta", "suma casi 1s de latencia por frase"),
}


def instaladas(carpeta: Path = CARPETA) -> list[str]:
    return sorted(p.stem for p in carpeta.glob("*.onnx"))


def catalogo() -> dict:
    global _catalogo
    with _lock:
        if _catalogo is None:
            try:
                with urlopen(CATALOGO, timeout=15) as r:
                    _catalogo = json.load(r)
            except Exception as e:
                log.warning("No pude bajar el catálogo de voces (%s).", e)
                _catalogo = {}
        return _catalogo


def _partes(nombre: str) -> tuple[str, str, str]:
    """'es_AR-daniela-high' -> ('es', 'daniela', 'high')"""
    try:
        locale, voz, calidad = nombre.split("-", 2)
        return locale.split("_")[0], voz, calidad
    except ValueError:
        return "", nombre, ""


def para_idioma(idioma: str, carpeta: Path = CARPETA) -> list[dict]:
    """Voces de ese idioma: primero las instaladas, despues las descargables."""
    puestas = set(instaladas(carpeta))
    nombres = set(puestas) | {
        n for n in catalogo() if _partes(n)[0] == idioma
    }

    salida = []
    for n in sorted(nombres):
        lengua, voz, calidad = _partes(n)
        if lengua != idioma:
            continue
        locale = n.split("-", 1)[0]
        nombre_cal, nota = CALIDADES.get(calidad, (calidad, ""))
        salida.append({
            "id": n,
            "instalada": n in puestas,
            "calidad": calidad,
            "nota": nota,
            # "daniela · es_AR · calidad alta"
            "etiqueta": f"{voz} · {locale} · calidad {nombre_cal}",
        })
    # Las que ya estan bajadas van arriba: son las que se pueden usar al toque.
    salida.sort(key=lambda v: (not v["instalada"], v["id"]))
    return salida
