"""Traductor automatico de culto.

    .venv/bin/python main.py                 # arranca con config.yaml
    .venv/bin/python main.py -c otro.yaml    # otra configuracion
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
import time

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from traductor import config
from traductor.entorno import cargar_env
from traductor.pipeline import Pipeline

consola = Console()


def barra(nivel: float, ancho: int = 22) -> Text:
    """Medidor de nivel de audio, para ver de un vistazo si entra señal."""
    llenos = int(min(max(nivel, 0.0), 1.0) * ancho)
    color = "red" if nivel > 0.95 else "yellow" if nivel > 0.7 else "green"
    t = Text()
    t.append("#" * llenos, style=color)
    t.append("-" * (ancho - llenos), style="grey37")
    return t


class Panel_:
    """Arma la pantalla del operador."""

    def __init__(self, pipeline: Pipeline, cfg):
        self.p = pipeline
        self.cfg = cfg
        self.t0 = time.time()

    def _cabecera(self) -> Panel:
        transcurrido = time.time() - self.t0
        modo = (
            Text("OFFLINE (Whisper)", style="bold yellow")
            if self.p.traductor.usando_fallback
            else Text("Claude", style="bold green")
        )
        t = Text()
        t.append("entrada  "); t.append_text(barra(self.p.captura.pico))
        t.append(f"   {self.cfg.entrada.dispositivo or 'defecto'}")
        t.append("     traduccion  "); t.append_text(modo)
        t.append(f"     {int(transcurrido // 60):02d}:{int(transcurrido % 60):02d}")
        t.append(f"     frases: {len(self.p.eventos)}")
        return Panel(t, title="estado", border_style="blue")

    def _canales(self) -> Panel:
        tabla = Table(box=None, expand=True, pad_edge=False)
        for col in ("idioma", "canal", "nivel", "atraso", "ultimo"):
            tabla.add_column(col, overflow="ellipsis", no_wrap=(col != "ultimo"))

        ultimo = self.p.eventos[-1] if self.p.eventos else None
        for s in self.cfg.salidas:
            atraso = self.p.ruteador.pendiente_s(s.idioma)
            estilo = "red" if atraso > self.cfg.latencia.umbral_maximo_s else (
                "yellow" if atraso > self.cfg.latencia.umbral_aceleracion_s else "green"
            )
            tabla.add_row(
                s.nombre,
                f"{s.dispositivo or 'defecto'} / {'izq' if s.canal == 0 else 'der' if s.canal == 1 else s.canal}",
                barra(self.p.ruteador.picos.get(s.idioma, 0.0), 12),
                Text(f"{atraso:4.1f}s", style=estilo),
                (ultimo.traducciones.get(s.idioma, "") if ultimo else ""),
            )
        return Panel(tabla, title="canales de salida", border_style="blue")

    def _historial(self) -> Panel:
        lineas = []
        for ev in self.p.eventos[-7:]:
            t = Text()
            t.append(f"{int(ev.t_inicio // 60):02d}:{int(ev.t_inicio % 60):02d} ", style="grey50")
            t.append(ev.texto, style="white")
            lineas.append(t)
            for idioma, trad in ev.traducciones.items():
                if trad:
                    sub = Text("      ")
                    sub.append(f"{idioma}  ", style="cyan")
                    sub.append(trad, style="grey70")
                    lineas.append(sub)
        if not lineas:
            lineas = [Text("Esperando audio...", style="grey50")]
        return Panel(Group(*lineas), title="transcripcion y traduccion", border_style="blue")

    def _pie(self) -> Panel:
        recientes = [e for e in self.p.eventos[-10:] if e.latencia_total_ms]
        t = Text()
        if recientes:
            prom = sum(e.latencia_total_ms for e in recientes) / len(recientes)
            t.append(f"proceso {prom / 1000:.1f}s por frase")
            stt = sum(e.ms_stt for e in recientes) / len(recientes)
            trad = sum(e.ms_traduccion for e in recientes) / len(recientes)
            t.append(f"   (whisper {stt / 1000:.1f}s + traduccion {trad / 1000:.1f}s)", style="grey50")
        tirado = sum(self.p.descartado_s.values())
        if tirado:
            t.append(f"   descartado: {tirado:.0f}s", style="yellow")
        t.append("      Ctrl+C para terminar", style="grey50")
        return Panel(t, border_style="grey37")

    def render(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(self._cabecera(), size=3),
            Layout(self._canales(), size=3 + len(self.cfg.salidas)),
            Layout(self._historial()),
            Layout(self._pie(), size=3),
        )
        return lay


def main() -> int:
    ap = argparse.ArgumentParser(description="Traductor automatico de culto")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument(
        "--archivo", metavar="RUTA",
        help="reproducir un archivo O UN ENLACE DE YOUTUBE por el pipeline en "
             "vez de escuchar el micrófono (ensayo general: la traducción sale "
             "igual por el transmisor)",
    )
    ap.add_argument(
        "--velocidad", type=float, default=1.0,
        help="velocidad de reproducción del archivo (1.0 = tiempo real)",
    )
    ap.add_argument(
        "--desde", metavar="TIEMPO", default=None,
        help="momento donde arrancar: 1418, 23:38 o 1:23:38 (por defecto, el "
             "?t= del enlace)",
    )
    ap.add_argument("--verboso", action="store_true", help="mostrar log detallado")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verboso else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=consola, show_path=False, rich_tracebacks=True)],
    )
    # Estas hablan de mas al descargar modelos y tapan lo que le importa
    # al operador.
    for ruidosa in ("httpx", "httpcore", "urllib3", "huggingface_hub", "filelock",
                    "faster_whisper"):
        logging.getLogger(ruidosa).setLevel(logging.WARNING)

    cargar_env()

    try:
        cfg = config.cargar(args.config)
    except Exception as e:
        consola.print(f"[red]Error en {args.config}:[/red] {e}")
        return 1

    # Avisar ANTES de arrancar: descubrir a mitad del culto que se esta
    # traduciendo en modo degradado es tarde.
    if cfg.traduccion.proveedor == "claude" and not os.environ.get("ANTHROPIC_API_KEY"):
        consola.print(
            "[yellow]No encuentro ANTHROPIC_API_KEY.[/yellow] Va a funcionar en "
            "modo offline (Whisper), con calidad menor.\n"
            "Para usar Claude: copiar [cyan].env.ejemplo[/cyan] a [cyan].env[/cyan] "
            "y pegar la clave adentro.\n"
        )

    try:
        desde = None
        if args.desde is not None:
            from traductor.fuente import segundos_de

            desde = segundos_de(args.desde)
        pipeline = Pipeline(
            cfg, archivo=args.archivo, velocidad=args.velocidad, desde=desde
        )
    except Exception as e:
        consola.print(f"[red]No pude arrancar:[/red] {e}")
        return 1

    servidor = None
    if cfg.web.activo:
        from traductor.servidor import ServidorWeb

        servidor = ServidorWeb(cfg.web.puerto, cfg, pipeline)
        try:
            url = servidor.iniciar()
        except OSError as e:
            consola.print(
                f"[yellow]No pude abrir el puerto {cfg.web.puerto} ({e}).[/yellow] "
                "Sigo sin interfaz web; cambiá `web.puerto` en config.yaml.\n"
            )
            servidor = None
        else:
            pipeline.al_actualizar = servidor.publicar
            consola.print(
                f"\n  Panel del operador   [bold cyan]{url}[/bold cyan]\n"
                f"  Vista congregación   [cyan]{url}/subtitulos[/cyan]  (para el QR)\n"
            )

    parar = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: parar.set())

    pipeline.iniciar()
    vista = Panel_(pipeline, cfg)
    try:
        with Live(vista.render(), console=consola, refresh_per_second=8, screen=True) as live:
            while not parar.is_set():
                live.update(vista.render())
                parar.wait(0.12)
    finally:
        pipeline.detener()
        if servidor is not None:
            servidor.detener()

    consola.print(f"\nCulto terminado: {len(pipeline.eventos)} frases traducidas.")
    consola.print(f"Registro en [cyan]{cfg.registro_carpeta}/[/cyan]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
