"""Traduccion de cada frase a todos los idiomas de salida.

Tres caminos, en orden de calidad:

  Claude / Gemini - una sola llamada devuelve TODOS los idiomas a la vez: menos
                    latencia, menos costo y traducciones coherentes entre si.
                    Ve el contexto de las frases anteriores, asi que acierta los
                    pronombres y el hilo del sermon, y corrige de paso los
                    errores tipicos del reconocimiento de voz.

  Whisper         - fallback sin internet. Whisper sabe traducir directo al
                    ingles (task="translate"), asi que si se cae la conexion el
                    culto sigue sin que nadie toque nada. Solo cubre ingles.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as TiempoAgotado

import numpy as np

from .glosario import NOMBRES_IDIOMA, Glosario

log = logging.getLogger(__name__)

SISTEMA = """Sos el interprete simultaneo de un culto de una iglesia cristiana evangelica.
Traducis del español a: {idiomas}.

Reglas:
- Traduci SOLO lo que se dijo. Nunca contestes, opines ni agregues nada.
- Registro hablado, calido y natural: esto lo va a escuchar una persona por
  auricular, no lo va a leer. Frases que se puedan decir en voz alta.
- Te llega la salida de un reconocimiento de voz automatico, asi que puede
  traer palabras mal reconocidas. Corregilas segun el contexto: si dice
  "vesiculo 28" claramente es "versiculo 28". Traduci lo que la persona quiso
  decir.
- Si una frase queda cortada a la mitad, traducila igual tal como esta. La
  frase siguiente la va a continuar.
- Nombres propios de personas quedan sin traducir.
- Las citas biblicas van con la redaccion habitual de las traducciones mas
  usadas en cada idioma, no palabra por palabra desde el español.
- Si la frase no tiene contenido traducible (ruido, una tos, una muletilla
  suelta), devolve string vacio en cada idioma.

{reglas}
{notas}"""


def _bloque_reglas(glosario: Glosario, idiomas: list[str]) -> str:
    reglas = glosario.reglas_para(idiomas)
    if not reglas:
        return ""
    return (
        "Terminos que esta congregacion usa siempre asi (respetalos):\n" + reglas + "\n"
    )


class TraductorLLM:
    """Base comun: prompt, glosario, contexto y parseo.

    Lo unico que cambia entre proveedores es la llamada HTTP, en _llamar().
    """

    def __init__(self, cfg, glosario: Glosario, idiomas: list[str]):
        self.cfg = cfg
        self.idiomas = idiomas
        self.sistema = SISTEMA.format(
            idiomas=", ".join(f"{NOMBRES_IDIOMA.get(i, i)} ({i})" for i in idiomas),
            reglas=_bloque_reglas(glosario, idiomas),
            notas=glosario.notas,
        )
        # El esquema obliga a que vuelva un objeto con exactamente un campo por
        # idioma: no hay que parsear prosa ni lidiar con formatos raros.
        # Cada proveedor acepta un subconjunto distinto de JSON Schema, asi que
        # la base es el minimo comun y cada uno le agrega lo suyo.
        self.esquema = {
            "type": "object",
            "properties": {i: {"type": "string"} for i in idiomas},
            "required": list(idiomas),
        }
        self._historial: deque[tuple[str, dict[str, str]]] = deque(
            maxlen=max(cfg.frases_contexto, 0)
        )
        # El timeout lo controlamos nosotros, no el SDK: Gemini no acepta
        # deadlines HTTP menores a 10s, y esperar 10s por una respuesta en
        # medio de un culto es inaceptable. Si tarda de mas, abandonamos la
        # llamada (termina sola en segundo plano y se descarta) y seguimos
        # con el modo offline.
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="traduccion")

    def _mensaje(self, texto: str) -> str:
        partes = []
        if self._historial:
            previas = "\n".join(f"- {es}" for es, _ in self._historial)
            partes.append(
                f"Frases anteriores del predicador (solo contexto):\n{previas}\n"
            )
        partes.append(f"Frase a traducir ahora:\n{texto}")
        return "\n".join(partes)

    def traducir(self, texto: str) -> dict[str, str]:
        futuro = self._pool.submit(self._llamar, self._mensaje(texto))
        try:
            crudo = futuro.result(timeout=self.cfg.timeout_s)
        except TiempoAgotado:
            futuro.cancel()
            raise TimeoutError(
                f"el proveedor no respondio en {self.cfg.timeout_s:g}s"
            ) from None
        datos = json.loads(crudo)
        traducciones = {i: str(datos.get(i) or "").strip() for i in self.idiomas}
        self._historial.append((texto, traducciones))
        return traducciones

    def _llamar(self, mensaje: str) -> str:
        raise NotImplementedError


class TraductorClaude(TraductorLLM):
    entorno_clave = "ANTHROPIC_API_KEY"

    def __init__(self, cfg, glosario: Glosario, idiomas: list[str]):
        super().__init__(cfg, glosario, idiomas)
        import anthropic

        self.cliente = anthropic.Anthropic(
            timeout=cfg.timeout_s,
            max_retries=0,  # en vivo no hay tiempo de reintentar: al fallback
        )
        # Claude exige additionalProperties para el modo estricto; Gemini lo
        # rechaza como campo desconocido.
        self.esquema = {**self.esquema, "additionalProperties": False}

    def _llamar(self, mensaje: str) -> str:
        respuesta = self.cliente.messages.create(
            model=self.cfg.modelo,
            max_tokens=1000,
            system=[
                {
                    "type": "text",
                    "text": self.sistema,
                    # El prompt de sistema no cambia en todo el culto.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": mensaje}],
            output_config={"format": {"type": "json_schema", "schema": self.esquema}},
        )
        return next(b.text for b in respuesta.content if b.type == "text")


class TraductorGemini(TraductorLLM):
    entorno_clave = "GEMINI_API_KEY"

    def __init__(self, cfg, glosario: Glosario, idiomas: list[str]):
        super().__init__(cfg, glosario, idiomas)
        from google import genai
        from google.genai import types

        self._types = types
        self.cliente = genai.Client(
            api_key=os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        self._config = types.GenerateContentConfig(
            system_instruction=self.sistema,
            response_mime_type="application/json",
            response_schema=self.esquema,
            # Traducir no es una tarea creativa: queremos el mismo termino
            # siempre, no variedad.
            temperature=0.0,
            max_output_tokens=1000,
            # Sin razonamiento previo: en vivo cada decima cuenta y esta tarea
            # no lo necesita.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
            # No usamos herramientas: sin esto el SDK avisa por cada llamada.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
            # Piso que impone la API. El limite util es el nuestro, mas corto:
            # ver el timeout del lado del cliente en TraductorLLM.traducir().
            http_options=types.HttpOptions(
                timeout=max(10_000, int(cfg.timeout_s * 1000))
            ),
        )

    def _llamar(self, mensaje: str) -> str:
        respuesta = self.cliente.models.generate_content(
            model=self.cfg.modelo, contents=mensaje, config=self._config
        )
        texto = respuesta.text
        if not texto:
            raise RuntimeError(f"Gemini devolvio vacio (motivo: {respuesta.candidates})")
        return texto


PROVEEDORES = {"claude": TraductorClaude, "gemini": TraductorGemini}


class TraductorWhisper:
    """Fallback offline. Whisper traduce directo al ingles desde el audio."""

    idiomas_soportados = {"en"}

    def __init__(self, transcriptor):
        self.transcriptor = transcriptor

    def traducir(self, audio: np.ndarray) -> dict[str, str]:
        segmentos, _ = self.transcriptor.modelo.transcribe(
            audio,
            task="translate",
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        return {"en": " ".join(s.text.strip() for s in segmentos).strip()}


class Traductor:
    """Elige el camino y degrada solo si el proveedor no responde."""

    def __init__(self, cfg, glosario: Glosario, idiomas: list[str], transcriptor):
        self.idiomas = idiomas
        self.proveedor = cfg.proveedor
        self.principal = None
        self.fallback = TraductorWhisper(transcriptor)
        self.usando_fallback = False
        self.fallos_seguidos = 0
        # Cuando el proveedor falla por algo que no se arregla solo (sin
        # credito, clave vencida), reintentar en cada frase cuesta ~1s de
        # latencia por una llamada que ya sabemos que va a fallar. Esperamos
        # cada vez mas entre intentos, hasta un minuto.
        self._reintentar_desde = 0.0
        self.ultimo_error = ""

        if cfg.proveedor in PROVEEDORES:
            clase = PROVEEDORES[cfg.proveedor]
            if not os.environ.get(clase.entorno_clave):
                log.warning(
                    "Falta %s. Arranco en modo offline; poner la clave en .env "
                    "y reiniciar para usar %s.",
                    clase.entorno_clave, cfg.proveedor,
                )
            else:
                try:
                    self.principal = clase(cfg, glosario, idiomas)
                except Exception as e:
                    log.warning("No pude iniciar %s (%s). Uso el modo offline.",
                                cfg.proveedor, e)
        elif cfg.proveedor != "local":
            raise ValueError(
                f"Proveedor de traduccion desconocido: {cfg.proveedor!r}. "
                f"Opciones: {sorted(PROVEEDORES)} o 'local'."
            )

        sin_cobertura = set(idiomas) - self.fallback.idiomas_soportados
        if self.principal is None and sin_cobertura:
            raise RuntimeError(
                f"Sin proveedor de traduccion, el modo offline solo cubre ingles. "
                f"No hay como traducir a: {sorted(sin_cobertura)}. "
                f"Cargar la clave en .env o sacar esos idiomas de config.yaml."
            )

    def traducir(self, texto: str, audio: np.ndarray) -> dict[str, str]:
        if self.principal is not None and time.monotonic() >= self._reintentar_desde:
            try:
                traducciones = self.principal.traducir(texto)
                if self.usando_fallback:
                    log.info("%s volvio a responder.", self.proveedor)
                    self.usando_fallback = False
                self.fallos_seguidos = 0
                self.ultimo_error = ""
                return traducciones
            except Exception as e:
                self.fallos_seguidos += 1
                self.ultimo_error = str(e)
                espera = min(5 * 2 ** (self.fallos_seguidos - 1), 60)
                self._reintentar_desde = time.monotonic() + espera
                if not self.usando_fallback:
                    log.warning(
                        "%s fallo (%s). Sigo con el modo offline; el culto no se "
                        "corta. Reintento en %ds.",
                        self.proveedor, e, espera,
                    )
                    self.usando_fallback = True

        traducciones = self.fallback.traducir(audio)
        # Los idiomas que el fallback no cubre quedan en blanco: preferimos
        # silencio en ese canal antes que mandarle español a alguien que
        # esta esperando otro idioma.
        return {i: traducciones.get(i, "") for i in self.idiomas}
