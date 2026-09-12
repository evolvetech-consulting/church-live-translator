"""Reconocimiento de voz local con faster-whisper."""

from __future__ import annotations

import logging
import os
import platform
import sys
from pathlib import Path

import numpy as np
from faster_whisper import WhisperModel

log = logging.getLogger(__name__)

# Whisper acepta un contexto de max_length // 2 - 1 tokens y descarta el resto
# en silencio. Dejamos un margen para no quedar justo en el borde.
LIMITE_CONTEXTO = 448 // 2 - 1
MARGEN = 10


def preparar_cuda_windows() -> list[str]:
    """Hace visibles las DLL de CUDA que instala pip.

    En Windows, `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12` deja las
    DLL en site-packages/nvidia/*/bin, que no esta en la ruta de busqueda del
    sistema. Sin esto, los paquetes quedan instalados pero CTranslate2 no los
    encuentra y falla con "cublas64_12.dll is not found": todo parece bien y
    la GPU nunca se usa.
    """
    if platform.system() != "Windows":
        return []

    agregadas = []
    raices = {Path(p) / "nvidia" for p in sys.path if p}
    for raiz in raices:
        if not raiz.is_dir():
            continue
        for paquete in raiz.iterdir():
            carpeta = paquete / "bin"
            if not carpeta.is_dir():
                continue
            try:
                os.add_dll_directory(str(carpeta))
                agregadas.append(str(carpeta))
            except OSError:
                pass
    return agregadas


def _hay_cuda() -> bool:
    try:
        import ctranslate2

        return ctranslate2.get_cuda_device_count() > 0
    except Exception:
        return False


def _elegir_backend(dispositivo: str, tipo_computo: str) -> tuple[str, str]:
    """Resuelve 'auto' al mejor backend disponible en esta maquina."""
    if dispositivo != "auto":
        return dispositivo, ("int8" if tipo_computo == "auto" else tipo_computo)

    # Antes de preguntar por la GPU hay que poder cargar sus librerias.
    agregadas = preparar_cuda_windows()
    if agregadas:
        log.debug("DLL de CUDA encontradas en: %s", ", ".join(agregadas))

    if _hay_cuda():
        return "cuda", ("float16" if tipo_computo == "auto" else tipo_computo)

    # CPU. int8 es varias veces mas rapido que float32 y la diferencia de
    # calidad no se nota en voz de pulpito. En Apple Silicon esto es lo unico
    # que hay: CTranslate2 no usa Metal.
    return "cpu", ("int8" if tipo_computo == "auto" else tipo_computo)


def _funciona(modelo) -> bool:
    """Prueba una transcripcion de verdad antes de confiar en el backend.

    Tener una placa NVIDIA no alcanza: si faltan las librerias de CUDA
    (cuBLAS, cuDNN), el modelo se construye sin quejarse y recien explota al
    transcribir la primera frase. En un culto eso significa que todo parece
    andar y no sale ni una traduccion.
    """
    try:
        silencio = np.zeros(16000, dtype=np.float32)
        list(modelo.transcribe(silencio, beam_size=1, vad_filter=False)[0])
        return True
    except Exception as e:
        log.warning("El backend no funciona (%s).", e)
        return False


class Transcriptor:
    def __init__(self, cfg_stt, contexto_glosario: str = ""):
        self.cfg = cfg_stt
        dispositivo, computo = _elegir_backend(cfg_stt.dispositivo, cfg_stt.tipo_computo)

        self.modelo = WhisperModel(
            cfg_stt.modelo,
            device=dispositivo,
            compute_type=computo,
            cpu_threads=0,  # 0 = que decida CTranslate2 segun la maquina
        )

        # Si la GPU no termina de funcionar, se cae a CPU en vez de fallar en
        # cada frase durante todo el culto.
        if dispositivo == "cuda" and not _funciona(self.modelo):
            log.warning(
                "La GPU no esta lista (faltan las librerias de CUDA). Sigo con la "
                "CPU, que anda bien con el modelo %s. Para usar la placa, instalar "
                "cuBLAS y cuDNN de NVIDIA, o poner stt.dispositivo: \"cpu\" en "
                "config.yaml para no volver a intentarlo.",
                cfg_stt.modelo,
            )
            dispositivo, computo = "cpu", "int8"
            self.modelo = WhisperModel(
                cfg_stt.modelo, device=dispositivo, compute_type=computo, cpu_threads=0
            )

        self.dispositivo, self.computo = dispositivo, computo
        log.info("Whisper %s en %s/%s", cfg_stt.modelo, dispositivo, computo)

        # Sesgamos el reconocimiento hacia el vocabulario de la iglesia:
        # nombres propios, terminos teologicos, libros de la Biblia. Es lo que
        # evita que "Efesios" salga como "efecto" o "Sehon" como "cejon".
        self.hotwords = self._recortar(contexto_glosario)
        self.prompt_inicial = cfg_stt.contexto_inicial or None

    def _recortar(self, terminos) -> str | None:
        """Arma la lista de terminos que entra en el contexto de Whisper.

        Se recorta aca, midiendo con el tokenizador real, en vez de dejar que
        Whisper lo trunque: asi sabemos que quedo afuera y podemos avisarlo. Si
        lo trunca Whisper, lo hace sin decir nada.
        """
        if isinstance(terminos, str):
            terminos = [p.strip() for p in terminos.split(",") if p.strip()]
        if not terminos:
            return None

        from faster_whisper.tokenizer import Tokenizer

        tok = Tokenizer(
            self.modelo.hf_tokenizer,
            self.modelo.model.is_multilingual,
            task="transcribe",
            language=self.cfg.idioma,
        )
        limite = LIMITE_CONTEXTO - MARGEN

        entran = []
        for termino in terminos:
            prueba = ", ".join(entran + [termino])
            if len(tok.encode(" " + prueba)) > limite:
                break
            entran.append(termino)

        if len(entran) < len(terminos):
            log.warning(
                "El glosario tiene %d términos y en Whisper entran %d. "
                "Quedaron afuera: %s. Whisper solo acepta ~%d tokens de "
                "contexto; los términos van por prioridad (nombres primero), "
                "así que conviene acortar la lista de `vocabulario` en "
                "glosario.yaml.",
                len(terminos), len(entran),
                ", ".join(terminos[len(entran):][:8])
                + ("..." if len(terminos) - len(entran) > 8 else ""),
                LIMITE_CONTEXTO,
            )
        return ", ".join(entran)

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
            # hotwords, y no initial_prompt, porque cuando no entra todo
            # Whisper recorta hotwords desde el final (respeta el orden de
            # prioridad) e initial_prompt desde el principio (se comeria
            # justo los nombres propios de la congregacion).
            hotwords=self.hotwords,
            # Ya venimos segmentados por el VAD; filtrar de nuevo solo agrega
            # trabajo y puede comerse audio valido.
            vad_filter=False,
            without_timestamps=True,
        )
        return " ".join(s.text.strip() for s in segmentos).strip()
