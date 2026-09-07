"""
Servidor web de PDF -> vídeo narrado.

    uvicorn server:app --host 0.0.0.0 --port 8000
o simplemente:
    python server.py
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pdf2video import pipeline as P

BASE = Path(__file__).parent
DATA = Path(os.environ.get("PDF2VIDEO_DATA", BASE / "data"))
DATA.mkdir(parents=True, exist_ok=True)

TOKEN = os.environ.get("PDF2VIDEO_TOKEN", "")          # protege la app si la expones
MAX_MB = int(os.environ.get("PDF2VIDEO_MAX_MB", "50"))
MAX_PAGES = int(os.environ.get("PDF2VIDEO_MAX_PAGES", "120"))
JOB_TTL = int(os.environ.get("PDF2VIDEO_TTL_HOURS", "12")) * 3600
MOTOR = os.environ.get("PDF2VIDEO_TTS", "edge")        # edge | piper
PIPER_MODEL = os.environ.get("PDF2VIDEO_PIPER_MODEL", "")

app = FastAPI(title="PDF → vídeo narrado")

JOBS: dict[str, dict[str, Any]] = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------ utils
def _get(job_id: str) -> dict[str, Any]:
    with LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Ese trabajo ya no existe. Vuelve a subir el PDF.")
    return job


def _purge() -> None:
    now = time.time()
    with LOCK:
        viejos = [k for k, v in JOBS.items() if now - v["created"] > JOB_TTL]
        for k in viejos:
            shutil.rmtree(JOBS[k]["dir"], ignore_errors=True)
            JOBS.pop(k, None)


@app.middleware("http")
async def auth(request: Request, call_next):
    if TOKEN and not request.url.path.startswith("/health"):
        enviado = request.headers.get("x-token") or request.query_params.get("t")
        if enviado != TOKEN:
            return JSONResponse({"detail": "Token no válido"}, status_code=401)
    return await call_next(request)


# ------------------------------------------------------------------ API
@app.get("/api/config")
def config():
    ollama = False
    try:
        import urllib.request
        urllib.request.urlopen("http://localhost:11434/api/tags", timeout=1)
        ollama = True
    except Exception:
        pass
    return {
        "motor": MOTOR,
        "voces": P.VOICES if MOTOR == "edge" else [],
        "ollama": ollama,
        "ffmpeg": not P.check_deps(),
        "max_mb": MAX_MB,
        "max_paginas": MAX_PAGES,
    }


@app.post("/api/jobs")
async def crear_job(
    pdf: UploadFile = File(...),
    dpi: int = Form(150),
    usar_llm: bool = Form(False),
    modelo: str = Form("llama3.1:8b"),
):
    _purge()
    if P.check_deps():
        raise HTTPException(500, "ffmpeg no está instalado en el servidor.")
    if not pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Solo se aceptan ficheros PDF.")

    datos = await pdf.read()
    if len(datos) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, f"El PDF supera los {MAX_MB} MB.")

    job_id = uuid.uuid4().hex[:12]
    d = DATA / job_id
    d.mkdir(parents=True, exist_ok=True)
    ruta = d / "origen.pdf"
    ruta.write_bytes(datos)

    try:
        job = P.render_pdf(ruta, d, dpi=max(72, min(int(dpi), 300)))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(400, f"No se pudo leer el PDF: {e}")

    if job.n > MAX_PAGES:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(413, f"El PDF tiene {job.n} páginas; el límite es {MAX_PAGES}.")

    P.build_scripts(job, use_llm=usar_llm, model=modelo)

    with LOCK:
        JOBS[job_id] = {
            "dir": d, "job": job, "created": time.time(),
            "estado": {"fase": "listo", "progreso": 0, "mensaje": "", "ficheros": []},
            "nombre": Path(pdf.filename).stem,
        }

    return {
        "id": job_id,
        "nombre": Path(pdf.filename).stem,
        "paginas": [
            {"n": p.number, "texto": p.script, "img": f"/api/jobs/{job_id}/img/{p.number}"}
            for p in job.pages
        ],
    }


@app.get("/api/jobs/{job_id}/img/{n}")
def imagen(job_id: str, n: int):
    job = _get(job_id)["job"]
    for p in job.pages:
        if p.number == n:
            return FileResponse(p.image, media_type="image/png")
    raise HTTPException(404, "Página no encontrada")


class Opciones(BaseModel):
    guion: str
    motor: str = ""                  # vacío = el configurado en el servidor
    voz: str = "es-ES-AlvaroNeural"
    velocidad: int = 0
    tono: int = 0
    piper_model: str = ""
    ancho: int = 1920
    alto: int = 1080
    fps: int = 25
    subtitulos: str = "srt"          # "srt" | "incrustados" | "no"
    seg_vacias: float = 3.0
    nombre: str = ""


@app.post("/api/jobs/{job_id}/generar")
def generar(job_id: str, op: Opciones):
    entrada = _get(job_id)
    if entrada["estado"]["fase"] == "trabajando":
        raise HTTPException(409, "Este vídeo ya se está generando.")

    job = entrada["job"]
    P.text_to_scripts(job, op.guion)
    entrada["estado"] = {"fase": "trabajando", "progreso": 1,
                         "mensaje": "Preparando…", "ficheros": []}

    def trabajo():
        estado = entrada["estado"]
        total = job.n * 2 + 3
        hechos = {"i": 0}

        def paso(msg: str):
            hechos["i"] += 1
            estado["progreso"] = min(int(hechos["i"] / total * 100), 98)
            estado["mensaje"] = msg

        try:
            P.synthesize(
                job, engine=op.motor or MOTOR, voice=op.voz,
                rate=f"{op.velocidad:+d}%", pitch=f"{op.tono:+d}Hz",
                piper_model=op.piper_model or PIPER_MODEL,
                piper_length_scale=max(0.4, 1.0 - op.velocidad / 100.0),
                blank_page_seconds=op.seg_vacias, progress=paso,
            )
            base = "".join(c for c in (op.nombre or entrada["nombre"] or "video")
                           if c.isalnum() or c in "-_") or "video"
            salida = entrada["dir"] / f"{base}.mp4"
            P.build_video(job, salida, width=op.ancho, height=op.alto,
                          fps=op.fps, progress=paso)

            ficheros = [{"nombre": salida.name, "url": f"/api/jobs/{job_id}/fichero/{salida.name}"}]
            if op.subtitulos in ("srt", "incrustados"):
                srt = P.write_srt(job, entrada["dir"] / f"{base}.srt")
                ficheros.append({"nombre": srt.name,
                                 "url": f"/api/jobs/{job_id}/fichero/{srt.name}"})
                if op.subtitulos == "incrustados":
                    estado["mensaje"] = "Incrustando subtítulos…"
                    quemado = entrada["dir"] / f"{base}_sub.mp4"
                    P.burn_subtitles(salida, srt, quemado)
                    ficheros.insert(0, {"nombre": quemado.name,
                                        "url": f"/api/jobs/{job_id}/fichero/{quemado.name}"})
                    salida = quemado

            estado.update(
                fase="hecho", progreso=100,
                mensaje=f"{job.n} páginas · {P.probe_duration(salida) / 60:.1f} min",
                video=f"/api/jobs/{job_id}/fichero/{salida.name}", ficheros=ficheros,
            )
        except Exception as e:
            estado.update(fase="error", progreso=0, mensaje=str(e)[:500])

    threading.Thread(target=trabajo, daemon=True).start()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/estado")
def estado(job_id: str):
    return _get(job_id)["estado"]


@app.get("/api/jobs/{job_id}/fichero/{nombre}")
def fichero(job_id: str, nombre: str):
    d = _get(job_id)["dir"]
    ruta = (d / nombre).resolve()
    if not ruta.is_file() or d.resolve() not in ruta.parents:
        raise HTTPException(404, "Fichero no encontrado")
    return FileResponse(ruta, filename=nombre)


@app.delete("/api/jobs/{job_id}")
def borrar(job_id: str):
    entrada = _get(job_id)
    shutil.rmtree(entrada["dir"], ignore_errors=True)
    with LOCK:
        JOBS.pop(job_id, None)
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "trabajos": len(JOBS)}


app.mount("/", StaticFiles(directory=BASE / "web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("PDF2VIDEO_HOST", "127.0.0.1"),
                port=int(os.environ.get("PORT", "8000")))
