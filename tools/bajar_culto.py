"""Baja un tramo de un culto de YouTube y lo deja listo para probar.

Como los cultos se streamean todos los sabados, hay archivo permanente de
material de prueba real: la voz del predicador, la acustica de la sala y el
vocabulario de esta congregacion. Es lo mejor que hay para afinar el glosario.

    .venv/bin/python -m tools.bajar_culto "https://youtu.be/XXXX?t=1418"
    .venv/bin/python -m tools.bajar_culto "https://youtu.be/XXXX" --desde 23:38 --minutos 6

Despues:

    .venv/bin/python -m tools.simulacro --wav pruebas/culto-XXXX.wav

Ojo: el audio del stream es la mezcla completa (musica, congregacion, sala).
En produccion vas a tener el aux send con solo el microfono del predicador, que
es bastante mas limpio. Lo que salga de aca es un piso, no un techo.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from rich.console import Console

consola = Console()


def a_segundos(valor: str) -> int:
    """Acepta 1418, 23:38 o 1:23:38."""
    if valor.isdigit():
        return int(valor)
    partes = [int(p) for p in valor.split(":")]
    if not 2 <= len(partes) <= 3:
        raise argparse.ArgumentTypeError(f"No entiendo el tiempo {valor!r}")
    segundos = 0
    for p in partes:
        segundos = segundos * 60 + p
    return segundos


def inicio_de_url(url: str) -> int | None:
    """Lee el ?t= del enlace, que es como YouTube comparte un momento."""
    q = parse_qs(urlparse(url).query)
    for clave in ("t", "start"):
        if clave in q:
            crudo = q[clave][0]
            if crudo.isdigit():
                return int(crudo)
            m = re.match(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", crudo)
            if m and any(m.groups()):
                h, mi, s = (int(g or 0) for g in m.groups())
                return h * 3600 + mi * 60 + s
    return None


def correr(cmd: list[str], que: str) -> None:
    proceso = subprocess.run(cmd, capture_output=True, text=True)
    if proceso.returncode != 0:
        consola.print(f"[red]Fallo {que}:[/red]\n{proceso.stderr[-1500:]}")
        raise SystemExit(1)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("url", help="enlace del culto (el ?t= se respeta)")
    ap.add_argument("--desde", type=a_segundos, default=None,
                    help="momento de inicio: 1418, 23:38 o 1:23:38")
    ap.add_argument("--minutos", type=float, default=4.0, help="cuanto bajar")
    ap.add_argument("--salida", type=Path, default=Path("pruebas"))
    args = ap.parse_args()

    desde = args.desde if args.desde is not None else (inicio_de_url(args.url) or 0)
    hasta = desde + int(args.minutos * 60)

    args.salida.mkdir(parents=True, exist_ok=True)
    ident = re.sub(r"\W+", "", urlparse(args.url).path)[-11:] or "culto"
    base = args.salida / f"culto-{ident}-{desde}"
    wav = base.with_suffix(".wav")

    consola.print(
        f"[grey50]Bajando {args.minutos:g} min desde "
        f"{desde // 60}:{desde % 60:02d}...[/grey50]"
    )
    correr(
        [sys.executable, "-m", "yt_dlp", "-f", "bestaudio", "--no-playlist",
         "--download-sections", f"*{desde}-{hasta}",
         "-o", f"{base}.%(ext)s", args.url],
        "la descarga (¿esta instalado yt-dlp?)",
    )

    descargado = next(
        (p for p in args.salida.glob(f"{base.name}.*") if p.suffix != ".wav"), None
    )
    if descargado is None:
        consola.print("[red]No encontre el archivo descargado.[/red]")
        return 1

    consola.print("[grey50]Convirtiendo a WAV mono 16 kHz...[/grey50]")
    correr(
        ["ffmpeg", "-y", "-i", str(descargado), "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", str(wav)],
        "la conversion (¿esta instalado ffmpeg?)",
    )
    descargado.unlink()

    consola.print(f"\nListo: [cyan]{wav}[/cyan]")
    consola.print(f"\nProbalo con:\n  [bold].venv/bin/python -m tools.simulacro --wav {wav}[/bold]\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
