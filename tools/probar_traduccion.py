"""Verifica que el proveedor de traduccion este bien configurado.

Hace unas pocas llamadas reales con frases de un culto y muestra que devuelve,
cuanto tarda y cuanto costaria. Sirve para confirmar antes del sabado que la
clave anda, que el modelo existe y que el glosario se esta respetando.

    .venv/bin/python -m tools.probar_traduccion
    .venv/bin/python -m tools.probar_traduccion --modelos    # que modelos hay
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from rich.console import Console

from traductor import config
from traductor.entorno import cargar_env
from traductor.glosario import Glosario
from traductor.traduccion import PROVEEDORES

consola = Console()

# Frases reales de un culto, con los errores de reconocimiento que comete
# Whisper. Una buena traduccion los corrige por contexto.
FRASES = [
    "Gracias, Ericordia. Y gracias a los jóvenes.",
    "Que nos han presentado con sus voces.",
    "La grandeza de Jehová, Dios poderoso.",
    "Sabemos que va incresciendo el numero de víctimas.",
    "El pastor Marcelo va a compartir después de la ofenda.",
]

AYUDA = {
    "api key": "La clave no es valida. Revisá el valor en .env.",
    "api_key": "La clave no es valida. Revisá el valor en .env.",
    "credit balance": "La cuenta no tiene credito. Cargá saldo en la consola del proveedor.",
    "quota": "Te quedaste sin cuota. Revisá los limites de tu cuenta.",
    "not found": "Ese modelo no existe o tu cuenta no lo tiene habilitado.\n"
                 "     Corré con --modelos para ver cuales podés usar.",
    "permission": "La clave no tiene permiso para ese modelo.",
    "unauthenticated": "La clave no es valida o falta.",
}


def diagnosticar(error: str) -> str | None:
    bajo = error.lower()
    for marca, consejo in AYUDA.items():
        if marca in bajo:
            return consejo
    return None


def listar_modelos(cfg) -> int:
    if cfg.traduccion.proveedor != "gemini":
        consola.print("[yellow]--modelos solo esta implementado para gemini.[/yellow]")
        return 1
    from google import genai

    cliente = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    consola.print("[grey50]Modelos que soportan generacion de texto:[/grey50]\n")
    for m in cliente.models.list():
        acciones = getattr(m, "supported_actions", None) or []
        if "generateContent" in acciones or not acciones:
            consola.print(f"  [cyan]{m.name.removeprefix('models/')}[/cyan]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--modelos", action="store_true", help="listar modelos disponibles")
    args = ap.parse_args()

    cargar_env()
    cfg = config.cargar(args.config)
    prov = cfg.traduccion.proveedor

    if prov not in PROVEEDORES:
        consola.print(
            f"[yellow]proveedor: {prov!r}[/yellow] — no usa ningun servicio de nube.\n"
            f"Se va a traducir con Whisper (offline, solo ingles)."
        )
        return 0

    clase = PROVEEDORES[prov]
    clave = os.environ.get(clase.entorno_clave)
    consola.print(f"proveedor  [bold]{prov}[/bold]")
    consola.print(f"modelo     [bold]{cfg.traduccion.modelo}[/bold]")
    consola.print(
        f"clave      {clase.entorno_clave} = "
        + (f"[green]{clave[:6]}…{clave[-4:]}[/green]" if clave
           else "[red]NO DEFINIDA[/red] — copiá .env.ejemplo a .env y pegala ahí")
    )
    if not clave:
        return 1
    if args.modelos:
        return listar_modelos(cfg)

    idiomas = cfg.idiomas
    consola.print(f"idiomas    {', '.join(idiomas)}\n")

    try:
        traductor = clase(cfg.traduccion, Glosario.cargar(), idiomas)
    except Exception as e:
        consola.print(f"[red]No pude iniciar {prov}:[/red] {e}")
        return 1

    total = 0.0
    fallos = 0
    for frase in FRASES:
        consola.print(f"  [grey50]es[/grey50]  {frase}")
        try:
            t0 = time.perf_counter()
            r = traductor.traducir(frase)
            dt = time.perf_counter() - t0
            total += dt
            for i in idiomas:
                consola.print(f"  [cyan]{i}[/cyan]  {r[i]}   [grey50][{dt:.2f}s][/grey50]")
        except Exception as e:
            fallos += 1
            consola.print(f"  [red]falló:[/red] {e}")
            consejo = diagnosticar(str(e))
            if consejo:
                consola.print(f"  [yellow]→ {consejo}[/yellow]")
            break
        consola.print()

    if fallos:
        consola.print(
            "\n[yellow]El culto igual funcionaría:[/yellow] al fallar el proveedor, "
            "el sistema cae solo al modo offline (Whisper, solo inglés)."
        )
        return 1

    consola.print(
        f"[green]Todo bien.[/green] Promedio {total / len(FRASES):.2f}s por frase.\n"
        f"[grey50]Un culto de 1 hora son unas 600 frases; a este ritmo, "
        f"la traducción no es el cuello de botella.[/grey50]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
