"""Sintesis de voz local con Piper.

Corre entero en esta maquina: sin costo por culto y sin depender de internet.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import logging

import numpy as np
from piper import PiperVoice, SynthesisConfig

log = logging.getLogger(__name__)

CARPETA_VOCES = Path("voces")

# espeak-ng guarda la ruta de sus datos en un buffer interno de tamaño fijo.
# Si la ruta es mas larga, la trunca en silencio y termina buscando los datos
# en la ruta donde se compilo el paquete, que en esta maquina no existe. El
# sintomo es un error sobre un directorio /Users/runner/work/... y ninguna voz.
#
# Pasa de verdad: basta con instalar la aplicacion en algo como
# C:\Users\Iglesia\OneDrive\Documentos\Programas\church-live-translator\
# para superar el limite. Por eso, si la ruta es larga, copiamos los datos de
# espeak a un lugar corto una sola vez.
LIMITE_RUTA_ESPEAK = 130


def datos_espeak() -> Path:
    """Devuelve una ruta a espeak-ng-data que espeak pueda leer."""
    import piper

    origen = Path(piper.__file__).parent / "espeak-ng-data"
    if len(str(origen)) <= LIMITE_RUTA_ESPEAK:
        return origen

    destino = Path(tempfile.gettempdir()) / "piper-espeak-ng-data"
    if not (destino / "phontab").exists():
        shutil.copytree(origen, destino, dirs_exist_ok=True)
    return destino


def _usar_certificados_de_certifi() -> None:
    """Hace que urlopen confie en el CA bundle de certifi, no en el del SO.

    piper.download_voices baja los modelos con urllib puro, sin pasar un
    contexto SSL propio. En Windows eso depende del almacen de certificados
    raiz de Windows, que en una maquina que no se actualiza seguido puede no
    tener la CA de turno todavia (Windows los va sumando bajo demanda via
    Windows Update). El sintoma es CERTIFICATE_VERIFY_FAILED: unable to get
    local issuer certificate, aun con internet andando bien. certifi trae su
    propio bundle empaquetado, asi que no depende del estado del SO.
    """
    import ssl

    import certifi

    ssl._create_default_https_context = lambda: ssl.create_default_context(
        cafile=certifi.where()
    )


def descargar_voz(voz: str, carpeta: Path = CARPETA_VOCES) -> None:
    """Baja una voz de Piper. Son ~60-120 MB cada una.

    Se llama a la funcion de piper directamente (no por subproceso): lanzar
    `sys.executable -m piper.download_voices` asume que sys.executable es un
    interprete de Python que entiende "-m". Empaquetado con PyInstaller,
    sys.executable es el propio .exe de la aplicacion, que no sabe que hacer
    con esos argumentos y, en el peor caso, termina relanzando la aplicacion
    entera en bucle en vez de bajar el archivo.
    """
    from piper.download_voices import download_voice

    _usar_certificados_de_certifi()
    carpeta.mkdir(parents=True, exist_ok=True)
    try:
        download_voice(voz, carpeta)
    except Exception as e:
        raise RuntimeError(f"No pude bajar la voz {voz!r}: {e}") from e


def ruta_voz(voz: str, carpeta: Path = CARPETA_VOCES, bajar: bool = False) -> Path:
    modelo = carpeta / f"{voz}.onnx"
    if not modelo.exists():
        if bajar:
            print(f"Falta la voz {voz!r}, la bajo (una sola vez)...")
            descargar_voz(voz, carpeta)
        else:
            raise FileNotFoundError(
                f"Falta la voz {voz!r} en {carpeta}/.\n"
                f"Bajarla con:\n"
                f"  .venv/bin/python -m piper.download_voices {voz} --data-dir {carpeta}"
            )
    return modelo


class MotorTTS:
    """Convierte texto en audio. Una instancia por idioma de salida."""

    def __init__(self, voz: str, velocidad_base: float = 1.0,
                 carpeta: Path = CARPETA_VOCES, bajar_si_falta: bool = False,
                 expresividad: float | None = None):
        self.voz = voz
        self.velocidad_base = velocidad_base
        # Variabilidad de tono y de duracion de los fonemas. None deja los
        # valores propios de la voz, que es lo recomendado.
        #
        # Medido: subirlo alarga la frase hasta un 25% y mueve poco el tono.
        # Piper es un motor rapido y plano por diseño; lo que de verdad cambia
        # la entonacion es la puntuacion del texto, no estos numeros.
        self.expresividad = expresividad
        self._voice = PiperVoice.load(
            ruta_voz(voz, carpeta, bajar_si_falta),
            espeak_data_dir=datos_espeak(),
        )
        self.frecuencia = self._voice.config.sample_rate

        # Algunas voces (la ucraniana, por ejemplo) no pasan por espeak: son
        # de tipo TEXT, cada caracter se mapea directo a un id, y su tabla
        # trae solo minusculas. Sin bajar el texto, cada mayuscula se descarta
        # sin avisar y las frases pierden su primera letra.
        #
        # Hay que mirar el tipo y no la tabla: la de una voz espeak contiene
        # simbolos IPA, que tampoco tienen mayusculas, y daria un falso
        # positivo. En espeak la capitalizacion si importa (siglas, enfasis).
        tipo = getattr(self._voice.config, "phoneme_type", None)
        self._bajar_texto = str(getattr(tipo, "name", tipo)).upper() == "TEXT"
        if self._bajar_texto:
            log.debug("La voz %s no tiene mayúsculas en su tabla: se bajan.", voz)

    def sintetizar(self, texto: str, velocidad: float = 1.0) -> np.ndarray:
        """Devuelve float32 mono a self.frecuencia."""
        texto = texto.strip()
        if not texto:
            return np.empty(0, dtype=np.float32)
        if self._bajar_texto:
            texto = texto.lower()

        # Piper usa length_scale: cuanto MAS grande, mas lento. Es el
        # inverso de "velocidad".
        factor = max(self.velocidad_base * velocidad, 0.1)
        cfg = SynthesisConfig(length_scale=1.0 / factor, normalize_audio=True)
        if self.expresividad is not None:
            e = max(0.0, min(self.expresividad, 1.5))
            cfg.noise_scale = 0.667 * e
            cfg.noise_w_scale = 0.8 * e

        trozos = [
            trozo.audio_float_array
            for trozo in self._voice.synthesize(texto, syn_config=cfg)
        ]
        if not trozos:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(trozos).astype(np.float32)
