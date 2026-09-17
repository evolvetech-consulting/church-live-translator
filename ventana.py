"""Traductor del culto, como una aplicacion de escritorio comun.

Para quien esta de turno en sonido y no tiene por que saber usar una
terminal: doble clic en iniciar.bat (Windows) o iniciar.command (macOS) y se
abre una ventana con el panel, sin consola, sin pestaña de navegador, sin
barra de direcciones. Se cierra como cualquier programa, con la X de la
ventana.

Es exactamente el mismo panel web de siempre (traductor/servidor.py) — esto
es solo una ventana nativa que lo muestra. Todo lo que hoy vive en
main.py (el panel de la terminal, --archivo para ensayos, --verboso, etc.)
sigue disponible corriendo `python main.py` a mano: esta ventana es el
camino para el uso de todos los dias, no un reemplazo para quien configura o
depura el sistema.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import webview

# Corriendo como script, RAIZ es la carpeta de este archivo. Empaquetado con
# PyInstaller, __file__ apunta adentro del bundle interno (_internal/...), no
# a la carpeta donde vive el .exe -- ahi hay que usar sys.executable en
# cambio. Y se fija el directorio de trabajo a RAIZ antes de cualquier otra
# cosa: config.yaml, voces/ y .env se buscan con ruta relativa en todo el
# proyecto, y sin esto dependerian de desde donde Windows haya lanzado el
# acceso directo, en vez de desde donde esta instalada la aplicacion.
if getattr(sys, "frozen", False):
    RAIZ = Path(sys.executable).resolve().parent
else:
    RAIZ = Path(__file__).resolve().parent
os.chdir(RAIZ)

from traductor import config
from traductor.entorno import cargar_env, resolver_pin
from traductor.pipeline import Pipeline
from traductor.servidor import ServidorWeb

ICONO = RAIZ / "traductor" / "paginas" / "estaticos" / "icono.ico"

log = logging.getLogger(__name__)


def pagina_error(titulo: str, detalle: str) -> str:
    """HTML autonomo para cuando algo falla antes de tener panel que mostrar.

    Sin terminal a la vista, esto es lo unico que va a ver quien esta de
    turno: tiene que decir en criollo que revisar, no un traceback.
    """
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>
  body{{margin:0;background:#0d1017;color:#e8ecf2;height:100vh;display:flex;
       align-items:center;justify-content:center;
       font:15px/1.6 -apple-system,system-ui,sans-serif}}
  .caja{{max-width:480px;padding:32px;text-align:center}}
  h1{{font-size:17px;margin:0 0 14px;color:#f0564a}}
  p{{color:#9aa4b6;margin:0 0 8px}}
  code{{background:#1c2129;padding:2px 7px;border-radius:5px;color:#e8ecf2}}
</style></head><body>
  <div class="caja">
    <h1>No pude arrancar</h1>
    <p>{titulo}</p>
    <p><code>{detalle}</code></p>
    <p style="margin-top:20px">Avisale a quien configuró el sistema.</p>
  </div>
</body></html>"""


def pagina_falta_pin() -> str:
    return pagina_error(
        "Falta un PIN fijo para poder mostrar el panel sin una terminal.",
        "Agregar PANEL_PIN=un-numero-de-6-digitos al archivo .env",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Sin terminal a la vista en el uso normal (se abre con pythonw.exe), asi
    # que este log solo sirve si alguien corre `python ventana.py` a mano
    # para depurar. No cuesta nada dejarlo.
    for ruidosa in ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock",
                    "faster_whisper"):
        logging.getLogger(ruidosa).setLevel(logging.WARNING)

    cargar_env()

    try:
        cfg = config.cargar("config.yaml")
    except Exception as e:
        webview.create_window("Traductor del culto",
                              html=pagina_error(f"Error en config.yaml", str(e)))
        webview.start(private_mode=False, icon=str(ICONO) if ICONO.exists() else None)
        return 1

    # Sin terminal no hay donde mostrar un PIN generado al azar: se pierde en
    # el aire y el panel queda inutilizable. El chequeo de esto va despues de
    # construir el Pipeline: si ademas falta la clave de traduccion o un
    # dispositivo, ese es el problema real y hay que mostrar ese primero.
    pin = resolver_pin()

    try:
        pipeline = Pipeline(cfg, ruta_config="config.yaml")
    except Exception as e:
        webview.create_window("Traductor del culto",
                              html=pagina_error("No pude arrancar.", str(e)))
        webview.start(private_mode=False, icon=str(ICONO) if ICONO.exists() else None)
        return 1

    if not os.environ.get("PANEL_PIN"):
        webview.create_window("Traductor del culto", html=pagina_falta_pin())
        webview.start(private_mode=False, icon=str(ICONO) if ICONO.exists() else None)
        return 1

    servidor = ServidorWeb(cfg.web.puerto, cfg, pipeline, pin=pin)
    try:
        url = servidor.iniciar()
    except OSError as e:
        webview.create_window(
            "Traductor del culto",
            html=pagina_error(f"No pude abrir el puerto {cfg.web.puerto}.", str(e)),
        )
        webview.start(private_mode=False, icon=str(ICONO) if ICONO.exists() else None)
        return 1

    pipeline.al_actualizar = servidor.publicar
    pipeline.iniciar()

    def al_cerrar(*_a):
        # Se llama al hacer clic en la X de la ventana. Si algo tarda o
        # falla apagando, que la ventana igual se cierre: quedarse
        # colgado ahi es peor que un stream que no se cerro prolijo.
        try:
            pipeline.detener()
            servidor.detener()
        except Exception:
            log.exception("Fallo cerrando el pipeline")

    # ?ventana=1: el panel usa esto para saber que no hay ninguna terminal
    # donde buscar un PIN al azar (le cambia el texto que pide al usuario).
    ventana = webview.create_window(
        "Traductor del culto", url + "?ventana=1",
        width=1180, height=820, min_size=(720, 560),
    )
    ventana.events.closing += al_cerrar
    # private_mode=False: sin esto, el PIN guardado en el panel (localStorage)
    # se borra cada vez que se cierra la ventana y habria que reingresarlo en
    # cada arranque.
    webview.start(private_mode=False, icon=str(ICONO) if ICONO.exists() else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
