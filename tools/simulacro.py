"""Corre el pipeline completo sin microfono ni placa de audio.

Sirve para probar en cualquier computadora, y sobre todo para ir afinando
glosario.yaml: se escucha como quedo la traduccion antes de llegar al culto.

    # con voz sintetica a partir de un guion de ejemplo
    .venv/bin/python -m tools.simulacro --demo

    # con una grabacion real de un culto (lo mas parecido a la realidad)
    .venv/bin/python -m tools.simulacro --wav culto.wav

    # con un guion propio, una frase por linea
    .venv/bin/python -m tools.simulacro --texto mi_guion.txt

Deja un WAV por idioma para escuchar el resultado.
"""

from __future__ import annotations

import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np
import soxr
from rich.console import Console
from rich.table import Table

from traductor import config
from traductor.entorno import cargar_env
from traductor.audio import FREC_INTERNA, Segmentador
from traductor.glosario import Glosario
from traductor.stt import Transcriptor
from traductor.traduccion import Traductor
from traductor.tts import MotorTTS

consola = Console()

GUION_DEMO = [
    "Buenos días iglesia, qué alegría verlos acá esta mañana.",
    "Antes de empezar quiero que saludes a la persona que tenés al lado.",
    "Hoy vamos a leer el libro de Romanos, capítulo ocho, versículo veintiocho.",
    "Y sabemos que a Dios todas las cosas les ayudan a bien, a los que conforme a su propósito son llamados.",
    "Esto no quiere decir que todo lo que nos pasa sea bueno, hermanos.",
    "Quiere decir que Dios toma hasta lo más difícil y lo usa para algo bueno.",
    "El pastor Marcelo va a compartir con nosotros después de la ofrenda.",
    "Vamos a orar juntos y después seguimos con la alabanza.",
]


def leer_wav(ruta: Path) -> np.ndarray:
    with wave.open(str(ruta), "rb") as w:
        if w.getsampwidth() != 2:
            raise SystemExit(f"{ruta}: necesito un WAV PCM de 16 bits.")
        canales, frec = w.getnchannels(), w.getframerate()
        crudo = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    audio = crudo.astype(np.float32) / 32768.0
    if canales > 1:
        audio = audio.reshape(-1, canales)[:, 0]
    if frec != FREC_INTERNA:
        audio = soxr.resample(audio, frec, FREC_INTERNA)
    return audio.astype(np.float32)


def escribir_wav(ruta: Path, audio: np.ndarray, frecuencia: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(ruta), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(frecuencia)
        w.writeframes(pcm.tobytes())


def sintetizar_guion(frases: list[str], voz: str) -> np.ndarray:
    consola.print(f"[grey50]Generando audio de prueba con la voz {voz}...[/grey50]")
    motor = MotorTTS(voz)
    tramos = [np.zeros(int(FREC_INTERNA * 0.8), dtype=np.float32)]
    for frase in frases:
        a = motor.sintetizar(frase)
        tramos.append(soxr.resample(a, motor.frecuencia, FREC_INTERNA).astype(np.float32))
        # Pausa entre frases: es lo que el VAD usa para cortar.
        tramos.append(np.zeros(int(FREC_INTERNA * 0.9), dtype=np.float32))
    return np.concatenate(tramos)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    fuente = ap.add_mutually_exclusive_group(required=True)
    fuente.add_argument("--wav", type=Path, help="grabacion real de un culto")
    fuente.add_argument("--texto", type=Path, help="guion propio, una frase por linea")
    fuente.add_argument("--demo", action="store_true", help="guion de ejemplo")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--voz-origen", default="es_AR-daniela-high",
                    help="voz para generar el audio de prueba")
    ap.add_argument("--salida", type=Path, default=Path("simulacro"))
    args = ap.parse_args()

    cargar_env()

    cfg = config.cargar(args.config)

    if args.wav:
        audio = leer_wav(args.wav)
    else:
        frases = (
            GUION_DEMO if args.demo
            else [l.strip() for l in args.texto.read_text(encoding="utf-8").splitlines() if l.strip()]
        )
        audio = sintetizar_guion(frases, args.voz_origen)

    consola.print(f"[grey50]Audio de entrada: {len(audio) / FREC_INTERNA:.1f}s[/grey50]")

    glosario = Glosario.cargar()
    consola.print(f"[grey50]Cargando Whisper ({cfg.stt.modelo})...[/grey50]")
    transcriptor = Transcriptor(cfg.stt, glosario.contexto_whisper())
    traductor = Traductor(cfg.traduccion, glosario, cfg.idiomas, transcriptor)
    motores = {s.idioma: MotorTTS(s.voz, s.velocidad) for s in cfg.salidas}

    # 1) Segmentar en frases igual que en vivo.
    frases_detectadas = []
    seg = Segmentador(cfg.vad, frases_detectadas.append)
    paso = int(FREC_INTERNA * 0.05)
    for i in range(0, len(audio), paso):
        seg.alimentar(audio[i : i + paso])
    seg.finalizar()
    consola.print(f"[grey50]El VAD corto {len(frases_detectadas)} frases.[/grey50]\n")

    # 2) Transcribir, traducir y sintetizar cada una.
    tabla = Table(show_lines=True)
    tabla.add_column("#", justify="right", style="grey50")
    tabla.add_column("español")
    for s in cfg.salidas:
        tabla.add_column(s.nombre, style="cyan")
    tabla.add_column("stt", justify="right", style="grey50")
    tabla.add_column("trad", justify="right", style="grey50")

    pistas: dict[str, list[np.ndarray]] = {i: [] for i in cfg.idiomas}
    tiempos = {"stt": [], "trad": [], "tts": []}

    for frase in frases_detectadas:
        t0 = time.perf_counter()
        texto = transcriptor.transcribir(frase.audio)
        ms_stt = (time.perf_counter() - t0) * 1000
        if not texto:
            continue

        t0 = time.perf_counter()
        traducciones = traductor.traducir(texto, frase.audio)
        ms_trad = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        for idioma, trad in traducciones.items():
            motor = motores[idioma]
            a = motor.sintetizar(trad) if trad else np.empty(0, dtype=np.float32)
            pistas[idioma].append(a)
            # Silencio entre frases para que el WAV final se escuche natural.
            pistas[idioma].append(np.zeros(int(motor.frecuencia * 0.4), dtype=np.float32))
        ms_tts = (time.perf_counter() - t0) * 1000

        tiempos["stt"].append(ms_stt)
        tiempos["trad"].append(ms_trad)
        tiempos["tts"].append(ms_tts)
        tabla.add_row(
            str(frase.seq), texto,
            *[traducciones.get(s.idioma, "") for s in cfg.salidas],
            f"{ms_stt / 1000:.1f}s", f"{ms_trad / 1000:.1f}s",
        )

    consola.print(tabla)

    # 3) Dejar los WAV para escuchar.
    args.salida.mkdir(parents=True, exist_ok=True)
    for idioma, trozos in pistas.items():
        if not any(t.size for t in trozos):
            continue
        destino = args.salida / f"{idioma}.wav"
        escribir_wav(destino, np.concatenate(trozos), motores[idioma].frecuencia)
        consola.print(f"  audio {idioma}: [cyan]{destino}[/cyan]")

    if tiempos["stt"]:
        n = len(tiempos["stt"])
        prom = lambda k: sum(tiempos[k]) / n / 1000
        total = prom("stt") + prom("trad") + prom("tts")
        consola.print(
            f"\n[bold]Latencia de proceso por frase:[/bold] {total:.2f}s"
            f"  [grey50](whisper {prom('stt'):.2f}s + traduccion {prom('trad'):.2f}s"
            f" + voz {prom('tts'):.2f}s)[/grey50]"
        )
        consola.print(
            "[grey50]A esto se le suma la pausa que espera el VAD para cerrar la "
            f"frase ({cfg.vad.silencio_fin_ms / 1000:.1f}s).[/grey50]"
        )
        if traductor.usando_fallback:
            consola.print("[yellow]Se uso el modo offline: Claude no respondio.[/yellow]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
