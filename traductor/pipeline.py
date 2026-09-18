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
                 desde=None, ruta_config=None):
        self.cfg = cfg
        self.ruta_config = ruta_config
        self.archivo = archivo
        self.al_actualizar = al_actualizar or (lambda ev: None)
        # Todos los idiomas configurados, prendidos o no: hacen falta para
        # armar los motores, las colas y los hilos de sintesis de cada salida,
        # asi un idioma apagado se puede volver a prender sin reiniciar nada.
        self.idiomas = cfg.idiomas
        # Solo los que van a pedirse al traductor. No todos los cultos
        # necesitan los cuatro idiomas, y apagar uno tiene que ahorrar de
        # verdad: si no se le pide, no hay tokens de mas ni audio de mas.
        self.idiomas_activos = [s.idioma for s in cfg.salidas if s.activo]
        if not self.idiomas_activos:
            log.warning(
                "No hay ningún idioma activo en config.yaml. Activo todos: sin "
                "al menos uno, no hay nada para traducir."
            )
            for s in cfg.salidas:
                s.activo = True
            self.idiomas_activos = list(self.idiomas)

        # Resolvemos las placas primero: si config.yaml tiene un nombre mal,
        # el operador se entera en un segundo y no despues de esperar medio
        # minuto a que carguen los modelos.
        self._verificar_dispositivos()

        self.glosario = Glosario.cargar()
        log.info("Cargando Whisper (%s)...", cfg.stt.modelo)
        self.transcriptor = Transcriptor(cfg.stt, self.glosario.terminos_para_whisper())
        self.traductor = Traductor(
            cfg.traduccion, self.glosario, self.idiomas_activos, self.transcriptor
        )

        log.info("Cargando voces...")
        # bajar_si_falta=True tambien aca: si config.yaml pide una voz que
        # todavia no esta en voces/ (por ejemplo, se agrego un idioma nuevo
        # a mano y no se lo probo antes desde el panel), se baja sola en vez
        # de romper el arranque con un FileNotFoundError.
        self.motores = {
            s.idioma: MotorTTS(
                s.voz, s.velocidad, expresividad=s.expresividad, bajar_si_falta=True
            )
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

    @staticmethod
    def _canal_valido(indice: int | None, canal: int, entrada: bool) -> bool:
        """El canal 0 siempre existe si el dispositivo tiene al menos una
        entrada/salida; uno mayor depende de cuantos canales tenga de verdad
        (un mixer de 8 canales vs. el micrófono de 1-2 canales de una laptop
        cualquiera, por ejemplo)."""
        import sounddevice as sd

        if canal <= 0:
            return True
        try:
            info = sd.query_devices(indice, "input" if entrada else "output")
        except Exception:
            return True  # no se pudo confirmar; no bloqueamos el arranque por esto
        clave = "max_input_channels" if entrada else "max_output_channels"
        return canal < info[clave]

    def _verificar_dispositivos(self) -> None:
        import sounddevice as sd

        # No estricto: un config.yaml pensado para el mixer de OTRA iglesia
        # (otro nombre de placa, u otra cantidad de canales) no tiene por que
        # impedir que la aplicacion abra. Se cae al dispositivo por defecto de
        # esta PC (y al canal 0, el unico que existe seguro) y se avisa en el
        # panel (ver servidor.py/_estado) -- desde ahi ya se puede elegir el
        # dispositivo y canal reales sin tocar ningun archivo.
        avisos = []
        if not self.archivo:
            nombre = self.cfg.entrada.dispositivo
            idx = buscar_dispositivo(nombre, entrada=True, estricto=False)
            if idx is None and nombre:
                avisos.append(
                    f"No encontré la entrada {nombre!r}: uso la de esta PC "
                    f"por defecto. Elegí la correcta en Entrada."
                )
                self.cfg.entrada.dispositivo = None
            if not self._canal_valido(idx, self.cfg.entrada.canal, entrada=True):
                avisos.append(
                    f"El canal {self.cfg.entrada.canal} de entrada no existe "
                    f"en este dispositivo: uso el canal 0. Elegí el correcto "
                    f"en Entrada."
                )
                self.cfg.entrada.canal = 0
        for s in self.cfg.salidas:
            idx = buscar_dispositivo(s.dispositivo, entrada=False, estricto=False)
            if idx is None and s.dispositivo:
                avisos.append(
                    f"No encontré la salida de {s.nombre} ({s.dispositivo!r}): "
                    f"uso la de esta PC por defecto. Elegí la correcta en "
                    f"Canales de salida."
                )
                s.dispositivo = None
            if not self._canal_valido(idx, s.canal, entrada=False):
                avisos.append(
                    f"El canal {s.canal} de {s.nombre} no existe en este "
                    f"dispositivo: uso el canal 0. Elegí el correcto en "
                    f"Canales de salida."
                )
                s.canal = 0
        self.aviso_dispositivo = " ".join(avisos)

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
            indice = buscar_dispositivo(s.dispositivo, entrada=False, estricto=False)
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
            # Solo mira los idiomas activos: uno apagado no recibe audio nuevo,
            # asi que su cola no deberia influir en si hay que condensar.
            atraso = max(
                (self.ruteador.pendiente_s(i) for i in self.idiomas_activos), default=0.0
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
        "pt": "Este é o canal de português. Teste: um, dois, três. "
              "Se você ouve com clareza, o nível está correto.",
        "fr": "Ceci est le canal francais. Test, un, deux, trois. "
              "Si vous entendez clairement, le niveau est correct.",
        "uk": "Це український канал. Перевірка: один, два, три. "
              "Якщо ви чуєте чітко, рівень правильний.",
        "it": "Questo e il canale italiano. Prova, uno, due, tre.",
        "de": "Dies ist der deutsche Kanal. Test, eins, zwei, drei.",
    }

    def cambiar_entrada(self, dispositivo: str | None, canal: int = 0) -> None:
        self.captura.cambiar_dispositivo(dispositivo, canal)
        self._entrada_previa = dispositivo
        # Se eligio a mano desde el panel: el aviso de "no encontre tu placa"
        # del arranque ya no aplica (sea esto o no lo que faltaba resolver).
        self.aviso_dispositivo = ""
        log.info("Entrada cambiada a %s (canal %d)", dispositivo or "por defecto", canal)

    def cambiar_salida(self, idioma: str, dispositivo: str | None, canal: int,
                       ganancia: float | None = None) -> None:
        self.ruteador.reconfigurar(idioma, dispositivo, canal, ganancia)
        self.aviso_dispositivo = ""
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
            self.ruteador.silenciar(idioma)
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

    def guardar_config(self) -> str:
        """Deja en el archivo lo que se ajusto desde el panel.

        Sin esto, cada prueba obliga a rehacer a mano la voz, el dispositivo y
        los niveles: el sistema se configura probando, y lo que se encontro
        probando tiene que sobrevivir al reinicio.
        """
        if not self.ruta_config:
            raise RuntimeError("No sé en qué archivo guardar.")
        destino = self.cfg.guardar(self.ruta_config)
        log.info("Configuración guardada en %s", destino)
        return str(destino)

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
            # silenciar(), no descartar_sobre(): pausa es una orden explicita
            # del operador, y tiene que cortar lo que este sonando en ese
            # instante (incluida una frase de "Probar este canal" en curso),
            # no esperar a que termine la oracion como hace el descarte
            # automatico por atraso.
            for idioma in self.idiomas:
                self.ruteador.silenciar(idioma)
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

    def _colision_canal(self, salida):
        """Otra salida ACTIVA en el mismo (dispositivo, canal), si hay una.

        Un idioma apagado no ocupa su canal de verdad, asi que no cuenta como
        colision. Lo usan tanto `activar_idioma()` (para no prender un idioma
        sobre un canal que ya esta sonando) como `probar_canal()` (para no
        mandar una frase de prueba a pisarse con audio real en vivo).
        """
        return next(
            (s for s in self.cfg.salidas
             if s.idioma != salida.idioma and s.activo
             and s.dispositivo == salida.dispositivo and s.canal == salida.canal),
            None,
        )

    def activar_idioma(self, idioma: str, activo: bool) -> list[str]:
        """Prende o apaga un idioma sin reiniciar nada.

        No todos los cultos necesitan traducirse a los cuatro idiomas. Apagar
        uno lo saca de verdad del pedido al traductor: no se gastan tokens en
        el, y como ademas no llega texto a su cola de sintesis, tampoco se
        gasta CPU sintetizando algo que no se va a usar. La placa y la voz
        quedan configuradas igual, listas para cuando se lo vuelva a prender.

        Reconstruye el traductor porque el esquema JSON y el prompt de sistema
        se arman una vez con la lista de idiomas: no hay forma de sacarle uno
        a mitad de camino sin rehacerlo. Es una operacion de configuracion,
        no del camino caliente de cada frase, asi que el costo de reconstruir
        no importa.
        """
        try:
            salida = next(s for s in self.cfg.salidas if s.idioma == idioma)
        except StopIteration:
            raise KeyError(f"No hay una salida configurada para {idioma!r}") from None

        if activo:
            # Un idioma apagado no ocupa su canal de verdad (ver
            # Ruteador.reconfigurar), asi que otro pudo haberse mudado ahi
            # mientras tanto. Antes de prenderlo nos fijamos que no choque con
            # uno que ya este sonando, o los dos terminarian pisandose en el
            # mismo canal.
            choque = self._colision_canal(salida)
            if choque is not None:
                raise ValueError(
                    f"No puedo activar {salida.nombre}: su canal ya lo usa "
                    f"{choque.nombre}, que esta activo. Cambiale el canal en "
                    f"'ajustes' antes de prenderlo."
                )

        activos = {i for i in self.idiomas_activos if i != idioma}
        if activo:
            activos.add(idioma)
        if not activos:
            raise ValueError(
                "Tiene que quedar al menos un idioma activo: no se puede "
                "apagar el último."
            )

        salida.activo = activo
        # Se preserva el orden de config.yaml, no el de insercion del set.
        self.idiomas_activos = [i for i in self.idiomas if i in activos]
        self.traductor = Traductor(
            self.cfg.traduccion, self.glosario, self.idiomas_activos, self.transcriptor
        )
        log.info(
            "%s: %s -> activos ahora %s",
            idioma, "activado" if activo else "apagado", self.idiomas_activos,
        )
        return self.idiomas_activos

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
        salida = next(s for s in self.cfg.salidas if s.idioma == idioma)
        choque = self._colision_canal(salida)
        if choque is not None:
            raise ValueError(
                f"No puedo probar {salida.nombre}: comparte canal con "
                f"{choque.nombre}, que esta activo. La prueba se pisaria con "
                f"su audio en vivo."
            )
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
