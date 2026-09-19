# Baja cuBLAS y cuDNN (las librerias de CUDA que le faltan a Whisper para
# correr en GPU) y las deja donde traductor/stt.py (preparar_cuda_windows)
# ya sabe buscarlas: una carpeta "nvidia" en algun lugar de sys.path.
#
# Lo llama instalador.iss, y SOLO si detecto una placa NVIDIA -- son
# ~1.3GB entre las dos, no tiene sentido bajarlas en una PC sin GPU.
#
# No es fatal si esto falla (sin internet en ese momento, PyPI caido,
# cambio de estructura en un paquete nuevo): la aplicacion ya sabe caer
# sola a CPU si no encuentra estas DLL (ver stt.py), asi que un error aca
# no debe tirar abajo el resto de la instalacion.

param(
    [Parameter(Mandatory=$true)]
    [string]$Destino  # <app>\_internal
)

$ErrorActionPreference = 'Stop'
# La carpeta que deja cada paquete adentro de nvidia/, para poder saltear el
# que ya este (una instalacion anterior que se corto a mitad de camino, por
# ejemplo, no tiene por que volver a bajar la que ya habia llegado bien).
$paquetes = @{
    'nvidia-cublas-cu12' = 'cublas'
    'nvidia-cudnn-cu12'  = 'cudnn'
}
$tmp = Join-Path $env:TEMP 'traductor_cuda_setup'
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$destinoNvidia = Join-Path $Destino 'nvidia'
New-Item -ItemType Directory -Force -Path $destinoNvidia | Out-Null

foreach ($pkg in $paquetes.Keys) {
    if (Test-Path (Join-Path $destinoNvidia $paquetes[$pkg])) {
        Write-Host "$pkg ya estaba, lo salteo."
        continue
    }
    try {
        Write-Host "Bajando $pkg..."
        $info = Invoke-RestMethod -Uri "https://pypi.org/pypi/$pkg/json" -UseBasicParsing
        $archivo = $info.urls | Where-Object { $_.filename -like '*win_amd64.whl' } | Select-Object -First 1
        if (-not $archivo) {
            Write-Host "  no encontre un wheel para win_amd64, sigo con el siguiente"
            continue
        }
        $zip = Join-Path $tmp ($pkg + '.zip')
        Invoke-WebRequest -Uri $archivo.url -OutFile $zip -UseBasicParsing
        $extraido = Join-Path $tmp $pkg
        Expand-Archive -Path $zip -DestinationPath $extraido -Force
        $nvidiaOrigen = Join-Path $extraido 'nvidia'
        if (Test-Path $nvidiaOrigen) {
            Copy-Item -Path (Join-Path $nvidiaOrigen '*') -Destination $destinoNvidia -Recurse -Force
            Write-Host "  listo: $pkg"
        }
    } catch {
        Write-Host "  no pude instalar $pkg ($_). Whisper sigue andando en CPU."
    }
}

Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
