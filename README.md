---
title: PDF a video narrado
emoji: 🎬
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
---

Sube un PDF de temario, repasa la narración y descarga el vídeo en MP4.
Voz generada con Piper (offline). Los ficheros se borran del servidor a las pocas horas.

## Cómo funciona

Interfaz web sobre FastAPI. El PDF se convierte en imágenes con PyMuPDF, el texto de
cada página se edita a mano antes de sintetizar, Piper genera la locución y ffmpeg
monta el MP4. Documentación técnica completa en `DOCUMENTACION.md`.
