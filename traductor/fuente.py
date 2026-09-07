"""Reproducir un archivo por el pipeline como si fuera el microfono en vivo.

Sirve para el ensayo general: se toma la grabacion de un sermon (video o
audio), se la pasa a la velocidad real y la traduccion sale por el transmisor.
Es lo mas parecido a un culto de verdad que se puede hacer un martes.

La alternativa seria un cable de audio virtual (BlackHole, VB-Cable) para que
la aplicacion capture lo que reproduce la computadora. Funciona, pero obliga a
configurar dispositivos virtuales en el sistema y a armar una salida doble
para no quedarse sin escuchar el video. Leer el archivo directo evita todo eso.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

from .audio import FREC_INTERNA, Segmentador

log = logging.getLogger(__name__)

BLOQUE_MS = 50


def es_url(valor: str) -> bool:
    return str(valor).startswith(("http://", "https://"))


def segundos_de(valor: str) -> int:
    """Acepta 1418, 23:38 o 1:23:38."""
    valor = str(valor).strip()
    if valor.isdigit():
        return int(valor)
    partes = [int(p) for p in valor.split(":")]
    segundos = 0
    for p in partes:
        segundos = segundos * 60 + p
    return segundos


def inicio_de_url(url: str) -> int:
    """Lee el ?t= del enlace, que es como YouTube comparte un momento."""
    q = parse_qs(urlparse(url).query)
    for clave in ("t", "start"):
        if clave in q:
            crudo = q[clave][0]
            if crudo.isdigit():
                return int(crudo)
            m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", crudo)
            if m and any(m.groups()):
                h, mi, s = (int(g or 0) for g in m.groups())
                return h * 3600 + mi * 60 + s
    return 0


def resolver_stream(url: str) -> str:
    """Devuelve la URL directa del audio, sin bajar el video entero.

    Asi el ensayo arranca en segundos en vez de esperar la descarga de un
    culto de una hora.
    """
    log.info("Resolviendo el audio del enlace...")
    r = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-f", "bestaudio", "--no-playlist", "-g", url],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        detalle = (r.stderr or "").strip().splitlines()
        raise RuntimeError(
            "No pude leer el enlace: " + (detalle[-1] if detalle else "error desconocido")
        )
    directa = r.stdout.strip().splitlines()
    if not directa:
        raise RuntimeError("El enlace no devolvió ninguna pista de audio.")
    return directa[-1]


class FuenteArchivo:
    """Sustituto de CapturaAudio que lee de un archivo en vez de una placa.

    Expone la misma interfaz, asi que el pipeline no sabe la diferencia.
    """

    def __init__(self, ruta: str | Path, cfg_entrada, cfg_vad, al_emitir,
                 velocidad: float = 1.0, al_terminar=None, desde: int | None = None):
        self.url = es_url(ruta)
        # Con un enlace de YouTube no se baja el video: se reproduce desde la
        # pista de audio directa, asi el ensayo arranca en segundos.
        self.desde = (
            desde if desde is not None else (inicio_de_url(str(ruta)) if self.url else 0)
        )
        if self.url:
            self.ruta = str(ruta)
            self.nombre = "YouTube"
        else:
            self.ruta = Path(ruta)
            if not self.ruta.exists():
                raise FileNotFoundError(f"No encuentro {self.ruta}")
            self.nombre = self.ruta.name
        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "Hace falta ffmpeg para leer archivos de audio o video.\n"
                "  macOS:    brew install ffmpeg\n"
                "  Windows:  winget install Gyan.FFmpeg"
            )

        self.cfg = cfg_entrada
        # El panel muestra esto donde normalmente iria la placa de entrada.
        marca = f" desde {self.desde // 60}:{self.desde % 60:02d}" if self.desde else ""
        self.cfg.dispositivo = f"{self.nombre}{marca}"
        self.velocidad = max(velocidad, 0.1)
        self.al_terminar = al_terminar
        self.segmentador = Segmentador(cfg_vad, al_emitir)

        self.pico = 0.0
        self.pico_crudo = 0.0
        self.terminado = False
        self._hilo: threading.Thread | None = None
        self._corriendo = False
        self._proc: subprocess.Popen | None = None

    def _leer(self) -> None:
        muestras = int(FREC_INTERNA * BLOQUE_MS / 1000)
        crudos = muestras * 2  # int16
        t0 = time.monotonic()
        leidos = 0

        while self._corriendo:
            datos = self._proc.stdout.read(crudos)
            if not datos:
                break
            bloque = np.frombuffer(datos, dtype=np.int16).astype(np.float32) / 32768.0

            self.pico_crudo = max(float(np.abs(bloque).max()), self.pico_crudo * 0.85)
            if self.cfg.ganancia != 1.0:
                bloque = bloque * self.cfg.ganancia
            self.pico = max(float(np.abs(bloque).max()), self.pico * 0.85)
            self.segmentador.alimentar(bloque)

            # Se respeta el reloj: si se leyera a toda velocidad, el pipeline
            # recibiria una hora de sermon en dos minutos y la cola de audio
            # explotaria. Aca queremos reproducir las condiciones del culto.
            leidos += len(bloque)
            objetivo = leidos / (FREC_INTERNA * self.velocidad)
            atraso = objetivo - (time.monotonic() - t0)
            if atraso > 0:
                time.sleep(atraso)

        self.segmentador.finalizar()
        self.terminado = True
        self.pico = self.pico_crudo = 0.0
        if self._corriendo:
            log.info("Se terminó %s.", self.nombre)
            if self.al_terminar:
                self.al_terminar()

    def iniciar(self) -> None:
        origen = resolver_stream(self.ruta) if self.url else str(self.ruta)
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.desde:
            cmd += ["-ss", str(self.desde)]      # antes de -i: seek rapido
        cmd += ["-i", origen, "-vn", "-ac", "1", "-ar", str(FREC_INTERNA),
                "-f", "s16le", "-"]
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self._corriendo = True
        self._hilo = threading.Thread(target=self._leer, daemon=True)
        self._hilo.start()
        log.info("Reproduciendo %s a velocidad %gx", self.cfg.dispositivo, self.velocidad)

    def detener(self) -> None:
        self._corriendo = False
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
        if self._hilo is not None:
            self._hilo.join(timeout=2)

    def cambiar_dispositivo(self, nombre, canal: int = 0) -> None:
        raise RuntimeError(
            "La entrada viene de un archivo. Para volver al micrófono, "
            "reiniciá sin la opción --archivo."
        )
