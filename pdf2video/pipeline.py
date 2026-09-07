"""
Núcleo del pipeline PDF -> vídeo narrado.

Etapas:
  1. render_pdf()      PDF -> PNG por página + texto extraído
  2. build_scripts()   texto -> guión de narración (limpieza o reescritura con Ollama)
  3. synthesize()      guión -> audio (edge-tts online / piper offline)
  4. build_video()     imágenes + audios -> MP4 (ffmpeg)
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pymupdf

# --------------------------------------------------------------------------
# Modelo de datos
# --------------------------------------------------------------------------

SEP = "=== PÁGINA {n} ==="
SEP_RE = re.compile(r"^===\s*PÁGINA\s+(\d+)\s*===\s*$", re.MULTILINE)

VOICES = [
    "es-ES-AlvaroNeural",
    "es-ES-ElviraNeural",
    "es-ES-XimenaNeural",
    "es-MX-JorgeNeural",
    "es-MX-DaliaNeural",
    "es-AR-TomasNeural",
    "es-AR-ElenaNeural",
]


@dataclass
class Page:
    number: int
    image: Path
    raw_text: str = ""
    script: str = ""
    audio: Path | None = None
    duration: float = 0.0
    segment: Path | None = None
    start: float = 0.0


@dataclass
class Job:
    workdir: Path
    pages: list[Page] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.pages)


# --------------------------------------------------------------------------
# 1. PDF -> imágenes + texto
# --------------------------------------------------------------------------

def render_pdf(pdf_path: str | Path, workdir: str | Path, dpi: int = 150) -> Job:
    workdir = Path(workdir)
    img_dir = workdir / "img"
    img_dir.mkdir(parents=True, exist_ok=True)

    job = Job(workdir=workdir)
    doc = pymupdf.open(str(pdf_path))
    zoom = dpi / 72.0
    matrix = pymupdf.Matrix(zoom, zoom)

    for i, page in enumerate(doc, start=1):
        out = img_dir / f"page_{i:03d}.png"
        page.get_pixmap(matrix=matrix, alpha=False).save(str(out))
        job.pages.append(Page(number=i, image=out, raw_text=page.get_text("text")))

    doc.close()
    return job


# --------------------------------------------------------------------------
# 2. Texto -> guión de narración
# --------------------------------------------------------------------------

def clean_text(text: str) -> str:
    """Limpieza mínima para que el TTS no lea basura."""
    t = text.replace("\u00ad", "")
    t = re.sub(r"-\n(?=[a-záéíóúñ])", "", t)          # partición de palabras
    t = re.sub(r"[•▪◦·]\s*", "", t)                    # viñetas
    t = re.sub(r"\n{2,}", "\n\n", t)
    t = re.sub(r"(?<![.:;!?\n])\n(?![\n])", " ", t)    # une líneas de un mismo párrafo
    t = re.sub(r"^\s*\d+\s*$", "", t, flags=re.MULTILINE)  # números de página sueltos
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r"(https?://\S+)", "", t)
    return t.strip()


OLLAMA_PROMPT = """Eres un profesor que graba la locución de una clase.
Convierte el texto de esta diapositiva/página en un guión hablado en español,
en primera persona plural, fluido y natural para ser leído en voz alta.

Reglas:
- Solo texto plano: sin markdown, sin viñetas, sin títulos, sin emojis.
- No inventes contenido que no esté en la página.
- No digas "en esta diapositiva" más de una vez por página.
- Desarrolla ligeramente las ideas telegráficas para que se entiendan al oído.
- Entre {lo} y {hi} palabras.
- Si la página no tiene contenido docente (portada, índice, página en blanco),
  responde solo con una frase muy breve o con la palabra OMITIR.

TEXTO DE LA PÁGINA {n}:
\"\"\"{text}\"\"\"

GUIÓN:"""


def refine_with_ollama(
    text: str,
    page_no: int,
    model: str = "llama3.1:8b",
    host: str = "http://localhost:11434",
    words: tuple[int, int] = (60, 130),
    timeout: int = 300,
) -> str:
    prompt = OLLAMA_PROMPT.format(n=page_no, text=text[:6000], lo=words[0], hi=words[1])
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3},
    }).encode()
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    out = (data.get("response") or "").strip()
    if out.upper().startswith("OMITIR"):
        return ""
    return out


def build_scripts(
    job: Job,
    use_llm: bool = False,
    model: str = "llama3.1:8b",
    host: str = "http://localhost:11434",
    min_chars: int = 25,
    progress=None,
) -> Job:
    for p in job.pages:
        base = clean_text(p.raw_text)
        if len(base) < min_chars:
            p.script = ""
        elif use_llm:
            if progress:
                progress(f"Reescribiendo página {p.number}/{job.n} con {model}…")
            try:
                p.script = refine_with_ollama(base, p.number, model, host)
            except Exception as e:  # si Ollama falla, no rompemos el flujo
                p.script = base
                if progress:
                    progress(f"Ollama falló en la página {p.number} ({e}); uso texto plano.")
        else:
            p.script = base
    return job


def scripts_to_text(job: Job) -> str:
    return "\n\n".join(f"{SEP.format(n=p.number)}\n{p.script}".rstrip() for p in job.pages)


def text_to_scripts(job: Job, text: str) -> Job:
    """Vuelca el texto editado por el usuario de vuelta a las páginas."""
    chunks = SEP_RE.split(text)
    mapping: dict[int, str] = {}
    for i in range(1, len(chunks) - 1, 2):
        mapping[int(chunks[i])] = chunks[i + 1].strip()
    for p in job.pages:
        p.script = mapping.get(p.number, p.script)
    return job


# --------------------------------------------------------------------------
# 3. Guión -> audio
# --------------------------------------------------------------------------

def _edge_tts(text: str, out: Path, voice: str, rate: str, pitch: str) -> None:
    import edge_tts

    async def _run():
        comm = edge_tts.Communicate(text, voice=voice, rate=rate, pitch=pitch)
        await comm.save(str(out))

    asyncio.run(_run())


async def _edge_tts_batch(
    job: Job, audio_dir: Path, voice: str, rate: str, pitch: str,
    blank_page_seconds: float, progress=None,
) -> None:
    import edge_tts

    done = [0]
    sem = asyncio.Semaphore(8)

    async def _one(p: Page) -> None:
        async with sem:
            out = audio_dir / f"page_{p.number:03d}.mp3"
            if not p.script.strip():
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, _silence, out, blank_page_seconds)
            else:
                comm = edge_tts.Communicate(p.script, voice=voice, rate=rate, pitch=pitch)
                await comm.save(str(out))
            p.audio = out
            done[0] += 1
            if progress:
                progress(f"Sintetizando voz {done[0]}/{job.n}…")

    await asyncio.gather(*[_one(p) for p in job.pages])


def _piper_tts(text: str, out: Path, model_path: str, length_scale: float = 1.0) -> None:
    cmd = [
        "piper", "--model", model_path,
        "--length_scale", str(length_scale),
        "--output_file", str(out),
    ]
    subprocess.run(cmd, input=text.encode("utf-8"), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _silence(out: Path, seconds: float) -> None:
    run_ffmpeg([
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
        "-t", f"{seconds:.2f}", "-c:a", "libmp3lame", "-q:a", "9", str(out),
    ])


def synthesize(
    job: Job,
    engine: str = "edge",
    voice: str = "es-ES-AlvaroNeural",
    rate: str = "+0%",
    pitch: str = "+0Hz",
    piper_model: str = "",
    piper_length_scale: float = 1.0,
    blank_page_seconds: float = 3.0,
    progress=None,
) -> Job:
    audio_dir = job.workdir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    if engine == "edge":
        if progress:
            progress(f"Sintetizando voz 0/{job.n}…")
        asyncio.run(_edge_tts_batch(job, audio_dir, voice, rate, pitch, blank_page_seconds, progress))
    else:
        for p in job.pages:
            out = audio_dir / f"page_{p.number:03d}.wav"
            if progress:
                progress(f"Sintetizando voz {p.number}/{job.n}…")
            if not p.script.strip():
                out = audio_dir / f"page_{p.number:03d}.mp3"
                _silence(out, blank_page_seconds)
            elif engine == "piper":
                if not piper_model:
                    raise ValueError("Falta la ruta del modelo .onnx de Piper.")
                _piper_tts(p.script, out, piper_model, piper_length_scale)
            else:
                raise ValueError(f"Motor TTS desconocido: {engine}")
            p.audio = out

    for p in job.pages:
        p.duration = probe_duration(p.audio)
    return job


# --------------------------------------------------------------------------
# 4. Vídeo
# --------------------------------------------------------------------------

def run_ffmpeg(args: list[str]) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg falló:\n{proc.stderr.decode()[-2000:]}")


def probe_duration(path: str | Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
    ).stdout.decode().strip()
    try:
        return float(out)
    except ValueError:
        return 0.0


def build_video(
    job: Job,
    output: str | Path,
    width: int = 1920,
    height: int = 1080,
    fps: int = 25,
    tail: float = 0.7,
    bg: str = "white",
    crf: int = 23,
    progress=None,
) -> Path:
    seg_dir = job.workdir / "seg"
    seg_dir.mkdir(parents=True, exist_ok=True)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
          f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bg},"
          f"format=yuv420p")

    done = [0]
    lock = threading.Lock()

    def _encode(p: Page) -> tuple:
        seg = seg_dir / f"seg_{p.number:03d}.mp4"
        dur = max(p.duration + tail, 1.5)
        run_ffmpeg([
            "-loop", "1", "-framerate", str(fps), "-i", str(p.image),
            "-i", str(p.audio),
            "-af", f"apad=whole_dur={dur:.2f}",
            "-t", f"{dur:.2f}",
            "-vf", vf,
            "-r", str(fps),
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", str(crf),
            "-tune", "stillimage",
            "-c:a", "aac", "-b:a", "96k", "-ar", "48000", "-ac", "1",
            str(seg),
        ])
        with lock:
            done[0] += 1
            if progress:
                progress(f"Renderizando segmento {done[0]}/{job.n}…")
        return p, seg, dur

    workers = min(os.cpu_count() or 1, 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        encoded = list(pool.map(_encode, job.pages))

    clock = 0.0
    for p, seg, dur in encoded:
        p.segment = seg
        p.start = clock
        clock += dur

    listfile = job.workdir / "concat.txt"
    listfile.write_text(
        "".join(f"file '{p.segment.as_posix()}'\n" for p in job.pages), encoding="utf-8"
    )

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if progress:
        progress("Uniendo segmentos…")
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listfile), "-c", "copy", str(output)])
    return output


def _ts(seconds: float) -> str:
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(job: Job, output: str | Path, max_chars: int = 90) -> Path:
    """Subtítulos aproximados: reparte el guión de cada página según su duración."""
    output = Path(output)
    lines, idx = [], 1
    for p in job.pages:
        if not p.script.strip() or p.duration <= 0:
            continue
        frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", p.script) if f.strip()]
        bloques, buf = [], ""
        for f in frases:
            if len(buf) + len(f) + 1 <= max_chars:
                buf = f"{buf} {f}".strip()
            else:
                if buf:
                    bloques.append(buf)
                buf = f
        if buf:
            bloques.append(buf)
        total = sum(len(b) for b in bloques) or 1
        t = p.start
        for b in bloques:
            d = p.duration * len(b) / total
            lines.append(f"{idx}\n{_ts(t)} --> {_ts(t + d)}\n{b}\n")
            idx += 1
            t += d
    output.write_text("\n".join(lines), encoding="utf-8")
    return output


def burn_subtitles(video: Path, srt: Path, output: Path) -> Path:
    style = ("FontName=Arial,FontSize=22,PrimaryColour=&H00FFFFFF,"
             "OutlineColour=&H90000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=40")
    style_esc = style.replace(",", "\\,")
    run_ffmpeg([
        "-i", str(video),
        "-vf", f"subtitles={srt.as_posix()}:force_style={style_esc}",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "copy", str(output),
    ])
    return output


def check_deps() -> list[str]:
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    return missing
