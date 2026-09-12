"""Escucha la entrada configurada y dice en que etapa se corta la cadena.

Cuando el medidor se mueve pero no aparece ninguna traduccion, el problema
esta en alguna de las etapas intermedias y desde afuera no se ve cual. Esto
graba unos segundos de la entrada real y reporta, etapa por etapa, que paso.

    .venv\\Scripts\\python -m tools.diagnostico
    .venv\\Scripts\\python -m tools.diagnostico -c config.prueba.yaml --segundos 30

Deja ademas un WAV de lo que entro, para poder escucharlo.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import soxr
from rich.console import Console

from traductor import config as cfgmod
from traductor.audio import FREC_INTERNA, Segmentador, buscar_dispositivo
from traductor.entorno import cargar_env
from traductor.glosario import Glosario

consola = Console()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--segundos", type=int, default=25)
    ap.add_argument("--wav", default="diagnostico.wav")
    args = ap.parse_args()

    cargar_env()
    cfg = cfgmod.cargar(args.config)
    indice = buscar_dispositivo(cfg.entrada.dispositivo, entrada=True)
    info = sd.query_devices(indice, "input")
    canales = max(cfg.entrada.canal + 1, 1)

    consola.print(
        f"\n[bold]1. PLACA[/bold]\n"
        f"   {info['name']}  [{sd.query_hostapis(info['hostapi'])['name']}]\n"
        f"   {info['max_input_channels']} canal(es) · leyendo el "
        f"[cyan]{cfg.entrada.canal}[/cyan] · ganancia x{cfg.entrada.ganancia}"
    )
    if canales > info["max_input_channels"]:
        consola.print(f"[red]   El canal {cfg.entrada.canal} no existe en esta placa.[/red]")
        return 1

    consola.print(f"\n[bold]2. GRABANDO {args.segundos}s[/bold] — que alguien hable ahora...")
    bloques = []
    with sd.InputStream(device=indice, channels=canales,
                        samplerate=cfg.entrada.frecuencia, dtype="float32",
                        blocksize=int(cfg.entrada.frecuencia * 0.05),
                        callback=lambda d, f, t, s: bloques.append(d[:, cfg.entrada.canal].copy())):
        for i in range(args.segundos):
            time.sleep(1)
            consola.print(f"   {i + 1}s", end="\r")

    crudo = np.concatenate(bloques) if bloques else np.zeros(1, dtype=np.float32)
    audio = crudo * cfg.entrada.ganancia
    if cfg.entrada.frecuencia != FREC_INTERNA:
        audio = soxr.resample(audio, cfg.entrada.frecuencia, FREC_INTERNA).astype(np.float32)

    pico, rms = float(np.abs(audio).max()), float(np.sqrt((audio.astype(np.float64) ** 2).mean()))
    dbp = 20 * np.log10(pico) if pico > 0 else -99
    dbr = 20 * np.log10(rms) if rms > 0 else -99
    recortado = float((np.abs(audio) >= 0.999).mean()) * 100

    consola.print(f"\n\n[bold]3. NIVEL[/bold] (ya con la ganancia aplicada)")
    consola.print(f"   pico {dbp:.0f} dB · promedio {dbr:.0f} dB · saturado {recortado:.1f}% del tiempo")
    if dbp < -40:
        consola.print("   [red]Casi no entra señal.[/red] Canal equivocado o micrófono cerrado.")
    elif dbp > -3 and recortado > 1:
        consola.print("   [red]Está saturando.[/red] Bajá la ganancia: así el reconocimiento falla.")
    elif dbr < -45:
        consola.print("   [yellow]Muy bajo en promedio.[/yellow] Subí la ganancia o el direct out.")
    else:
        consola.print("   [green]Nivel razonable.[/green]")

    escribir = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    with wave.open(args.wav, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(FREC_INTERNA)
        w.writeframes(escribir.tobytes())
    consola.print(f"   [grey50]audio guardado en {args.wav} — escuchalo para confirmar[/grey50]")

    consola.print(f"\n[bold]4. DETECCIÓN DE VOZ[/bold] (umbral {cfg.vad.umbral})")
    from traductor.audio import DetectorVoz, MUESTRAS_FRAME, FRAMES_POR_BLOQUE

    det = DetectorVoz(cfg.vad.motor, cfg.vad.umbral)
    probs = []
    paso = MUESTRAS_FRAME * FRAMES_POR_BLOQUE
    for i in range(0, len(audio) - paso, paso):
        probs.extend(det.probabilidades(audio[i:i + paso]))
    probs = np.array(probs) if probs else np.zeros(1)
    voz = float((probs >= cfg.vad.umbral).mean()) * 100
    consola.print(f"   probabilidad media {probs.mean():.2f} · máxima {probs.max():.2f}")
    consola.print(f"   [{'green' if voz > 5 else 'red'}]{voz:.0f}% del audio se considera voz[/]")
    if voz < 1:
        consola.print(
            "   [red]El detector no reconoce voz acá.[/red] O no es habla lo que entra,\n"
            "   o el nivel es demasiado bajo. Escuchá el WAV para saber cuál."
        )

    frases = []
    seg = Segmentador(cfg.vad, frases.append)
    for i in range(0, len(audio), 800):
        seg.alimentar(audio[i:i + 800])
    seg.finalizar()
    consola.print(f"\n[bold]5. CORTE EN FRASES[/bold]")
    consola.print(f"   {len(frases)} frase(s) en {len(audio) / FREC_INTERNA:.0f}s de audio")
    if not frases:
        consola.print(
            f"   [yellow]Ninguna frase llegó a cerrarse.[/yellow] Con min_frase_s="
            f"{cfg.vad.min_frase_s}s hace falta ese tanto de habla seguida."
        )
        return 0

    consola.print(f"\n[bold]6. RECONOCIMIENTO[/bold] (whisper {cfg.stt.modelo})")
    from traductor.stt import Transcriptor

    tr = Transcriptor(cfg.stt, Glosario.cargar().terminos_para_whisper())
    textos = []
    for f in frases:
        txt = tr.transcribir(f.audio)
        textos.append(txt)
        consola.print(f"   [{'white' if txt else 'red'}]{txt or '(vacío)'}[/]")
    if not any(textos):
        consola.print("   [red]Whisper no sacó nada.[/red] El audio no es habla inteligible.")
        return 0

    consola.print(f"\n[bold]7. TRADUCCIÓN[/bold] ({cfg.traduccion.proveedor})")
    from traductor.traduccion import Traductor

    trad = Traductor(cfg.traduccion, Glosario.cargar(), cfg.idiomas, tr)
    for f, txt in zip(frases, textos):
        if not txt:
            continue
        r = trad.traducir(txt, f.audio)
        for i in cfg.idiomas:
            consola.print(f"   [cyan]{i}[/cyan]  {r.get(i) or '(vacío)'}")
    if trad.usando_fallback:
        consola.print(f"   [yellow]Se usó el modo offline: {trad.ultimo_error[:120]}[/yellow]")
    else:
        consola.print("\n[green]La cadena completa funciona.[/green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
