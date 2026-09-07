"""Sintesis de voz local con Piper.

Corre entero en esta maquina: sin costo por culto y sin depender de internet.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
from piper import PiperVoice, SynthesisConfig

CARPETA_VOCES = Path("voces")

# espeak-ng guarda la ruta de sus datos en un buffer interno de tamaño fijo.
# Si la ruta es mas larga, la trunca en silencio y termina buscando los datos
# en la ruta donde se compilo el paquete, que en esta maquina no existe. El
# sintomo es un error sobre un directorio /Users/runner/work/... y ninguna voz.
#
# Pasa de verdad: basta con instalar la aplicacion en algo como
# C:\Users\Iglesia\OneDrive\Documentos\Programas\church-live-translator\
# para superar el limite. Por eso, si la ruta es larga, copiamos los datos de
# espeak a un lugar corto una sola vez.
LIMITE_RUTA_ESPEAK = 130


def datos_espeak() -> Path:
    """Devuelve una ruta a espeak-ng-data que espeak pueda leer."""
    import piper

    origen = Path(piper.__file__).parent / "espeak-ng-data"
    if len(str(origen)) <= LIMITE_RUTA_ESPEAK:
        return origen

    destino = Path(tempfile.gettempdir()) / "piper-espeak-ng-data"
    if not (destino / "phontab").exists():
        shutil.copytree(origen, destino, dirs_exist_ok=True)
    return destino


def descargar_voz(voz: str, carpeta: Path = CARPETA_VOCES) -> None:
    """Baja una voz de Piper. Son ~60-120 MB cada una."""
    import subprocess

    carpeta.mkdir(parents=True, exist_ok=True)
    # Capturamos la salida: si el nombre esta mal, piper vuelca un traceback
    # entero, y lo que necesita ver quien esta configurando es una linea.
    r = subprocess.run(
        [sys.executable, "-m", "piper.download_voices", voz, "--data-dir", str(carpeta)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        detalle = (r.stderr or "").strip().splitlines()
        motivo = detalle[-1] if detalle else "error desconocido"
        motivo = motivo.split(": ", 1)[-1] if ": " in motivo else motivo
        raise RuntimeError(f"No pude bajar la voz {voz!r}: {motivo}")


def ruta_voz(voz: str, carpeta: Path = CARPETA_VOCES, bajar: bool = False) -> Path:
    modelo = carpeta / f"{voz}.onnx"
    if not modelo.exists():
        if bajar:
            print(f"Falta la voz {voz!r}, la bajo (una sola vez)...")
            descargar_voz(voz, carpeta)
        else:
            raise FileNotFoundError(
                f"Falta la voz {voz!r} en {carpeta}/.\n"
                f"Bajarla con:\n"
                f"  .venv/bin/python -m piper.download_voices {voz} --data-dir {carpeta}"
            )
    return modelo


class MotorTTS:
    """Convierte texto en audio. Una instancia por idioma de salida."""

    def __init__(self, voz: str, velocidad_base: float = 1.0,
                 carpeta: Path = CARPETA_VOCES, bajar_si_falta: bool = False):
        self.voz = voz
        self.velocidad_base = velocidad_base
        self._voice = PiperVoice.load(
            ruta_voz(voz, carpeta, bajar_si_falta),
            espeak_data_dir=datos_espeak(),
        )
        self.frecuencia = self._voice.config.sample_rate

    def sintetizar(self, texto: str, velocidad: float = 1.0) -> np.ndarray:
        """Devuelve float32 mono a self.frecuencia."""
        texto = texto.strip()
        if not texto:
            return np.empty(0, dtype=np.float32)

        # Piper usa length_scale: cuanto MAS grande, mas lento. Es el
        # inverso de "velocidad".
        factor = max(self.velocidad_base * velocidad, 0.1)
        cfg = SynthesisConfig(length_scale=1.0 / factor, normalize_audio=True)

        trozos = [
            trozo.audio_float_array
            for trozo in self._voice.synthesize(texto, syn_config=cfg)
        ]
        if not trozos:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(trozos).astype(np.float32)
