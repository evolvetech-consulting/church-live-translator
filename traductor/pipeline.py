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
    def __init__(self, cfg, al_actualizar=None, archivo=None, velocidad=1.0,
                 desde=None):
        self.cfg = cfg
        self.archivo = archivo
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
            s.idioma: MotorTTS(s.voz, s.velocidad, expresividad=s.expresividad)
            for s in cfg.salidas
        }
        self.ruteador = Ruteador(cfg.salidas)
        # Se guarda para poder volver al microfono despues de reproducir un
        # archivo: FuenteArchivo pisa cfg.entrada.dispositivo con su nombre.
        self._entrada_previa = cfg.entrada.dispositivo
        if archivo:
            from .fuente import FuenteArchivo

            self.captura = FuenteArchivo(
                archivo, cfg.entrada, cfg.vad, self._al_detectar_frase,
                velocidad, desde=desde,
            )
        else:
            self.captura = CapturaAudio(
                cfg.entrada, cfg.vad, self._al_detectar_frase
            )

        self._cola_stt: queue.Queue[Frase | None] = queue.Queue()
        self._cola_traduccion: queue.Queue[tuple[Evento, np.ndarray] | None] = queue.Queue()
        self._colas_tts: dict[str, queue.Queue] = {
            i: queue.Queue() for i in self.idiomas
        }
        self._hilos: list[threading.Thread] = []
        self._corriendo = False

        # Pausar no es apagar: la captura sigue abierta (asi el medidor de
        # entrada sigue sirviendo para ajustar niveles) pero no se procesa
        # nada. Sirve para los himnos, donde traducir no aporta y solo gasta.
        self.pausado = False
        # Se incrementa en cada sesion nueva. Una frase que ya estaba dentro
        # de Whisper cuando el operador reinicio no se puede interrumpir, pero
        # si se puede descartar al volver: pertenece al culto anterior.
        self._generacion = 0
        self.eventos: list[Evento] = []
        # Ultimo error de una etapa, para que el panel lo muestre. Un fallo que
        # se repite en cada frase se ve desde afuera igual que "no entra
        # audio", y son dos problemas completamente distintos.
        self.ultimo_fallo = ""
        self.fallos_stt = 0
        self.descartado_s: dict[str, float] = {i: 0.0 for i in self.idiomas}
        self._registro = None

    def _verificar_dispositivos(self) -> None:
        import sounddevice as sd

        if not self.archivo:
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
        if self.pausado:
            return
        self._cola_stt.put(frase)

    def _hilo_stt(self) -> None:
        while self._corriendo:
            frase = self._cola_stt.get()
            if frase is None:
                break
            generacion = self._generacion
            t0 = time.perf_counter()
            try:
                texto = self.transcriptor.transcribir(frase.audio)
            except Exception as e:
                self.fallos_stt += 1
                self.ultimo_fallo = f"Whisper: {e}"
                if self.fallos_stt in (1, 5, 25):
                    log.exception(
                        "Fallo la transcripcion (%d veces seguidas)", self.fallos_stt
                    )
                continue
            self.fallos_stt = 0
            if self.ultimo_fallo.startswith("Whisper"):
                self.ultimo_fallo = ""
            if not texto or generacion != self._generacion:
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
            # Cuanto atras vamos, para que el traductor condense si hace falta.
            atraso = max(
                (self.ruteador.pendiente_s(i) for i in self.idiomas), default=0.0
            )
            t0 = time.perf_counter()
            try:
                ev.traducciones = self.traductor.traducir(ev.texto, audio, atraso)
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
        cola = self._colas_tts[idioma]
        while self._corriendo:
            item = cola.get()
            if item is None:
                break
            ev, texto = item

            # Si la cola se fue de mano, preferimos estar sincronizados a
            # decirlo todo: lo viejo ya perdio sentido porque el predicador
            # esta hablando de otra cosa.
            #
            # Primero se descartan frases que ni siquiera se sintetizaron:
            # gastar CPU en generar audio que vamos a tirar solo empeora el
            # atraso. Recien despues se recorta lo ya sintetizado, y eso nunca
            # toca la frase que esta sonando.
            if self.ruteador.pendiente_s(idioma) > self.cfg.latencia.descartar_sobre_s:
                saltadas = 0
                while cola.qsize() > 1:
                    try:
                        siguiente = cola.get_nowait()
                    except queue.Empty:
                        break
                    if siguiente is None:
                        cola.put(None)
                        break
                    ev, texto = siguiente
                    saltadas += 1
                if saltadas:
                    log.warning(
                        "[%s] muy atrasado: saltee %d frase(s) sin sintetizar",
                        idioma, saltadas,
                    )

            tirado = self.ruteador.descartar_sobre(
                idioma, self.cfg.latencia.descartar_sobre_s
            )
            if tirado:
                self.descartado_s[idioma] += tirado
                log.warning(
                    "[%s] cola muy larga: descarto %.1fs de frases en espera",
                    idioma, tirado,
                )

            # El motor se busca en cada frase y no una vez al arrancar el
            # hilo: cambiar_voz() reemplaza la entrada del diccionario, y con
            # una referencia guardada el canal seguiria hablando con la voz
            # vieja para siempre.
            motor = self.motores[idioma]
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
        self._entrada_previa = dispositivo
        log.info("Entrada cambiada a %s (canal %d)", dispositivo or "por defecto", canal)

    def cambiar_salida(self, idioma: str, dispositivo: str | None, canal: int,
                       ganancia: float | None = None) -> None:
        self.ruteador.reconfigurar(idioma, dispositivo, canal, ganancia)
        log.info("Salida de %s a %s (canal %d)", idioma,
                 dispositivo or "por defecto", canal)

    def nueva_sesion(self) -> None:
        """Deja todo limpio para empezar de cero, como un culto nuevo.

        Pausar no alcanza: la transcripcion anterior sigue en pantalla y en el
        registro. El sabado que viene tiene que arrancar en blanco.
        """
        self.pausar(True)

        # Volver al microfono si se estaba reproduciendo un archivo.
        if hasattr(self.captura, "url"):
            try:
                self.cambiar_fuente(None)
            except Exception:
                log.exception("No pude volver al micrófono")

        self._generacion += 1
        self._vaciar_colas()
        for idioma in self.idiomas:
            self.ruteador.descartar_sobre(idioma, 0.0)
            self.descartado_s[idioma] = 0.0
        self.eventos.clear()

        # Cada sesion tiene su propio archivo de registro, con su fecha y hora.
        if self._registro is not None:
            self._registro.close()
            self._registro = None
        self._abrir_registro()

        # Se corta tambien el hilo de contexto: las frases del culto anterior
        # no tienen nada que ver con el que arranca.
        if getattr(self.traductor, "principal", None) is not None:
            self.traductor.principal._historial.clear()

        self.pausar(False)
        log.info("Sesión nueva: todo limpio.")

    def _vaciar_colas(self) -> None:
        for cola in (self._cola_stt, self._cola_traduccion, *self._colas_tts.values()):
            while not cola.empty():
                try:
                    cola.get_nowait()
                except queue.Empty:
                    break

    def cambiar_fuente(self, origen: str | None = None, desde=None,
                       velocidad: float = 1.0) -> str:
        """Cambia de donde viene el audio, sin cortar el resto del pipeline.

        `origen` None vuelve al microfono; si no, es la ruta de un archivo o
        un enlace de YouTube. Los modelos siguen cargados: solo se reemplaza
        la fuente, que expone la misma interfaz en los dos casos.
        """
        anterior = self.captura
        anterior.detener()

        try:
            if origen:
                from .fuente import FuenteArchivo, segundos_de

                nueva = FuenteArchivo(
                    origen, self.cfg.entrada, self.cfg.vad, self._al_detectar_frase,
                    velocidad, desde=segundos_de(desde) if desde else None,
                )
            else:
                self.cfg.entrada.dispositivo = self._entrada_previa
                nueva = CapturaAudio(
                    self.cfg.entrada, self.cfg.vad, self._al_detectar_frase
                )
            nueva.iniciar()
        except Exception:
            # Si la fuente nueva no sirve, se vuelve al microfono en vez de
            # quedarse sin entrada en medio de un culto.
            self.cfg.entrada.dispositivo = self._entrada_previa
            self.captura = CapturaAudio(
                self.cfg.entrada, self.cfg.vad, self._al_detectar_frase
            )
            self.captura.iniciar()
            raise

        self.captura = nueva
        log.info("Fuente de audio: %s", self.cfg.entrada.dispositivo or "micrófono")
        return self.cfg.entrada.dispositivo or ""

    def pausar(self, pausado: bool = True) -> bool:
        """Corta o reanuda la traduccion sin tocar el resto.

        Al pausar se descarta lo que quedo en el camino: si alguien pausa
        porque arranca la alabanza, que siga saliendo la frase anterior por el
        auricular es peor que el silencio.
        """
        self.pausado = pausado
        # Si la fuente es un archivo, se frena tambien la reproduccion: si no,
        # el sermon sigue corriendo y esos minutos se pierden.
        if hasattr(self.captura, "pausar"):
            self.captura.pausar(pausado)
        if pausado:
            # Se vacian todas las etapas, no solo la de sintesis: una frase ya
            # segmentada esperando en la cola de Whisper igual terminaria
            # sonando despues de que el operador puso pausa.
            self._vaciar_colas()
            for idioma in self.idiomas:
                self.ruteador.descartar_sobre(idioma, 0.0)
        log.info("Traduccion %s", "pausada" if pausado else "reanudada")
        return self.pausado

    def cambiar_voz(self, idioma: str, voz: str) -> str:
        """Cambia la voz de un idioma. La baja si hace falta (tarda un poco)."""
        from .tts import MotorTTS

        if idioma not in self.motores:
            raise KeyError(f"No hay salida configurada para {idioma!r}")
        salida = next(s for s in self.cfg.salidas if s.idioma == idioma)
        # Se arma el motor nuevo antes de soltar el viejo: si la voz no existe
        # o falla la descarga, el canal sigue funcionando con la de antes.
        motor = MotorTTS(voz, salida.velocidad, bajar_si_falta=True,
                         expresividad=salida.expresividad)
        self.motores[idioma] = motor
        salida.voz = voz
        log.info("Voz de %s cambiada a %s", idioma, voz)
        return voz

    def cambiar_nivel(self, destino: str, valor: float) -> float:
        """Ajusta la ganancia de la entrada o de un canal de salida.

        Se aplica al instante y sin cortar nada: es un multiplicador, no
        reabre ningun stream. `destino` es "entrada" o el codigo de idioma.
        """
        # Hasta x12: un direct out de mixer puede llegar 20 dB por debajo de
        # lo que entrega un microfono conectado directo.
        valor = max(0.0, min(float(valor), 12.0))
        if destino == "entrada":
            self.cfg.entrada.ganancia = valor
        elif destino in self.ruteador.buffers:
            self.ruteador.salidas[destino].ganancia = valor
            self.ruteador.buffers[destino].ganancia = valor
        else:
            raise KeyError(f"No hay un canal llamado {destino!r}")
        return valor

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
        base = datetime.now().strftime("culto-%Y-%m-%d-%H%M")
        # Dos sesiones dentro del mismo minuto no pueden compartir archivo: se
        # abre en modo escritura y la segunda borraria la primera.
        destino = carpeta / f"{base}.jsonl"
        n = 2
        while destino.exists():
            destino = carpeta / f"{base}-{n}.jsonl"
            n += 1
        self._registro = destino.open("w", encoding="utf-8")
        log.info("Registrando en %s", destino)

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
