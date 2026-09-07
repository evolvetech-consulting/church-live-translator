"""Captura de audio desde la placa y segmentacion en frases.

El segmentador es la pieza que mas define la latencia percibida de todo el
sistema. En vez de cortar el audio cada N segundos (lo que parte palabras al
medio y obliga a Whisper a adivinar), corta en las pausas naturales del que
habla. Whisper recibe frases completas, transcribe mejor y responde antes.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
import soxr

# Whisper trabaja a 16 kHz; Silero VAD tambien. Todo el pipeline interno
# usa esta frecuencia y float32 mono.
FREC_INTERNA = 16000

# Silero procesa de a 512 muestras (32 ms a 16 kHz).
MUESTRAS_FRAME = 512
MS_POR_FRAME = 1000 * MUESTRAS_FRAME // FREC_INTERNA  # 32

# El modelo ONNX de faster-whisper reinicia su estado interno en cada llamada,
# asi que lo alimentamos de a bloques en vez de frame por frame: con 8 frames
# (256 ms) la red ya viene "caliente" y el costo de arranque es despreciable.
FRAMES_POR_BLOQUE = 8


@dataclass
class Frase:
    """Un tramo de voz continua, listo para transcribir."""

    audio: np.ndarray  # float32 mono a 16 kHz
    t_inicio: float  # segundos desde que arranco la captura
    seq: int

    @property
    def duracion(self) -> float:
        return len(self.audio) / FREC_INTERNA


def buscar_dispositivo(nombre: str | None, entrada: bool) -> int | None:
    """Resuelve un nombre parcial de dispositivo a su indice de PortAudio."""
    if not nombre:
        return None

    clave = "max_input_channels" if entrada else "max_output_channels"
    candidatos = [
        (i, d)
        for i, d in enumerate(sd.query_devices())
        if d[clave] > 0 and nombre.lower() in d["name"].lower()
    ]
    if not candidatos:
        tipo = "entrada" if entrada else "salida"
        disponibles = [
            d["name"] for d in sd.query_devices() if d[clave] > 0
        ]
        raise RuntimeError(
            f"No encuentro un dispositivo de {tipo} que contenga {nombre!r}.\n"
            f"Disponibles: {disponibles}\n"
            f"Correr `python -m tools.dispositivos` para ver la lista completa."
        )
    return candidatos[0][0]


class DetectorVoz:
    """Decide, frame a frame, si hay voz. Dos motores intercambiables."""

    def __init__(self, motor: str, umbral: float):
        self.motor = motor
        self.umbral = umbral
        self._modelo = None
        # Solo para el motor de energia: piso de ruido estimado en vivo.
        self._piso_ruido = 1e-2
        self._frames_vistos = 0
        self._frames_calibracion = 500 // MS_POR_FRAME

        if motor == "silero":
            from faster_whisper.vad import get_vad_model

            self._modelo = get_vad_model()
        elif motor != "energia":
            raise ValueError(f"Motor de VAD desconocido: {motor!r}")

    def probabilidades(self, bloque: np.ndarray) -> np.ndarray:
        """Devuelve una probabilidad de voz por cada frame de 512 muestras."""
        n_frames = len(bloque) // MUESTRAS_FRAME
        if n_frames == 0:
            return np.empty(0, dtype=np.float32)

        recorte = bloque[: n_frames * MUESTRAS_FRAME]

        if self.motor == "silero":
            return self._modelo(recorte).reshape(-1).astype(np.float32)

        # Motor de energia: RMS de cada frame contra un piso de ruido estimado
        # por seguimiento de minimos. Baja rapido cuando aparece un frame mas
        # silencioso y sube muy despacio, asi se adapta si el ruido de sala
        # cambia sin que la voz levante su propio umbral. No se puede adaptar
        # "solo durante el silencio": si el piso arranca mal, nunca detecta
        # silencio y no se corrige jamas.
        frames = recorte.reshape(n_frames, MUESTRAS_FRAME)
        rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1)) + 1e-9

        probs = np.empty(n_frames, dtype=np.float32)
        for i, energia in enumerate(rms):
            if energia < self._piso_ruido:
                self._piso_ruido = 0.7 * self._piso_ruido + 0.3 * energia
            else:
                self._piso_ruido *= 1.0008
            self._piso_ruido = max(self._piso_ruido, 1e-7)

            # Mapea 3x..12x sobre el piso de ruido al rango 0..1.
            relacion = energia / self._piso_ruido
            probs[i] = float(np.clip((relacion - 3.0) / 9.0, 0.0, 1.0))

            # Durante el primer medio segundo el piso todavia se esta
            # calibrando: no afirmamos que haya voz.
            if self._frames_vistos < self._frames_calibracion:
                self._frames_vistos += 1
                probs[i] = 0.0
        return probs


class Segmentador:
    """Maquina de estados que junta frames de voz en frases completas."""

    def __init__(self, cfg_vad, al_emitir):
        self.cfg = cfg_vad
        self.al_emitir = al_emitir
        self.detector = DetectorVoz(cfg_vad.motor, cfg_vad.umbral)

        self._frames_pre_roll = max(1, cfg_vad.pre_roll_ms // MS_POR_FRAME)
        self._frames_silencio_fin = max(1, cfg_vad.silencio_fin_ms // MS_POR_FRAME)
        self._frames_silencio_largo = max(1, cfg_vad.silencio_largo_ms // MS_POR_FRAME)
        self._min_frames_frase = int(cfg_vad.min_frase_s * 1000 // MS_POR_FRAME)
        self._max_frames = int(cfg_vad.max_frase_s * 1000 // MS_POR_FRAME)
        self._min_frames_voz = max(1, cfg_vad.min_voz_ms // MS_POR_FRAME)

        self._pre_roll: deque[np.ndarray] = deque(maxlen=self._frames_pre_roll)
        self._en_voz = False
        self._frames: list[np.ndarray] = []
        self._probs: list[float] = []
        self._silencio_seguido = 0
        # Muestras totales consumidas desde que arranco la captura, y muestra
        # en la que empieza la frase que estamos armando. Se llevan por
        # separado porque un corte forzado arrastra audio a la frase
        # siguiente, y ahi las dos dejan de moverse juntas.
        self._muestras_vistas = 0
        self._inicio_muestras = 0
        self._seq = 0
        self._resto = np.empty(0, dtype=np.float32)

    def alimentar(self, audio: np.ndarray) -> None:
        """Procesa audio 16 kHz float32; llama a al_emitir por cada frase."""
        datos = np.concatenate([self._resto, audio]) if self._resto.size else audio

        largo_bloque = MUESTRAS_FRAME * FRAMES_POR_BLOQUE
        pos = 0
        while pos + largo_bloque <= len(datos):
            bloque = datos[pos : pos + largo_bloque]
            pos += largo_bloque
            probs = self.detector.probabilidades(bloque)
            for i, p in enumerate(probs):
                frame = bloque[i * MUESTRAS_FRAME : (i + 1) * MUESTRAS_FRAME]
                self._procesar_frame(frame, float(p))

        self._resto = datos[pos:].copy()

    def _procesar_frame(self, frame: np.ndarray, prob: float) -> None:
        self._muestras_vistas += MUESTRAS_FRAME
        hay_voz = prob >= self.cfg.umbral

        if not self._en_voz:
            self._pre_roll.append(frame)
            if hay_voz:
                # Arranca una frase: sembramos con el pre-roll para no
                # cortar la primera silaba.
                self._en_voz = True
                self._frames = list(self._pre_roll)
                self._probs = [0.0] * (len(self._pre_roll) - 1) + [prob]
                self._pre_roll.clear()
                self._silencio_seguido = 0
                self._inicio_muestras = (
                    self._muestras_vistas - len(self._frames) * MUESTRAS_FRAME
                )
            return

        self._frames.append(frame)
        self._probs.append(prob)
        self._silencio_seguido = 0 if hay_voz else self._silencio_seguido + 1

        # Dos umbrales, porque un predicador hace pausas cortas EN MEDIO de la
        # oracion: "...que nos han presentado con sus voces / la grandeza / de
        # Jehova". Con un solo umbral eso sale como tres frases sueltas y la
        # traduccion de cada pedazo, sin el resto, queda sin sentido.
        #
        #   silencio_fin_ms   -> cierra solo si ya juntamos una frase con
        #                        cuerpo suficiente (min_frase_s)
        #   silencio_largo_ms -> cierra siempre: aca el orador termino de verdad
        largo = self._silencio_seguido >= self._frames_silencio_largo
        con_cuerpo = (
            self._silencio_seguido >= self._frames_silencio_fin
            and len(self._frames) >= self._min_frames_frase
        )
        if largo or con_cuerpo:
            self._cerrar()
        elif len(self._frames) >= self._max_frames:
            self._cerrar(forzado=True)

    def _frames_con_voz(self, probs: list[float]) -> int:
        return sum(1 for p in probs if p >= self.cfg.umbral)

    def _cerrar(self, forzado: bool = False) -> None:
        frames, probs = self._frames, self._probs
        corte = len(frames)

        if forzado:
            # Nadie hizo una pausa lo bastante larga. Cortamos igual, pero en
            # el punto mas silencioso del ultimo tramo, para partir entre
            # palabras y no en el medio de una. Lo que sobra queda como
            # arranque de la frase siguiente, asi no se pierde audio.
            ventana = min(len(probs), int(2000 // MS_POR_FRAME))
            desde = max(len(probs) - ventana, self._min_frames_voz)
            # Buscamos la ultima pausa real (frame por debajo del umbral) en el
            # tramo final. Tomar simplemente el minimo no sirve: si el tramo es
            # todo voz, el minimo cae en el primer frame y terminariamos
            # picando la frase en pedacitos del mismo largo una y otra vez.
            for i in range(len(probs) - 1, desde - 1, -1):
                if probs[i] < self.cfg.umbral:
                    corte = i
                    break

        emitidos, emitidos_probs = frames[:corte], probs[:corte]
        arrastre, arrastre_probs = frames[corte:], probs[corte:]
        inicio = self._inicio_muestras

        self._reiniciar()

        if emitidos and self._frames_con_voz(emitidos_probs) >= self._min_frames_voz:
            self._seq += 1
            self.al_emitir(
                Frase(
                    audio=np.concatenate(emitidos),
                    t_inicio=inicio / FREC_INTERNA,
                    seq=self._seq,
                )
            )

        if arrastre:
            # Continuamos la frase con el audio sobrante, sin volver a pasarlo
            # por el detector: ya tenemos sus probabilidades.
            self._en_voz = True
            self._frames = arrastre
            self._probs = arrastre_probs
            self._inicio_muestras = inicio + corte * MUESTRAS_FRAME
            self._silencio_seguido = 0
            for p in reversed(arrastre_probs):
                if p >= self.cfg.umbral:
                    break
                self._silencio_seguido += 1

    def _reiniciar(self) -> None:
        self._en_voz = False
        self._frames = []
        self._probs = []
        self._silencio_seguido = 0
        self._pre_roll.clear()

    def finalizar(self) -> None:
        """Emite lo que haya quedado a medias al cerrar el culto."""
        if self._en_voz:
            self._cerrar()


class CapturaAudio:
    """Abre la placa, remuestrea a 16 kHz y alimenta al segmentador."""

    def __init__(self, cfg_entrada, cfg_vad, al_emitir):
        self.cfg = cfg_entrada
        self.dispositivo = buscar_dispositivo(cfg_entrada.dispositivo, entrada=True)
        self.segmentador = Segmentador(cfg_vad, al_emitir)

        self._cola: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=200)
        self._stream: sd.InputStream | None = None
        self._hilo: threading.Thread | None = None
        self._corriendo = False
        # Un remuestreador con estado: mantiene continuidad entre bloques y
        # evita los clics que aparecen si se remuestrea cada bloque aislado.
        self._resampler = (
            soxr.ResampleStream(cfg_entrada.frecuencia, FREC_INTERNA, 1, dtype="float32")
            if cfg_entrada.frecuencia != FREC_INTERNA
            else None
        )
        # Nivel de pico reciente, para el medidor en pantalla.
        self.pico = 0.0

    def _callback(self, indata, frames, tiempo, estado):
        # Corre en el hilo de audio de PortAudio: nada bloqueante aca adentro.
        canal = min(self.cfg.canal, indata.shape[1] - 1)
        mono = indata[:, canal].copy()
        try:
            self._cola.put_nowait(mono)
        except queue.Full:
            pass  # preferimos perder un bloque antes que trabar la placa

    def _procesar(self):
        while self._corriendo:
            try:
                bloque = self._cola.get(timeout=0.5)
            except queue.Empty:
                continue
            if bloque is None:
                break

            self.pico = max(float(np.abs(bloque).max()), self.pico * 0.85)

            if self._resampler is not None:
                bloque = self._resampler.resample_chunk(bloque)
            if bloque.size:
                self.segmentador.alimentar(bloque)

    def iniciar(self):
        canales = 1
        if self.cfg.canal > 0:
            info = sd.query_devices(self.dispositivo, "input")
            canales = max(self.cfg.canal + 1, 1)
            if canales > info["max_input_channels"]:
                raise RuntimeError(
                    f"Pediste el canal {self.cfg.canal} pero el dispositivo solo "
                    f"tiene {info['max_input_channels']} canal(es) de entrada."
                )

        self._corriendo = True
        self._hilo = threading.Thread(target=self._procesar, daemon=True)
        self._hilo.start()

        self._stream = sd.InputStream(
            device=self.dispositivo,
            channels=canales,
            samplerate=self.cfg.frecuencia,
            dtype="float32",
            blocksize=int(self.cfg.frecuencia * 0.05),  # bloques de 50 ms
            callback=self._callback,
        )
        self._stream.start()

    def detener(self):
        self._corriendo = False
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._cola.put(None)
        if self._hilo is not None:
            self._hilo.join(timeout=2)
        self.segmentador.finalizar()
