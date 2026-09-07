# PDF → vídeo narrado (versión web)

Mismo pipeline que la versión de escritorio, pero servido como web: subes el PDF desde
el navegador (portátil, tablet o el PC del aula), repasas la narración página a página
y descargas el MP4. Backend FastAPI + frontend estático sin dependencias de terceros.

```
navegador ──upload──> FastAPI ──PyMuPDF──> páginas PNG + texto
     ↑                    │
   edición del guión      ├── edge-tts / Piper ──> audio por página
     ↓                    └── ffmpeg ──> MP4 + SRT
navegador <──polling── estado del trabajo (hilo en segundo plano)
```

## Ejecutar en tu máquina

Requisitos: Python 3.10+ y **ffmpeg** en el PATH.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py            # http://127.0.0.1:8000
```

Para abrirlo desde otros equipos de la red (por ejemplo, el ordenador del aula):

```bash
PDF2VIDEO_HOST=0.0.0.0 PDF2VIDEO_TOKEN=loquesea python server.py
# desde otro equipo: http://IP-DE-TU-PC:8000/?t=loquesea
```

## Con Docker

```bash
docker build -t pdf2video .
docker run -p 8000:8000 -e PDF2VIDEO_TOKEN=loquesea pdf2video
```

## Publicarlo en internet (gratis)

| Opción | Comentario |
|---|---|
| **Cloudflare Tunnel** o **Tailscale** sobre tu propio equipo | Lo más sencillo y sin límites de CPU: `cloudflared tunnel --url http://localhost:8000` |
| **Hugging Face Spaces** (SDK Docker, CPU basic) | Gratis; sube este repo con el `Dockerfile`. Ojo con el límite de disco temporal |
| **Render / Railway** capa gratuita | Funciona, pero el contenedor se duerme y con 512 MB de RAM el montaje va justo |

### Hugging Face Spaces, paso a paso

1. Crea un Space con **SDK: Docker** (privado si el temario no es público).
2. Renombra `README-HF.md` a `README.md` (esa cabecera es la que configura el Space)
   y sube el resto del repo tal cual.
3. En *Settings → Variables and secrets* añade `PDF2VIDEO_TOKEN`.
4. Entra con `https://TU-USUARIO-TU-SPACE.hf.space/?t=TU-TOKEN`.

El `Dockerfile` deja la imagen lista para eso: escucha en el 7860 y usa **Piper**,
que sintetiza dentro del contenedor. Es importante: `edge-tts` sale contra Microsoft
y desde IPs de datacenter suele acabar bloqueado. En tu portátil usa edge (mejor voz),
en la nube usa piper (siempre funciona).

Si lo publicas, pon **siempre** `PDF2VIDEO_TOKEN`: no hay usuarios ni contraseñas,
cualquiera con la URL podría consumir tu CPU.

## Variables de entorno

| Variable | Por defecto | Para qué |
|---|---|---|
| `PDF2VIDEO_TOKEN` | vacío | Si está puesta, exige `?t=…` o cabecera `x-token` |
| `PDF2VIDEO_HOST` | `127.0.0.1` | `0.0.0.0` para exponer en la red |
| `PORT` | `8000` | Puerto |
| `PDF2VIDEO_DATA` | `./data` | Dónde se guardan los trabajos |
| `PDF2VIDEO_MAX_MB` | `50` | Tamaño máximo del PDF |
| `PDF2VIDEO_MAX_PAGES` | `120` | Páginas máximas por PDF |
| `PDF2VIDEO_TTL_HOURS` | `12` | Horas antes de borrar un trabajo del disco |

## Cómo funciona por dentro

- `server.py`: API REST. Cada PDF crea un *trabajo* con su carpeta en `data/`.
  La generación corre en un hilo aparte y el navegador consulta `/api/jobs/{id}/estado`
  cada 1,5 s, así que puedes cerrar y volver a abrir la pestaña sin cortar el proceso.
- `pdf2video/pipeline.py`: idéntico al de la versión de escritorio (render, TTS,
  ffmpeg, subtítulos). Si tocas ahí, ambas versiones se benefician.
- `web/`: HTML + CSS + JS planos, sin build ni npm. El reloj de la izquierda estima
  la duración final a 155 palabras por minuto mientras escribes.

### Endpoints

| Método | Ruta | Qué hace |
|---|---|---|
| `GET` | `/api/config` | Voces disponibles, si hay Ollama, límites |
| `POST` | `/api/jobs` | Sube el PDF y devuelve páginas + texto extraído |
| `POST` | `/api/jobs/{id}/generar` | Lanza la síntesis y el montaje |
| `GET` | `/api/jobs/{id}/estado` | Fase, progreso 0-100, mensaje, ficheros |
| `GET` | `/api/jobs/{id}/fichero/{n}` | Descarga MP4 / SRT |
| `DELETE` | `/api/jobs/{id}` | Borra el trabajo y sus ficheros |

Es una API normal, así que puedes automatizarla con `curl` si algún día quieres
convertir un curso entero en lote.

## Ollama (guión redactado, opcional)

Si el servidor tiene Ollama escuchando en `localhost:11434`, aparece la casilla
para que reescriba el texto de cada página como locución antes de mostrarlo.
Solo tiene sentido si ejecutas el servidor en tu propio equipo.

## Limitaciones

- Un PDF de 60 páginas tarda varios minutos: el cuello de botella es el TTS.
- `edge-tts` sale a internet contra el servicio de Microsoft. Desde una IP de datacenter
  puede acabar limitado; en ese caso usa Piper (`motor: "piper"` en la API, requiere
  `pip install piper-tts` y un modelo `.onnx` en el servidor).
- Sin cola de trabajos: si dos personas generan a la vez, ffmpeg compite por la CPU.
  Para uso de un departamento, mételo detrás de un `--workers 2` y súbele la máquina.
- PDF escaneado sin capa de texto: pásale `ocrmypdf -l spa` antes de subirlo.
