"""Medidor en vivo de todos los canales de una placa de entrada.

Sirve para responder una pregunta concreta: de los 8 canales que manda el
mixer por USB, cual trae el microfono del predicador. Se abre la placa, se
habla al microfono, y se mira cual barra se mueve.

    .venv/bin/python -m tools.canales
    .venv/bin/python -m tools.canales --dispositivo "IN 1-8"

El numero que aparece a la izquierda es el que va en config.yaml, en
entrada.canal.
"""

from __future__ import annotations

import argparse
import sys
import threading

import numpy as np
import sounddevice as sd
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from traductor.audio import buscar_dispositivo

consola = Console()


class Medidor:
    def __init__(self, canales: int):
        self.canales = canales
        self.pico = np.zeros(canales, dtype=np.float32)
        self.maximo = np.zeros(canales, dtype=np.float32)
        self._lock = threading.Lock()

    def alimentar(self, bloque: np.ndarray) -> None:
        p = np.abs(bloque).max(axis=0)
        with self._lock:
            # Caida lenta para que un pico se alcance a ver, y un maximo que
            # no baja nunca: si alguien hablo una sola vez, queda la marca.
            self.pico = np.maximum(p, self.pico * 0.80)
            self.maximo = np.maximum(p, self.maximo)

    def tabla(self) -> Table:
        t = Table(title="Nivel por canal — hablá al micrófono y mirá cuál se mueve")
        t.add_column("canal", justify="right", style="cyan")
        t.add_column("nivel", width=34)
        t.add_column("dB", justify="right", style="grey50")
        t.add_column("máximo visto", justify="right")

        with self._lock:
            picos, maximos = self.pico.copy(), self.maximo.copy()

        for i, (p, m) in enumerate(zip(picos, maximos)):
            # Escala logaritmica: en lineal la voz normal casi no se ve.
            frac = 0.0 if p <= 0 else max(0.0, min(1.0, (20 * np.log10(p) + 60) / 60))
            llenos = int(frac * 30)
            color = "red" if p > 0.95 else "yellow" if p > 0.7 else "green"
            barra = Text()
            barra.append("█" * llenos, style=color)
            barra.append("·" * (30 - llenos), style="grey30")

            marca = Text(f"{20 * np.log10(m):.0f} dB" if m > 0.0005 else "—")
            if m > 0.02:
                marca.stylize("bold green")
            t.add_row(str(i), barra, f"{20 * np.log10(p):.0f}" if p > 0.0005 else "—", marca)
        return t


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-d", "--dispositivo", default=None,
                    help="parte del nombre de la placa (por defecto, la del sistema)")
    ap.add_argument("--frecuencia", type=int, default=48000)
    args = ap.parse_args()

    indice = buscar_dispositivo(args.dispositivo, entrada=True)
    info = sd.query_devices(indice, "input")
    canales = info["max_input_channels"]
    consola.print(
        f"[bold]{info['name']}[/bold] — {canales} canal(es) de entrada\n"
        f"[grey50]Hablá al micrófono del púlpito. El canal que se mueva es el "
        f"que va en config.yaml.\nCtrl+C para salir.[/grey50]\n"
    )

    medidor = Medidor(canales)

    def callback(indata, frames, tiempo, estado):
        medidor.alimentar(indata)

    with sd.InputStream(device=indice, channels=canales, samplerate=args.frecuencia,
                        dtype="float32", blocksize=int(args.frecuencia * 0.05),
                        callback=callback):
        try:
            with Live(medidor.tabla(), console=consola, refresh_per_second=10) as live:
                while True:
                    live.update(medidor.tabla())
                    sd.sleep(100)
        except KeyboardInterrupt:
            pass

    with medidor._lock:
        maximos = medidor.maximo.copy()
    # Ordenados por nivel: el microfono que se hablo queda primero, y el ruido
    # de fondo de otros canales abajo, donde no confunde.
    activos = sorted(
        ((i, m) for i, m in enumerate(maximos) if m > 0.01),
        key=lambda x: -x[1],
    )
    consola.print()
    if not activos:
        consola.print(
            "[yellow]Ningún canal recibió señal.[/yellow] Revisá que el mixer esté "
            "mandando algo por USB y que el micrófono esté abierto."
        )
        return 0

    consola.print("[bold]Canales con señal, del más fuerte al más débil:[/bold]")
    for puesto, (i, m) in enumerate(activos):
        db = 20 * np.log10(m)
        etiqueta = ("  <- este parece el micrófono" if puesto == 0 and db > -25
                    else "  (muy bajo, probablemente ruido)" if db < -35 else "")
        consola.print(f"   canal [cyan]{i}[/cyan]   {db:6.0f} dB[green]{etiqueta}[/green]")

    consola.print(
        f"\nPonelo en [cyan]config.yaml[/cyan]:\n"
        f"  entrada.dispositivo: nombre de la placa\n"
        f"  entrada.canal: [cyan]{activos[0][0]}[/cyan]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
