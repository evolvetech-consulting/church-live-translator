"""Lista las placas de audio de esta computadora.

    .venv/bin/python -m tools.dispositivos

Los nombres que salen aca son los que van en config.yaml (alcanza con una
parte del nombre). La columna "salidas" dice cuantos idiomas soporta cada
placa: una salida estereo son DOS canales independientes, o sea dos
transmisores con una sola interfaz.
"""

from __future__ import annotations

import sounddevice as sd
from rich.console import Console
from rich.table import Table

consola = Console()


def main() -> int:
    dispositivos = sd.query_devices()
    try:
        ent_def, sal_def = sd.default.device
    except Exception:
        ent_def = sal_def = None

    tabla = Table(title="Placas de audio disponibles")
    tabla.add_column("#", justify="right", style="grey50")
    tabla.add_column("nombre")
    tabla.add_column("entradas", justify="right")
    tabla.add_column("salidas", justify="right")
    tabla.add_column("idiomas", justify="right", style="cyan")
    tabla.add_column("", style="grey50")

    for i, d in enumerate(dispositivos):
        salidas = d["max_output_channels"]
        marcas = []
        if i == ent_def:
            marcas.append("entrada por defecto")
        if i == sal_def:
            marcas.append("salida por defecto")
        tabla.add_row(
            str(i),
            d["name"],
            str(d["max_input_channels"]),
            str(salidas),
            str(salidas) if salidas else "-",
            ", ".join(marcas),
        )

    consola.print(tabla)
    consola.print(
        "\n[bold]Entrada:[/bold] el aux send del mixer, con SOLO el microfono "
        "del predicador.\n"
        "[bold]Salida:[/bold] un canal por idioma. En una placa estereo, "
        "canal 0 = izquierdo y canal 1 = derecho,\n"
        "y cada uno va a un transmisor Retekess distinto.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
