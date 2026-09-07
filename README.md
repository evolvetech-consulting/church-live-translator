# Traductor automático de culto

Traduce el culto en vivo y lo manda a los transmisores Retekess, un idioma por
transmisor. Reemplaza al intérprete humano, o le sirve de respaldo.

El reconocimiento de voz y la síntesis corren **en la computadora de la
iglesia**: no se paga por hora de culto. Solo la traducción usa un modelo en la
nube (Gemini o Claude), y son centavos. Si se cae internet, sigue funcionando solo.

```
mixer (aux send)  ──►  UM2 in  ──►  PC  ──►  UM2 out L  ──►  T130 #1  (inglés)
                                       └──►  UM2 out R  ──►  T130 #2  (otro idioma)
```

---

## 1. Cableado

### Entrada — lo más importante de todo

Sacá un **aux send** del mixer con **solo el micrófono del predicador**, y
metelo en la entrada de la UM2.

No uses la mezcla principal. Si al reconocimiento de voz le entra la música de
alabanza, el coro o el ruido de sala, la calidad se cae a pedazos. Un aux send
pre-fader con un solo canal es lo que hace que todo lo demás funcione.

### Salida — dos idiomas con la UM2 que ya tenés

La UM2 tiene salida estéreo (RCA L/R), y **los dos canales son independientes**.
No estás obligado a usarlos como par estéreo:

| Canal   | `canal` en config | Va a          |
|---------|-------------------|---------------|
| L (izq) | `0`               | Transmisor 1  |
| R (der) | `1`               | Transmisor 2  |

Para un tercer idioma, agregá un dongle USB de audio (~$10) y poné su nombre en
`dispositivo`.

### ⚠️ Nivel de salida hacia el T130

La entrada del T130 es de **micrófono** y la salida de la UM2 es de **línea**:
son órdenes de magnitud distintos. Si le mandás línea directo, satura y se
escucha rota.

Procedimiento seguro, la primera vez:

1. Poné `ganancia: 0.1` en `config.yaml` y el volumen de la UM2 al mínimo.
2. Subí de a poco escuchando en un receptor hasta tener buen volumen sin
   distorsión.
3. Si para llegar a buen volumen tenés que subir la ganancia por encima de
   `0.5` y se escucha ruido de fondo, conseguí un **cable atenuador de línea a
   micrófono** (line-to-mic pad). Es lo correcto y son ~$10.

El software recorta a máximo digital como último resguardo, pero eso evita el
daño, no reemplaza tener bien el nivel.

---

## 2. Instalación

### La forma fácil

1. Bajá el proyecto: **Code → Download ZIP** en GitHub, y descomprimilo.
   O con git: `git clone https://github.com/evolvetech-consulting/church-live-translator`
2. Doble clic en:
   - **`instalar.bat`** en Windows
   - **`instalar.command`** en macOS

El instalador crea el entorno, baja las librerías y las voces, prepara el
archivo de la clave y deja un acceso directo en el Escritorio. Tarda unos
minutos la primera vez. Es seguro volver a ejecutarlo: detecta lo que ya está
hecho y saltea.

> **Windows:** si no tenés Python, el instalador te lo dice y te pasa el enlace.
> Al instalarlo, tildá **"Add python.exe to PATH"** en la primera pantalla.

> **Poné la carpeta en una ruta corta** — `C:\traductor` es ideal. Windows corta
> las rutas a 260 caracteres y algunas librerías tienen límites más bajos
> todavía. El instalador avisa si la ruta es larga.

Después: **`iniciar.bat`** (o el acceso directo del Escritorio) arranca todo y
abre el panel en el navegador.

### A mano

Si preferís hacerlo paso a paso:

```bash
# macOS
brew install python@3.12 portaudio
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m piper.download_voices en_US-lessac-medium --data-dir voces
```

```bat
REM Windows (no hace falta portaudio: viene con sounddevice)
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m piper.download_voices en_US-lessac-medium --data-dir voces
```

**Si la PC tiene placa NVIDIA**, instalá CUDA y poné `dispositivo: "cuda"` en la
sección `stt`. Ahí podés usar el modelo `medium` o `large-v3`, que reconocen
bastante mejor.

### La clave del traductor

Podés usar **Gemini** o **Claude** — se elige en `config.yaml`, en
`traduccion.proveedor`. Los dos cuestan centavos por culto.

| Proveedor | Dónde se saca la clave | Variable |
|---|---|---|
| `gemini` | <https://aistudio.google.com/apikey> | `GEMINI_API_KEY` |
| `claude` | <https://console.anthropic.com> → API Keys | `ANTHROPIC_API_KEY` |

```bash
cp .env.ejemplo .env     # en Windows:  copy .env.ejemplo .env
```

y editás **`.env`** (no `.env.ejemplo`):

```
GEMINI_API_KEY=...
```

> ⚠️ `.env.ejemplo` **sí se sube al repositorio** — nunca pongas una clave real
> ahí. La que queda afuera del repo es `.env`, y ya está en `.gitignore`.

Verificá que quedó bien antes del sábado:

```bash
.venv/bin/python -m tools.probar_traduccion
```

Hace unas llamadas reales con frases de un culto, muestra qué devuelve y cuánto
tarda. Si algo falla, dice qué revisar. Con `--modelos` lista los modelos que tu
cuenta tiene habilitados.

Sin clave arranca igual y avisa al iniciar, pero en modo offline (ver §9).
---

## 3. Configuración

```bash
.venv/bin/python -m tools.dispositivos    # ver los nombres de las placas
```

Poné esos nombres en `config.yaml`. Alcanza con una parte del nombre: `"UM2"`
matchea `"UM2  Behringer"`.

Para agregar un idioma: descomentá el segundo bloque de `salidas`, poné
`canal: 1`, y bajá la voz correspondiente:

```bash
.venv/bin/python -m piper.download_voices pt_BR-faber-medium --data-dir voces
```

Voces disponibles: <https://huggingface.co/rhasspy/piper-voices>

---

## 4. Uso

```bash
.venv/bin/python main.py
```

Al arrancar imprime dos direcciones:

```
  Panel del operador   http://192.168.1.50:8080
  Vista congregación   http://192.168.1.50:8080/subtitulos   (para el QR)
```

### Panel del operador

Se abre en cualquier navegador de la red — sirve desde el celular, así que no
hace falta estar sentado frente a la PC. Muestra:

- **Nivel de entrada.** Lo primero que hay que mirar. Si no se mueve cuando
  alguien habla, el problema está en el aux send del mixer, no en el software.
- **Canales de salida**, con el nivel y el **atraso** de cada idioma. En verde
  va bien; en amarillo el sistema ya está acelerando el habla; en rojo está
  descartando audio viejo para volver a sincronizar.
- **Transcripción y traducción en vivo**, con el tiempo que tardó cada frase.
- **Modo**: en línea u offline. Si cambia a offline durante el culto, lo ves acá.

La terminal muestra lo mismo en versión compacta, por si preferís no abrir un
navegador. `Ctrl+C` para terminar.

### Vista congregación

La página del QR. Elige idioma (si hay más de uno), guarda la preferencia y
tiene un botón para agrandar la letra. Poné el QR en el boletín.

### Probar en vivo desde una laptop

Sin la UM2 ni los transmisores: le hablás al micrófono de la máquina y escuchás
la traducción al instante.

```bash
.venv/bin/python main.py -c config.mac.yaml
```

Usa la entrada y la salida que tengas puestas por defecto en el sistema, así que
sigue a los auriculares cuando los conectás.

> **Ponete auriculares.** Si sale por los parlantes, el micrófono se escucha a sí
> mismo: la traducción en inglés vuelve a entrar, se transcribe y se traduce de
> nuevo.

`config.mac.yaml` también baja `min_frase_s` a 1.5s: hablando de a una frase
suelta para probar, esperar 3 segundos de "cuerpo" se siente eterno. Para el
culto de verdad se usa `config.yaml`.

### Probar sin micrófono

```bash
.venv/bin/python -m tools.simulacro --demo          # guion de ejemplo
.venv/bin/python -m tools.simulacro --wav culto.wav # grabación real
```

Corre todo el pipeline y deja un WAV por idioma para escuchar.
---

## 5. Afinar el corte de frases

**Este es el ajuste que más impacta la calidad, más incluso que el glosario.**

Un predicador hace pausas cortas *en medio* de la oración. Si se corta en cada
pausa, cada pedazo se traduce aislado y sin contexto:

```
"Que nos han presentado con sus voces."     → "that have presented us with their voices."
"La grandeza."                              → "the greatness"
"Dejiova, Dios Poderoso."                   → "Dejeoba, Dios Poderoso."
```

Eso era una sola oración. Por eso hay dos umbrales de silencio:

| Ajuste | Qué hace |
|---|---|
| `min_frase_s` | Por debajo de esto seguimos acumulando: la pausa se considera interna |
| `silencio_fin_ms` | Cierra la frase, pero solo si ya tiene el cuerpo de `min_frase_s` |
| `silencio_largo_ms` | Cierra siempre: acá el orador terminó de verdad |

Con `min_frase_s: 3.0`, la misma parte del culto sale así:

```
"Que nos han presentado con sus voces."
"La grandeza de Jehová, Dios poderoso."
```

**`min_frase_s` es la perilla principal.** Subirlo da mejores traducciones y más
latencia; bajarlo responde antes y fragmenta más. Ajustalo según la cadencia de
quien predica, con una grabación real (ver abajo).

---

## 6. Probar con los cultos ya streameados

Como se streamea todos los sábados, hay archivo permanente de material de prueba
real: la voz del predicador, la acústica de la sala y el vocabulario de esta
congregación.

```bash
.venv/bin/python -m tools.bajar_culto "https://youtu.be/XXXX?t=1418"
.venv/bin/python -m tools.simulacro --wav pruebas/culto-XXXX-1418.wav
```

El primero baja 4 minutos desde el momento del enlace y lo deja en WAV; el
segundo corre el pipeline completo y deja un audio para escuchar.

> El audio del stream es la **mezcla completa** (música, congregación, sala). En
> producción vas a tener el aux send con solo el micrófono del predicador, que
> es bastante más limpio. Lo que salga de acá es un piso, no un techo.

Ese es el ciclo: probás, escuchás, corregís `glosario.yaml` o `min_frase_s`,
repetís. Sin esperar al sábado.

---

## 7. El glosario

`glosario.yaml` es lo que más mueve la aguja. Se usa en dos lugares:

- **Whisper** lo recibe como contexto, lo que sesga el reconocimiento hacia esas
  palabras. Es lo que evita que "Efesios" salga como "efectos".
- **El traductor** lo recibe en el prompt, para traducir con el término que usa esta
  congregación y no el genérico del diccionario.

Cada culto queda registrado en `registros/culto-AAAA-MM-DD-HHMM.jsonl` con lo
que se dijo y cómo se tradujo. Revisalo las primeras semanas: los errores que se
repiten se arreglan agregando una línea al glosario.

---

## 8. Latencia

Medido en una MacBook M1 Pro con `small` y `gemini-2.5-flash`, sobre audio real
de un culto (4 minutos, 51 frases):

| Etapa                       | Tiempo  |
|-----------------------------|---------|
| Pausa que espera el VAD      | 0.70 s  |
| Whisper (reconocimiento)     | 0.85 s  |
| Traducción                   | 0.54 s  |
| Síntesis de voz              | 0.11 s  |
| **Total**                    | **2.20 s** |

Comparable a un intérprete humano, o mejor.

**El problema que sí importa** no es la latencia de una frase, sino la
acumulada: si el predicador no hace pausas, la cola crece y a la media hora
estás 40 segundos atrás. Por eso el sistema **acelera el habla** cuando la cola
crece (hasta 1.25x, que todavía se entiende bien), y si se pasa de
`descartar_sobre_s` tira lo más viejo. Preferimos estar sincronizados a decirlo
todo.

Si el panel muestra el atraso en rojo seguido, bajá el modelo de Whisper
(`medium` → `small`) o conseguí una GPU.

---

## 9. Si se cae internet

Sigue solo, sin que nadie toque nada: Whisper también sabe traducir directo al
inglés. La calidad baja de forma clara —dice "Ilecia" por "iglesia" y no acierta
la redacción habitual de las citas bíblicas— pero el culto no se corta. Cuando
vuelve la conexión, vuelve al proveedor solo. El panel indica en qué modo está.

**El fallback offline solo cubre inglés.** Para otros idiomas, sin internet ese
canal queda en silencio (que es mejor que mandarle español a alguien que está
esperando portugués).

---

## 10. Recomendaciones para el primer culto

1. **Que alguien monitoree con auriculares** en un receptor, al menos los
   primeros meses. La IA se va a equivocar en algún momento, y en un culto eso
   importa.
2. **Dejá el camino del intérprete humano cableado y disponible.** Cambiar de
   uno a otro tiene que ser mover un fader, no re-cablear.
3. **Grabá un culto entero** y pasalo por `tools/simulacro.py` antes de salir en
   vivo. Vas a encontrar la mitad de los problemas del glosario ahí.
4. Corré todo un culto **en paralelo** con el intérprete humano antes de
   reemplazarlo. Compará.

---

## Estructura

| Archivo | Qué hace |
|---|---|
| `instalar.bat` / `.command` | Instalador de doble clic (la lógica está en `instalar.py`) |
| `main.py` | Arranque y panel del operador |
| `config.yaml` | Toda la configuración |
| `config.mac.yaml` | Configuración para probar en una laptop |
| `glosario.yaml` | Vocabulario y términos de la iglesia |
| `.env` | La clave del proveedor (crear a partir de `.env.ejemplo`) |
| `traductor/audio.py` | Captura y corte en frases (VAD) |
| `traductor/stt.py` | Reconocimiento de voz (Whisper) |
| `traductor/traduccion.py` | Traducción (Gemini / Claude + fallback offline) |
| `traductor/tts.py` | Síntesis de voz (Piper) |
| `traductor/salida.py` | Ruteo a los canales/transmisores |
| `traductor/pipeline.py` | Orquestación de las etapas |
| `traductor/servidor.py` | Servidor web (panel + subtítulos) |
| `traductor/paginas/` | HTML del panel y de la vista congregación |
| `tools/dispositivos.py` | Lista las placas de audio |
| `tools/probar_traduccion.py` | Verifica la clave y el modelo del proveedor |
| `tools/simulacro.py` | Prueba el pipeline sin hardware |
| `tools/bajar_culto.py` | Baja un tramo de un culto de YouTube para probar |
