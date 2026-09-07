#!/bin/bash
# Instalador para macOS. Doble clic para ejecutar.
# Toda la logica esta en instalar.py, para que sea la misma en Mac y Windows.
cd "$(dirname "$0")" || exit 1

for py in python3.12 python3.11 python3.10 python3; do
  if command -v "$py" >/dev/null 2>&1; then
    exec "$py" instalar.py
  fi
done

echo
echo " ============================================================"
echo "  No encontre Python 3 en esta computadora."
echo " ============================================================"
echo
echo "  Instalalo con:   brew install python@3.12"
echo "  O bajalo de:     https://www.python.org/downloads/"
echo
read -r -p "Enter para cerrar..."
