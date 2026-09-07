"""Orquestador: conecta captura -> VAD -> STT -> traduccion -> TTS -> salida.

Cada etapa corre en su propio hilo y se comunican por colas, asi que mientras
se traduce la frase N, Whisper ya esta transcribiendo la N+1. Sin ese
solapamiento las latencias se sumarian en vez de solaparse.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .audio import CapturaAudio, Frase, buscar_dispositivo
from .glosario import Glosario
from .salida import Ruteador
from .stt import Transcriptor
from .traduccion import Traductor
from .tts import MotorTTS

log = logging.getLogger(__name__)


@dataclass
class Evento:
    """Una frase recorriendo el pipeline, para mostrar y para registrar."""

    seq: int
    t_inicio: float
    duracion_audio: float
    texto: str = ""
    traducciones: dict[str, str] = field(default_factory=dict)
    ms_stt: float = 0.0
    ms_traduccion: float = 0.0
    ms_tts: dict[str, float] = field(default_factory=dict)
    offline: bool = False

    @property
    def latencia_total_ms(self) -> float:
        return self.ms_stt + self.ms_traduccion + max(self.ms_tts.values(), default=0.0)


class Pipeline:
    def __init__(self, cfg, al_actualizar=None):
        self.cfg = cfg
        self.al_actualizar = al_actualizar or (lambda ev: None)
        self.idiomas = cfg.idiomas

        # Resolvemos las placas primero: si config.yaml tiene un nombre mal,
        # el operador se entera en un segundo y no despues de esperar medio
        # minuto a que carguen los modelos.
        self._verificar_dispositivos()

        self.glosario = Glosario.cargar()
        log.info("Cargando Whisper (%s)...", cfg.stt.modelo)
        self.transcriptor = Transcriptor(cfg.stt, self.glosario.terminos_para_whisper())
        self.traductor = Traductor(
            cfg.traduccion, self.glosario, self.idiomas, self.transcriptor
        )

        log.info("Cargando voces...")
        self.motores = {
            s.idioma: MotorTTS(s.voz, s.velocidad) for s in cfg.salidas
        }
        self.ruteador = Ruteador(cfg.salidas)
        self.captura = CapturaAudio(cfg.entrada, cfg.vad, self._al_detectar_frase)

        self._cola_stt: queue.Queue[Frase | None] = queue.Queue()
        self._cola_traduccion: queue.Queue[tuple[Evento, np.ndarray] | None] = queue.Queue()
        self._colas_tts: dict[str, queue.Queue] = {
            i: queue.Queue() for i in self.idiomas
        }
        self._hilos: list[threading.Thread] = []
        self._corriendo = False

        self.eventos: list[Evento] = []
        self.descartado_s: dict[str, float] = {i: 0.0 for i in self.idiomas}
        self._registro = None

    def _verificar_dispositivos(self) -> None:
        import sounddevice as sd

        buscar_dispositivo(self.cfg.entrada.dispositivo, entrada=True)
        for s in self.cfg.salidas:
            buscar_dispositivo(s.dispositivo, entrada=False)

        # La PC de la iglesia suele hacer tambien otra cosa: pasar los himnos,
        # videos, la letra en pantalla. Todo eso sale por el dispositivo por
        # defecto de Windows. Si la traduccion apunta al mismo, dos cosas
        # malas pasan a la vez: la congregacion escucha la traduccion al
        # ingles por los parlantes de la sala, y quien tiene el receptor
        # escucha los himnos y los sonidos del sistema.
        try:
            defecto = sd.default.device[1]
        except Exception:
            return
        for s in self.cfg.salidas:
            indice = buscar_dispositivo(s.dispositivo, entrada=False)
            # indice None significa literalmente "la salida por defecto", que
            # es el caso peligroso, no uno distinto.
            if indice is not None and indice != defecto:
                continue
            nombre = sd.query_devices(defecto)["name"]
            log.warning(
                "La salida de %s va al dispositivo POR DEFECTO del sistema (%s). "
                "Si esta computadora reproduce otra cosa (himnos, videos, "
                "sonidos de Windows), va a salir por el mismo lugar: la sala va "
                "a escuchar la traduccion y el receptor va a escuchar los "
                "himnos. Elegi una placa dedicada para el transmisor.",
                s.nombre, nombre,
            )

    # ---------------- etapas ----------------

    def _al_detectar_frase(self, frase: Frase) -> None:
        self._cola_stt.put(frase)

    def _hilo_stt(self) -> None:
        while self._corriendo:
            frase = self._cola_stt.get()
            if frase is None:
                break
            t0 = time.perf_counter()
            try:
                texto = self.transcriptor.transcribir(frase.audio)
            except Exception:
                log.exception("Fallo la transcripcion de la frase #%d", frase.seq)
                continue
            if not texto:
                continue

            ev = Evento(
                seq=frase.seq,
                t_inicio=frase.t_inicio,
                duracion_audio=frase.duracion,
                texto=texto,
                ms_stt=(time.perf_counter() - t0) * 1000,
            )
            self.eventos.append(ev)
            self.al_actualizar(ev)
            self._cola_traduccion.put((ev, frase.audio))

    def _hilo_traduccion(self) -> None:
        while self._corriendo:
            item = self._cola_traduccion.get()
            if item is None:
                break
            ev, audio = item
            t0 = time.perf_counter()
            try:
                ev.traducciones = self.traductor.traducir(ev.texto, audio)
            except Exception:
                log.exception("Fallo la traduccion de la frase #%d", ev.seq)
                continue
            ev.ms_traduccion = (time.perf_counter() - t0) * 1000
            ev.offline = self.traductor.usando_fallback
            self.al_actualizar(ev)
            self._anotar(ev)

            for idioma, texto in ev.traducciones.items():
                if texto:
                    self._colas_tts[idioma].put((ev, texto))

    def _velocidad_para(self, idioma: str) -> float:
        """Acelera el habla cuando la cola crece, para no quedar cada vez mas atras.

        Sin esto, un predicador que no hace pausas deja la traduccion 40
        segundos atras a la media hora: cada frase tarda en decirse mas de lo
        que tarda en llegar la siguiente.
        """
        lat = self.cfg.latencia
        pendiente = self.ruteador.pendiente_s(idioma)
        if pendiente <= lat.umbral_aceleracion_s:
            return 1.0
        if pendiente >= lat.umbral_maximo_s:
            return lat.velocidad_maxima
        avance = (pendiente - lat.umbral_aceleracion_s) / (
            lat.umbral_maximo_s - lat.umbral_aceleracion_s
        )
        return 1.0 + avance * (lat.velocidad_maxima - 1.0)

    def _hilo_tts(self, idioma: str) -> None:
        motor = self.motores[idioma]
        cola = self._colas_tts[idioma]
        while self._corriendo:
            item = cola.get()
            if item is None:
                break
            ev, texto = item

            # Si la cola se fue de mano, preferimos estar sincronizados a
            # decirlo todo: tiramos lo mas viejo, que ya perdio sentido.
            tirado = self.ruteador.descartar_sobre(
                idioma, self.cfg.latencia.descartar_sobre_s
            )
            if tirado:
                self.descartado_s[idioma] += tirado
                log.warning(
                    "[%s] cola muy larga: descarto %.1fs para volver a sincronizar",
                    idioma, tirado,
                )

            t0 = time.perf_counter()
            try:
                audio = motor.sintetizar(texto, self._velocidad_para(idioma))
            except Exception:
                log.exception("Fallo la sintesis de %s para la frase #%d", idioma, ev.seq)
                continue
            ev.ms_tts[idioma] = (time.perf_counter() - t0) * 1000
            self.ruteador.reproducir(idioma, audio, motor.frecuencia)
            self.al_actualizar(ev)

    # ---------------- ajustes en caliente ----------------

    # Frase de prueba por idioma. Sirve para ajustar el nivel hacia el
    # transmisor sin tener que hablar: se dispara desde el panel y se escucha
    # en el receptor. Dice de que canal se trata, asi con varios transmisores
    # se sabe cual es cual.
    PRUEBAS = {
        "en": "This is the English channel. Testing, one, two, three. "
              "If you can hear this clearly, the level is correct.",
        "pt": "Este e o canal de portugues. Teste, um, dois, tres. "
              "Se voce ouve com clareza, o nivel esta correto.",
        "fr": "Ceci est le canal francais. Test, un, deux, trois.",
        "it": "Questo e il canale italiano. Prova, uno, due, tre.",
        "de": "Dies ist der deutsche Kanal. Test, eins, zwei, drei.",
    }

    def cambiar_entrada(self, dispositivo: str | None, canal: int = 0) -> None:
        self.captura.cambiar_dispositivo(dispositivo, canal)
        log.info("Entrada cambiada a %s (canal %d)", dispositivo or "por defecto", canal)

    def cambiar_salida(self, idioma: str, dispositivo: str | None, canal: int,
                       ganancia: float | None = None) -> None:
        self.ruteador.reconfigurar(idioma, dispositivo, canal, ganancia)
        log.info("Salida de %s a %s (canal %d)", idioma,
                 dispositivo or "por defecto", canal)

    def probar_canal(self, idioma: str) -> float:
        """Manda una frase de prueba al canal. Devuelve su duracion."""
        if idioma not in self.motores:
            raise KeyError(f"No hay salida configurada para {idioma!r}")
        motor = self.motores[idioma]
        texto = self.PRUEBAS.get(idioma) or f"Test channel {idioma}. One, two, three."
        audio = motor.sintetizar(texto)
        self.ruteador.reproducir(idioma, audio, motor.frecuencia)
        return len(audio) / motor.frecuencia

    # ---------------- registro ----------------

    def _abrir_registro(self) -> None:
        carpeta = Path(self.cfg.registro_carpeta)
        carpeta.mkdir(parents=True, exist_ok=True)
        nombre = datetime.now().strftime("culto-%Y-%m-%d-%H%M.jsonl")
        self._registro = (carpeta / nombre).open("w", encoding="utf-8")
        log.info("Registrando en %s", carpeta / nombre)

    def _anotar(self, ev: Evento) -> None:
        if self._registro is None:
            return
        self._registro.write(
            json.dumps(
                {
                    "seq": ev.seq,
                    "t": round(ev.t_inicio, 2),
                    "es": ev.texto,
                    **{f"trad_{k}": v for k, v in ev.traducciones.items()},
                    "ms_stt": round(ev.ms_stt),
                    "ms_trad": round(ev.ms_traduccion),
                    "offline": ev.offline,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        self._registro.flush()

    # ---------------- ciclo de vida ----------------

    def iniciar(self) -> None:
        self._abrir_registro()
        self._corriendo = True

        self._hilos = [
            threading.Thread(target=self._hilo_stt, name="stt", daemon=True),
            threading.Thread(target=self._hilo_traduccion, name="traduccion", daemon=True),
        ]
        self._hilos += [
            threading.Thread(target=self._hilo_tts, args=(i,), name=f"tts-{i}", daemon=True)
            for i in self.idiomas
        ]
        for h in self._hilos:
            h.start()

        self.ruteador.iniciar()
        self.captura.iniciar()
        log.info("Escuchando.")

    def detener(self) -> None:
        log.info("Cerrando...")
        self.captura.detener()
        self._corriendo = False
        self._cola_stt.put(None)
        self._cola_traduccion.put(None)
        for c in self._colas_tts.values():
            c.put(None)
        for h in self._hilos:
            h.join(timeout=3)
        self.ruteador.detener()
        if self._registro is not None:
            self._registro.close()
            self._registro = None
