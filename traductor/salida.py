"""Ruteo del audio traducido a cada transmisor Retekess.

Cada idioma sale por su propio canal fisico. La clave: los dos canales de una
salida estereo son independientes, asi que una sola UM2 alimenta DOS
transmisores (izquierdo = idioma 1, derecho = idioma 2) sin comprar nada.
Para un tercer idioma se agrega un dongle USB y se lo nombra en config.yaml.

Un unico stream por dispositivo fisico escribe todos sus canales: abrir dos
streams sobre la misma placa haria que se pisen.
"""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
import sounddevice as sd
import soxr

from .audio import buscar_dispositivo


class BufferIdioma:
    """Cola de audio ya sintetizado, lista para que la consuma la placa."""

    def __init__(self, ganancia: float = 1.0):
        self.ganancia = ganancia
        self._trozos: deque[np.ndarray] = deque()
        self._muestras = 0
        self._lock = threading.Lock()

    def agregar(self, audio: np.ndarray) -> None:
        if audio.size == 0:
            return
        with self._lock:
            self._trozos.append(audio)
            self._muestras += len(audio)

    def tomar(self, n: int) -> np.ndarray:
        """Saca n muestras; completa con silencio si no alcanza."""
        salida = np.zeros(n, dtype=np.float32)
        escrito = 0
        with self._lock:
            while escrito < n and self._trozos:
                trozo = self._trozos[0]
                falta = n - escrito
                if len(trozo) <= falta:
                    salida[escrito : escrito + len(trozo)] = trozo
                    escrito += len(trozo)
                    self._trozos.popleft()
                    self._muestras -= len(trozo)
                else:
                    salida[escrito:] = trozo[:falta]
                    self._trozos[0] = trozo[falta:]
                    self._muestras -= falta
                    escrito = n
        return salida * self.ganancia

    def muestras_pendientes(self) -> int:
        with self._lock:
            return self._muestras

    def vaciar_hasta(self, muestras_max: int) -> int:
        """Descarta lo mas viejo si la cola se fue de mano. Devuelve lo tirado."""
        tirado = 0
        with self._lock:
            while self._muestras > muestras_max and self._trozos:
                trozo = self._trozos.popleft()
                self._muestras -= len(trozo)
                tirado += len(trozo)
        return tirado


class Ruteador:
    """Abre las placas de salida y mantiene un canal por idioma."""

    def __init__(self, salidas, frecuencia: int = 48000):
        self.frecuencia = frecuencia
        self.buffers: dict[str, BufferIdioma] = {
            s.idioma: BufferIdioma(s.ganancia) for s in salidas
        }
        self.salidas = {s.idioma: s for s in salidas}

        # Agrupamos por dispositivo fisico: un stream por placa.
        self._por_dispositivo: dict[int | None, list] = {}
        for s in salidas:
            indice = buscar_dispositivo(s.dispositivo, entrada=False)
            self._por_dispositivo.setdefault(indice, []).append(s)

        self._streams: list[sd.OutputStream] = []
        # Nivel de pico por idioma, para el medidor en pantalla.
        self.picos: dict[str, float] = {s.idioma: 0.0 for s in salidas}

    def _hacer_callback(self, grupo):
        canales = max(s.canal for s in grupo) + 1

        def callback(outdata, frames, tiempo, estado):
            outdata.fill(0.0)
            for s in grupo:
                muestras = self.buffers[s.idioma].tomar(frames)
                # Recortamos por las dudas: la entrada del T130 es de microfono
                # y satura facil. Mejor limitar aca que distorsionar en el aire.
                np.clip(muestras, -1.0, 1.0, out=muestras)
                outdata[:, s.canal] = muestras
                pico = float(np.abs(muestras).max()) if frames else 0.0
                self.picos[s.idioma] = max(pico, self.picos[s.idioma] * 0.8)

        return callback, canales

    def iniciar(self) -> None:
        for indice, grupo in self._por_dispositivo.items():
            callback, canales = self._hacer_callback(grupo)
            info = sd.query_devices(indice, "output")
            if canales > info["max_output_channels"]:
                nombres = ", ".join(f"{s.nombre}(canal {s.canal})" for s in grupo)
                raise RuntimeError(
                    f"{info['name']!r} tiene {info['max_output_channels']} canal(es) "
                    f"de salida, pero config.yaml pide {canales} para: {nombres}."
                )
            stream = sd.OutputStream(
                device=indice,
                channels=canales,
                samplerate=self.frecuencia,
                dtype="float32",
                blocksize=int(self.frecuencia * 0.02),  # 20 ms
                callback=callback,
            )
            stream.start()
            self._streams.append(stream)

    def detener(self) -> None:
        for stream in self._streams:
            stream.stop()
            stream.close()
        self._streams.clear()

    def reproducir(self, idioma: str, audio: np.ndarray, frecuencia_origen: int) -> None:
        if frecuencia_origen != self.frecuencia:
            audio = soxr.resample(audio, frecuencia_origen, self.frecuencia)
        self.buffers[idioma].agregar(audio.astype(np.float32))

    def pendiente_s(self, idioma: str) -> float:
        return self.buffers[idioma].muestras_pendientes() / self.frecuencia

    def descartar_sobre(self, idioma: str, segundos: float) -> float:
        tirado = self.buffers[idioma].vaciar_hasta(int(segundos * self.frecuencia))
        return tirado / self.frecuencia
