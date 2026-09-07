"""Carga del glosario y armado de los prompts que lo consumen."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

NOMBRES_IDIOMA = {
    "en": "ingles",
    "pt": "portugues",
    "fr": "frances",
    "it": "italiano",
    "de": "aleman",
    "zh": "chino mandarin",
    "ko": "coreano",
    "ht": "criollo haitiano",
}


@dataclass
class Glosario:
    nombres: list[str] = field(default_factory=list)
    terminos: dict[str, dict[str, str]] = field(default_factory=dict)
    vocabulario: list[str] = field(default_factory=list)
    notas: str = ""

    @classmethod
    def cargar(cls, ruta: str | Path = "glosario.yaml") -> "Glosario":
        ruta = Path(ruta)
        if not ruta.exists():
            return cls()
        d = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
        return cls(
            nombres=d.get("nombres") or [],
            terminos=d.get("terminos") or {},
            vocabulario=d.get("vocabulario") or [],
            notas=(d.get("notas") or "").strip(),
        )

    def terminos_para_whisper(self) -> list[str]:
        """Vocabulario con el que sesgar el reconocimiento, por prioridad.

        Whisper solo acepta unos 223 tokens de contexto y descarta el resto,
        asi que el orden importa: lo que va primero es lo que sobrevive.

        Van primero los nombres propios de la congregacion, que no hay forma
        de que Whisper acierte solo, y despues el vocabulario curado.

        `terminos` NO entra aca: existe para fijar como se traduce cada cosa,
        y al traductor le llega completo porque no tiene este limite. Si una
        palabra ademas se reconoce mal, va tambien en `vocabulario`.
        """
        return list(dict.fromkeys(self.nombres + self.vocabulario))

    def reglas_para(self, idiomas: list[str]) -> str:
        """Bloque de terminos obligatorios para el prompt de traduccion."""
        lineas = []
        for termino, trads in self.terminos.items():
            pares = [f"{i}={trads[i]!r}" for i in idiomas if i in trads]
            if pares:
                lineas.append(f"  - {termino!r} -> " + ", ".join(pares))
        return "\n".join(lineas)
