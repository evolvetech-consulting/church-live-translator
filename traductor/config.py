"""Carga y validacion de config.yaml."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Entrada:
    dispositivo: str | None = None
    frecuencia: int = 48000
    canal: int = 0
    ganancia: float = 1.0


@dataclass
class VAD:
    motor: str = "silero"
    umbral: float = 0.5
    silencio_fin_ms: int = 700
    silencio_largo_ms: int = 1600
    min_frase_s: float = 3.0
    min_voz_ms: int = 400
    max_frase_s: float = 14.0
    pre_roll_ms: int = 300


@dataclass
class STT:
    modelo: str = "small"
    idioma: str = "es"
    dispositivo: str = "auto"
    tipo_computo: str = "auto"
    contexto_inicial: str = ""


@dataclass
class Traduccion:
    proveedor: str = "gemini"
    modelo: str = "gemini-2.5-flash"
    frases_contexto: int = 4
    timeout_s: float = 6.0


@dataclass
class Salida:
    idioma: str
    nombre: str
    voz: str
    dispositivo: str | None = None
    canal: int = 0
    velocidad: float = 1.0
    ganancia: float = 0.7
    expresividad: float | None = None
    # No todos los cultos necesitan los cuatro idiomas. Un idioma inactivo
    # sigue configurado (placa, voz, canal) pero no se le pide traduccion al
    # LLM ni se sintetiza audio para el: apagarlo ahorra de verdad, no es
    # cosmetico. Se puede prender y apagar en caliente desde el panel.
    activo: bool = True


@dataclass
class Latencia:
    umbral_aceleracion_s: float = 4.0
    umbral_maximo_s: float = 12.0
    velocidad_maxima: float = 1.25
    descartar_sobre_s: float = 25.0


@dataclass
class Web:
    activo: bool = True
    puerto: int = 8080


# Lo que se puede cambiar desde el panel y hay que poder guardar. El resto de
# config.yaml se edita a mano y nunca se toca al guardar.
CAMPOS_ENTRADA = ("dispositivo", "canal", "ganancia")
CAMPOS_SALIDA = ("dispositivo", "canal", "voz", "velocidad", "ganancia", "activo")


@dataclass
class Config:
    entrada: Entrada = field(default_factory=Entrada)
    vad: VAD = field(default_factory=VAD)
    stt: STT = field(default_factory=STT)
    traduccion: Traduccion = field(default_factory=Traduccion)
    salidas: list[Salida] = field(default_factory=list)
    latencia: Latencia = field(default_factory=Latencia)
    web: Web = field(default_factory=Web)
    registro_carpeta: str = "registros"

    @property
    def idiomas(self) -> list[str]:
        return [s.idioma for s in self.salidas]

    def guardar(self, ruta: str | Path = "config.yaml") -> Path:
        """Escribe al archivo lo que se cambio desde el panel.

        Se reescribe solo el valor de cada campo, no el archivo entero: estos
        YAML estan llenos de comentarios que explican para que sirve cada cosa
        y por que tiene el valor que tiene, y volcarlos de nuevo desde cero los
        perderia todos.

        Tampoco se tocan las secciones que no se editan desde el panel (vad,
        stt, traduccion, latencia): si alguien las ajusto a mano mientras el
        programa corria, su version es la buena.
        """
        from ruamel.yaml import YAML

        ruta = Path(ruta)
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        datos = yaml_rt.load(ruta.read_text(encoding="utf-8"))

        entrada = datos.setdefault("entrada", {})
        for campo in CAMPOS_ENTRADA:
            entrada[campo] = getattr(self.entrada, campo)

        por_idioma = {s["idioma"]: s for s in datos.get("salidas") or []}
        for salida in self.salidas:
            destino = por_idioma.get(salida.idioma)
            if destino is None:
                continue
            for campo in CAMPOS_SALIDA:
                destino[campo] = getattr(salida, campo)

        # Se escribe a un archivo temporal y recien despues se reemplaza: si
        # algo falla a mitad de camino, el operador se queda con su
        # configuracion anterior y no con un archivo cortado por la mitad.
        temporal = ruta.with_suffix(ruta.suffix + ".tmp")
        with temporal.open("w", encoding="utf-8") as f:
            yaml_rt.dump(datos, f)
        temporal.replace(ruta)
        return ruta


def _seccion(datos: dict, clave: str, cls):
    """Construye una dataclass ignorando claves desconocidas del YAML."""
    crudo = datos.get(clave) or {}
    validas = {f.name for f in cls.__dataclass_fields__.values()}
    desconocidas = set(crudo) - validas
    if desconocidas:
        raise ValueError(
            f"config.yaml: claves no reconocidas en '{clave}': {sorted(desconocidas)}"
        )
    return cls(**crudo)


def cargar(ruta: str | Path = "config.yaml") -> Config:
    ruta = Path(ruta)
    if not ruta.exists():
        raise FileNotFoundError(f"No encuentro {ruta}")

    datos = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}

    salidas_crudas = datos.get("salidas") or []
    if not salidas_crudas:
        raise ValueError("config.yaml: hay que definir al menos una salida de idioma")

    salidas = []
    for i, s in enumerate(salidas_crudas):
        faltan = {"idioma", "nombre", "voz"} - set(s)
        if faltan:
            raise ValueError(f"config.yaml: salida #{i + 1} sin {sorted(faltan)}")
        salidas.append(Salida(**s))

    # Dos idiomas ACTIVOS no pueden compartir el mismo canal fisico: se
    # pisarian el audio. Uno apagado no cuenta: no le manda nada a nadie, asi
    # que puede compartir canal con el que este activo sin problema. Es lo
    # mismo que valida Ruteador.reconfigurar() en caliente, y tiene que ser
    # igual: si no, "Guardar" desde el panel podria dejar un config.yaml que
    # esta misma funcion despues rechaza al reiniciar.
    ocupados: dict[tuple[str | None, int], str] = {}
    for s in salidas:
        if not s.activo:
            continue
        llave = (s.dispositivo, s.canal)
        if llave in ocupados:
            raise ValueError(
                f"config.yaml: '{s.nombre}' y '{ocupados[llave]}' apuntan al mismo "
                f"canal {s.canal} de {s.dispositivo!r}, y los dos estan activos. "
                f"Cada idioma activo necesita su propio canal fisico."
            )
        ocupados[llave] = s.nombre

    return Config(
        entrada=_seccion(datos, "entrada", Entrada),
        vad=_seccion(datos, "vad", VAD),
        stt=_seccion(datos, "stt", STT),
        traduccion=_seccion(datos, "traduccion", Traduccion),
        salidas=salidas,
        latencia=_seccion(datos, "latencia", Latencia),
        web=_seccion(datos, "web", Web),
        registro_carpeta=(datos.get("registro") or {}).get("carpeta", "registros"),
    )
