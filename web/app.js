const $ = (s) => document.querySelector(s);
const PPM = 155;                       // palabras por minuto de la locución
const T = new URLSearchParams(location.search).get("t") || "";

let jobId = null;
let paginas = [];
let sondeo = null;
let motor = "edge";
let errores5xx = 0;
const PISTA = document.querySelector(".zona__pista").innerHTML;

const api = async (url, opciones = {}) => {
  const cab = { ...(opciones.headers || {}) };
  if (T) cab["x-token"] = T;
  const r = await fetch(url, { ...opciones, headers: cab });
  if (!r.ok) {
    let msg = `Error ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};

const reloj = (seg) => {
  const m = Math.floor(seg / 60), s = Math.round(seg % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
};
const escapar = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;");
const palabras = (t) => (t.trim() ? t.trim().split(/\s+/).length : 0);
const segundos = (t) => (palabras(t) ? (palabras(t) / PPM) * 60 : 3);

function aviso(txt, donde = "#aviso") {
  const el = $(donde);
  el.hidden = !txt;
  el.textContent = txt || "";
}

// ------------------------------------------------------------- arranque
(async function inicio() {
  try {
    const cfg = await api("/api/config");
    $("#lim-mb").textContent = cfg.max_mb;
    motor = cfg.motor || "edge";
    $("#voz").innerHTML = cfg.voces.map((v) => `<option>${v}</option>`).join("");
    if (motor !== "edge") {
      $("#voz").closest(".campo").hidden = true;   // Piper: la voz la fija el servidor
    }
    if (cfg.ollama) { $("#campo-llm").hidden = false; $("#nota-llm").hidden = true; }
    if (!cfg.ffmpeg) aviso("Falta ffmpeg en el servidor: no se podrán montar los vídeos.", "#aviso-inicio");
  } catch (e) {
    aviso(e.message, "#aviso-inicio");
  }
})();

// ------------------------------------------------------------- subir PDF
const zona = $("#zona");
["dragenter", "dragover"].forEach((ev) =>
  zona.addEventListener(ev, (e) => { e.preventDefault(); zona.classList.add("activa"); }));
["dragleave", "drop"].forEach((ev) =>
  zona.addEventListener(ev, () => zona.classList.remove("activa")));
zona.addEventListener("drop", (e) => {
  e.preventDefault();
  const f = e.dataTransfer.files[0];
  if (f) subir(f);
});
$("#pdf").addEventListener("change", (e) => e.target.files[0] && subir(e.target.files[0]));

async function subir(archivo) {
  aviso("", "#aviso-inicio");
  const mb = (archivo.size / 1024 / 1024).toFixed(1);
  $(".zona__titulo").textContent = archivo.name;
  $(".zona__pista").textContent = `${mb} MB · procesando…`;
  zona.classList.add("cargando");

  const fd = new FormData();
  fd.append("pdf", archivo);
  fd.append("dpi", $("#dpi").value);
  fd.append("usar_llm", $("#usar-llm")?.checked ? "true" : "false");

  try {
    const datos = await api("/api/jobs", { method: "POST", body: fd });
    jobId = datos.id;
    paginas = datos.paginas;
    $("#inicio").hidden = true;
    $("#banco").hidden = false;
    pintar();
  } catch (e) {
    aviso(e.message, "#aviso-inicio");
  } finally {
    zona.classList.remove("cargando");
    $(".zona__titulo").textContent = "Suelta aquí el PDF";
    $(".zona__pista").innerHTML = PISTA;
  }
}

// ------------------------------------------------------------- editor
function pintar() {
  const q = T ? `?t=${T}` : "";

  $("#paginas").innerHTML = paginas.map((p) => `
    <article class="pagina" id="pag-${p.n}">
      <div class="pagina__vista"><img src="${p.img}${q}" alt="Página ${p.n}" loading="lazy"></div>
      <div class="pagina__cuerpo">
        <p class="pagina__meta">
          <b>Página ${p.n}</b>
          <time data-t="${p.n}"></time>
        </p>
        <textarea data-n="${p.n}" placeholder="Sin texto: la página se verá en silencio unos segundos."
          >${escapar(p.texto)}</textarea>
      </div>
    </article>`).join("");

  $("#tira").innerHTML = paginas.map((p) => `
    <li><button class="tira__item" data-ir="${p.n}">
      <img src="${p.img}${q}" alt="">
      <span><span class="tira__n">Pág. ${p.n}</span><br>
      <span class="tira__t" data-tt="${p.n}"></span></span>
    </button></li>`).join("");

  document.querySelectorAll(".pagina textarea").forEach((ta) => {
    ta.addEventListener("input", () => {
      const n = +ta.dataset.n;
      paginas.find((p) => p.n === n).texto = ta.value;
      totales();
    });
  });
  document.querySelectorAll("[data-ir]").forEach((b) => {
    b.addEventListener("click", () => {
      const n = b.dataset.ir;
      $(`#pag-${n}`).scrollIntoView({ behavior: "smooth", block: "center" });
      $(`#pag-${n} textarea`).focus();
      document.querySelectorAll(".tira__item").forEach((x) => x.classList.remove("activa"));
      b.classList.add("activa");
    });
  });

  totales();
}

function totales() {
  let total = 0;
  paginas.forEach((p) => {
    const s = segundos(p.texto);
    total += s + 0.7;
    const t = $(`[data-t="${p.n}"]`);
    if (t) t.textContent = `${palabras(p.texto)} palabras · ${reloj(s)}`;
    const tt = $(`[data-tt="${p.n}"]`);
    if (tt) {
      tt.textContent = reloj(s);
      tt.closest(".tira__item").classList.toggle("muda", !palabras(p.texto));
    }
  });
  $("#reloj").textContent = reloj(total);
  $("#rail-sub").textContent = `${paginas.length} páginas · estimación`;
}

function _faseMensaje(msg) {
  if (!msg || msg.includes("Preparando")) return ["Preparando", ""];
  if (msg.includes("Sintetizando")) {
    const m = msg.match(/(\d+)\/(\d+)/);
    return ["Generando audio", m ? `Página ${m[1]} de ${m[2]}` : ""];
  }
  if (msg.includes("Renderizando")) {
    const m = msg.match(/(\d+)\/(\d+)/);
    return ["Codificando vídeo", m ? `Segmento ${m[1]} de ${m[2]}` : ""];
  }
  if (msg.includes("Uniendo")) return ["Ensamblando vídeo", ""];
  return ["Procesando", msg];
}

function actualizarProgreso(pct, msg) {
  const prog = $("#progreso");
  prog.hidden = false;
  prog.classList.remove("progreso--error");
  const [fase, detalle] = _faseMensaje(msg);
  $("#prog-fase").textContent = fase;
  $("#prog-detalle").textContent = detalle;
  $("#prog-fill").style.width = `${pct}%`;
  $("#prog-pct").textContent = `${pct}%`;
}

// ------------------------------------------------------------- generar
$("#generar").addEventListener("click", async () => {
  aviso("");
  $("#resultado").hidden = true;
  const [ancho, alto] = $("#res").value.split("x").map(Number);
  const guion = paginas.map((p) => `=== PÁGINA ${p.n} ===\n${p.texto}`).join("\n\n");

  $("#generar").disabled = true;
  $("#generar").textContent = "Generando…";
  $("#barra").hidden = false;
  $("#rail-etiqueta").textContent = "Generando vídeo";
  actualizarProgreso(1, "Preparando…");

  try {
    await api(`/api/jobs/${jobId}/generar`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        guion, motor, voz: $("#voz").value,
        velocidad: +$("#velocidad").value, ancho, alto,
        subtitulos: $("#subs").value,
      }),
    });
    clearInterval(sondeo);
    sondeo = setInterval(consultar, 1500);
  } catch (e) {
    fallo(e.message);
  }
});

async function consultar() {
  let e;
  try {
    e = await api(`/api/jobs/${jobId}/estado`);
    errores5xx = 0;
  } catch (err) {
    if (/50[234]/.test(err.message)) {
      errores5xx++;
      actualizarProgreso(0, `Servidor arrancando… (${errores5xx})`);
      if (errores5xx < 40) return;  // aguanta hasta ~60s de cold start
      clearInterval(sondeo);
      fallo("El servidor tardó demasiado en responder. Vuelve a intentarlo.");
      return;
    }
    clearInterval(sondeo);
    fallo(err.message.includes("404") || err.message.includes("ya no existe")
      ? "El servidor se reinició y perdió el trabajo. Vuelve a subir el PDF."
      : err.message);
    return;
  }

  $("#barra-relleno").style.width = `${e.progreso}%`;
  $("#rail-sub").textContent = e.mensaje || "";
  actualizarProgreso(e.progreso, e.mensaje || "");

  if (e.fase === "hecho") {
    $("#progreso").hidden = true;
    clearInterval(sondeo);
    const q = T ? `?t=${T}` : "";
    $("#video").src = e.video + q;
    $("#resultado-dato").textContent = e.mensaje;
    $("#descargas").innerHTML = e.ficheros
      .map((f) => `<li><a href="${f.url}${q}" download>${f.nombre}</a></li>`).join("");
    $("#resultado").hidden = false;
    $("#resultado").scrollIntoView({ behavior: "smooth", block: "start" });
    $("#rail-etiqueta").textContent = "Vídeo generado";
    $("#barra").hidden = true;
    $("#generar").disabled = false;
    $("#generar").textContent = "Generar de nuevo";
    totales();
  } else if (e.fase === "error") {
    clearInterval(sondeo);
    fallo(e.mensaje);
  }
}

function fallo(msg) {
  clearInterval(sondeo);
  const prog = $("#progreso");
  prog.hidden = false;
  prog.classList.add("progreso--error");
  $("#prog-fase").textContent = "Error al generar el vídeo";
  $("#prog-detalle").textContent = msg;
  $("#prog-fill").style.width = "0%";
  $("#prog-pct").textContent = "";
  aviso("");
  $("#generar").disabled = false;
  $("#generar").textContent = "Generar vídeo";
  $("#barra").hidden = true;
  $("#rail-etiqueta").textContent = "Duración estimada";
  totales();
}

// ------------------------------------------------------------- reiniciar / cancelar
async function reiniciar() {
  clearInterval(sondeo);
  errores5xx = 0;
  if (jobId) { try { await api(`/api/jobs/${jobId}`, { method: "DELETE" }); } catch {} }
  jobId = null; paginas = [];
  $("#video").removeAttribute("src");
  $("#resultado").hidden = true;
  $("#progreso").hidden = true;
  $("#generar").disabled = false;
  $("#generar").textContent = "Generar vídeo";
  $("#barra").hidden = true;
  $("#rail-etiqueta").textContent = "Duración estimada";
  $("#banco").hidden = true;
  $("#inicio").hidden = false;
  $("#pdf").value = "";
}

$("#reiniciar").addEventListener("click", reiniciar);
$("#cancelar").addEventListener("click", reiniciar);
