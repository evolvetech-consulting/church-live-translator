"""Reconocimiento de voz local con faster-whisper."""

from __future__ import annotations

import numpy as np
from faster_whisper import WhisperModel


def _elegir_backend(dispositivo: str, tipo_computo: str) -> tuple[str, str]:
    """Resuelve 'auto' al mejor backend disponible en esta maquina."""
    if dispositivo != "auto":
        return dispositivo, ("int8" if tipo_computo == "auto" else tipo_computo)

    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", ("float16" if tipo_computo == "auto" else tipo_computo)
    except Exception:
        pass

    # CPU. int8 es varias veces mas rapido que float32 y la diferencia de
    # calidad no se nota en voz de pulpito. En Apple Silicon esto es lo unico
    # que hay: CTranslate2 no usa Metal.
    return "cpu", ("int8" if tipo_computo == "auto" else tipo_computo)


class Transcriptor:
    def __init__(self, cfg_stt, contexto_glosario: str = ""):
        self.cfg = cfg_stt
        dispositivo, computo = _elegir_backend(cfg_stt.dispositivo, cfg_stt.tipo_computo)
        self.dispositivo = dispositivo
        self.computo = computo

        self.modelo = WhisperModel(
            cfg_stt.modelo,
            device=dispositivo,
            compute_type=computo,
            cpu_threads=0,  # 0 = que decida CTranslate2 segun la maquina
        )

        # El prompt inicial sesga el reconocimiento hacia el vocabulario de la
        # iglesia: nombres propios, terminos teologicos, libros de la Biblia.
        # Es lo que evita que "Efesios" salga como "efecto".
        partes = [p for p in (contexto_glosario, cfg_stt.contexto_inicial) if p]
        self.prompt_inicial = " ".join(partes) or None

    def transcribir(self, audio: np.ndarray) -> str:
        segmentos, _ = self.modelo.transcribe(
            audio,
            language=self.cfg.idioma,
            # Busqueda voraz: en vivo, 200 ms de latencia valen mas que la
            # mejora marginal de un beam search.
            beam_size=1,
            # Sin condicionar en el texto previo: si Whisper se equivoca una
            # vez, condicionar hace que repita el error en bucle.
            condition_on_previous_text=False,
            initial_prompt=self.prompt_inicial,
            # Ya venimos segmentados por el VAD; filtrar de nuevo solo agrega
            # trabajo y puede comerse audio valido.
            vad_filter=False,
            without_timestamps=True,
        )
        return " ".join(s.text.strip() for s in segmentos).strip()
