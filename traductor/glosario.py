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

    def contexto_whisper(self) -> str:
        """Sesga el reconocimiento de voz hacia el vocabulario de la iglesia.

        Whisper toma esto como si fuera texto que viene justo antes del audio,
        asi que va como una frase corrida y no como una lista.
        """
        palabras = self.nombres + self.vocabulario + list(self.terminos)
        if not palabras:
            return ""
        return ", ".join(dict.fromkeys(palabras)) + "."

    def reglas_para(self, idiomas: list[str]) -> str:
        """Bloque de terminos obligatorios para el prompt de traduccion."""
        lineas = []
        for termino, trads in self.terminos.items():
            pares = [f"{i}={trads[i]!r}" for i in idiomas if i in trads]
            if pares:
                lineas.append(f"  - {termino!r} -> " + ", ".join(pares))
        return "\n".join(lineas)
