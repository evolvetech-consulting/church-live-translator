# Runtime hook de PyInstaller: corre antes que cualquier import de la
# aplicacion, incluido numpy.
#
# El wheel de numpy para Windows trae su OpenBLAS y su propio msvcp140 en una
# carpeta hermana ("numpy.libs"), no adentro de numpy/ mismo, para no
# depender del Visual C++ Redistributable del sistema. Como encuentra esos
# archivos es un mecanismo interno del .pyd compilado (no hay ningun import
# Python que lo haga), y con PyInstaller a veces no lo resuelve -- el
# sintoma es "DLL load failed" al importar numpy, con el archivo presente y
# del tamaño correcto, y solo en algunas maquinas (anduvo bien en la VM de
# pruebas, fallo en la PC real de la iglesia). Agregar la carpeta a mano con
# add_dll_directory() no depende de adivinar por que el mecanismo interno no
# alcanzo: Windows la suma a su propia busqueda de ahi en mas.
import os
import sys

if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    for carpeta in ("numpy.libs",):
        ruta = os.path.join(base, carpeta)
        if os.path.isdir(ruta):
            try:
                os.add_dll_directory(ruta)
            except (AttributeError, OSError):
                pass
