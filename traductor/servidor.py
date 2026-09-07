"""Servidor web: panel del operador y vista para la congregacion.

Dos paginas sobre el mismo flujo de eventos:

  /            panel del operador. Nivel de entrada, canales de salida, atraso
               por idioma, transcripcion en vivo y tiempos de proceso. Pensado
               para que quien este de turno no necesite mirar una terminal.

  /subtitulos  vista para la congregacion, la del codigo QR. El texto ya lo
               tenemos, asi que publicarlo no cuesta nada: sirve para personas
               sordas o hipoacusicas, para quien no alcanzo un receptor, y como
               red de seguridad si falla un transmisor.
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

PAGINAS = Path(__file__).parent / "paginas"

# Cada cuanto se manda el estado (medidores, atraso, contadores). 5 Hz alcanza
# para que los medidores se vean fluidos sin inundar la red de la iglesia.
PERIODO_ESTADO = 0.2


def ip_local() -> str:
    """IP de esta maquina en la red de la iglesia, para armar el QR."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class ServidorWeb:
    def __init__(self, puerto: int, cfg, pipeline=None):
        self.puerto = puerto
        self.cfg = cfg
        self.pipeline = pipeline
        self.t0 = time.time()

        self._suscriptores: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._servidor: ThreadingHTTPServer | None = None
        self._latidos: threading.Thread | None = None
        self._corriendo = False
        self._paginas: dict[str, bytes] = {}

    # ---------------- datos que se publican ----------------

    def _idiomas(self) -> list[dict]:
        return [
            {
                "idioma": s.idioma,
                "nombre": s.nombre,
                "ruta": f"{s.dispositivo or 'defecto'} · "
                        f"{'izq' if s.canal == 0 else 'der' if s.canal == 1 else f'canal {s.canal}'}",
            }
            for s in self.cfg.salidas
        ]

    def _estado(self) -> dict:
        p, lat = self.pipeline, self.cfg.latencia
        estado = {
            "tipo": "estado",
            "transcurrido": time.time() - self.t0,
            "dispositivo": self.cfg.entrada.dispositivo or "por defecto",
            "idiomas": self._idiomas(),
            "entrada": 0.0,
            "modo": "claude",
            "frases": 0,
            "canales": {},
            "promedios": {},
        }
        if p is None:
            return estado

        estado["entrada"] = round(p.captura.pico, 4)
        estado["modo"] = "offline" if p.traductor.usando_fallback else "claude"
        estado["frases"] = len(p.eventos)
        estado["canales"] = {
            s.idioma: {
                "nivel": round(p.ruteador.picos.get(s.idioma, 0.0), 4),
                "atraso": round(p.ruteador.pendiente_s(s.idioma), 2),
                "umbral_medio": lat.umbral_aceleracion_s,
                "umbral_alto": lat.umbral_maximo_s,
            }
            for s in self.cfg.salidas
        }

        recientes = [e for e in p.eventos[-10:] if e.ms_traduccion]
        if recientes:
            n = len(recientes)
            estado["promedios"] = {
                "stt": round(sum(e.ms_stt for e in recientes) / n),
                "trad": round(sum(e.ms_traduccion for e in recientes) / n),
                "tts": round(
                    sum(max(e.ms_tts.values(), default=0) for e in recientes) / n
                ),
            }
        return estado

    # ---------------- publicacion ----------------

    def _emitir(self, dato: dict) -> None:
        crudo = json.dumps(dato, ensure_ascii=False)
        with self._lock:
            for cola in list(self._suscriptores):
                try:
                    cola.put_nowait(crudo)
                except queue.Full:
                    # Un celular colgado o con mala señal no puede frenar el culto.
                    pass

    def publicar(self, evento) -> None:
        """Callback del pipeline: una frase avanzo de etapa."""
        self._emitir(
            {
                "tipo": "frase",
                "seq": evento.seq,
                "t": round(evento.t_inicio, 1),
                "es": evento.texto,
                "traducciones": evento.traducciones,
                "ms_stt": round(evento.ms_stt),
                "ms_trad": round(evento.ms_traduccion),
                "offline": evento.offline,
            }
        )

    def _latir(self) -> None:
        while self._corriendo:
            self._emitir(self._estado())
            time.sleep(PERIODO_ESTADO)

    # ---------------- HTTP ----------------

    def _pagina(self, nombre: str) -> bytes:
        # Se lee de disco una sola vez y queda cacheada: durante el culto no
        # hay que tocar el disco por cada celular que se conecta.
        if nombre not in self._paginas:
            self._paginas[nombre] = (PAGINAS / nombre).read_bytes()
        return self._paginas[nombre]

    def _handler(self):
        srv = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass  # sin ruido en la consola del operador

            def _enviar(self, cuerpo: bytes, tipo: str = "text/html; charset=utf-8"):
                self.send_response(200)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(cuerpo)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(cuerpo)

            def do_GET(self):
                ruta = self.path.split("?")[0].rstrip("/") or "/"
                if ruta == "/eventos":
                    return self._sse()
                if ruta == "/estado":
                    return self._enviar(
                        json.dumps(srv._estado(), ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8",
                    )
                if ruta == "/subtitulos":
                    return self._enviar(srv._pagina("subtitulos.html"))
                if ruta == "/":
                    return self._enviar(srv._pagina("panel.html"))
                self.send_error(404)

            def _sse(self):
                cola: queue.Queue = queue.Queue(maxsize=200)
                with srv._lock:
                    srv._suscriptores.append(cola)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                try:
                    # Estado inicial, para que la pagina se dibuje entera sin
                    # esperar al primer latido.
                    self.wfile.write(
                        f"data: {json.dumps(srv._estado(), ensure_ascii=False)}\n\n".encode()
                    )
                    # Y el historial de lo que ya se dijo, para quien entra tarde.
                    if srv.pipeline is not None:
                        for ev in srv.pipeline.eventos[-40:]:
                            if ev.traducciones:
                                srv_dato = {
                                    "tipo": "frase", "seq": ev.seq,
                                    "t": round(ev.t_inicio, 1), "es": ev.texto,
                                    "traducciones": ev.traducciones,
                                    "ms_stt": round(ev.ms_stt),
                                    "ms_trad": round(ev.ms_traduccion),
                                    "offline": ev.offline,
                                }
                                self.wfile.write(
                                    f"data: {json.dumps(srv_dato, ensure_ascii=False)}\n\n".encode()
                                )
                    self.wfile.flush()

                    while True:
                        try:
                            dato = cola.get(timeout=15)
                            self.wfile.write(f"data: {dato}\n\n".encode("utf-8"))
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")  # mantiene viva la conexion
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with srv._lock:
                        if cola in srv._suscriptores:
                            srv._suscriptores.remove(cola)

        return Handler

    # ---------------- ciclo de vida ----------------

    def iniciar(self) -> str:
        self._servidor = ThreadingHTTPServer(("0.0.0.0", self.puerto), self._handler())
        self._servidor.daemon_threads = True
        threading.Thread(target=self._servidor.serve_forever, daemon=True).start()

        self._corriendo = True
        self._latidos = threading.Thread(target=self._latir, daemon=True)
        self._latidos.start()

        return f"http://{ip_local()}:{self.puerto}"

    def detener(self) -> None:
        self._corriendo = False
        if self._latidos is not None:
            self._latidos.join(timeout=1)
        if self._servidor is not None:
            self._servidor.shutdown()
            self._servidor.server_close()
            self._servidor = None
