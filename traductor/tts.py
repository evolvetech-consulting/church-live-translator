"""Sintesis de voz local con Piper.

Corre entero en esta maquina: sin costo por culto y sin depender de internet.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from piper import PiperVoice, SynthesisConfig

CARPETA_VOCES = Path("voces")


def ruta_voz(voz: str, carpeta: Path = CARPETA_VOCES) -> Path:
    modelo = carpeta / f"{voz}.onnx"
    if not modelo.exists():
        raise FileNotFoundError(
            f"Falta la voz {voz!r} en {carpeta}/.\n"
            f"Bajarla con:\n"
            f"  .venv/bin/python -m piper.download_voices {voz} --data-dir {carpeta}"
        )
    return modelo


class MotorTTS:
    """Convierte texto en audio. Una instancia por idioma de salida."""

    def __init__(self, voz: str, velocidad_base: float = 1.0,
                 carpeta: Path = CARPETA_VOCES):
        self.voz = voz
        self.velocidad_base = velocidad_base
        self._voice = PiperVoice.load(ruta_voz(voz, carpeta))
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
