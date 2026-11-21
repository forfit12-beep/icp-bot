#!/usr/bin/env python3
"""
Bot de avisos de citas de Extranjería (ICP) -> Telegram.

- SOLO detecta disponibilidad. No resuelve CAPTCHA ni reserva citas.
- Configuración desde Telegram: /provincia, /tramite, /activar, /parar, /estado.
- Config persistida en config.json.

Variables de entorno:
  TELEGRAM_TOKEN      (obligatoria) token de BotFather
  TELEGRAM_CHAT_ID    (recomendada) solo ese chat podrá controlar el bot
  CONFIG_PATH         ruta del JSON (def. config.json)
  HEADLESS            1 (def.) / 0 para ver el navegador (uso local)
  WAIT_CAPTCHA_SECONDS  (solo local con HEADLESS=0) segundos que el bot espera
                        a que TÚ resuelvas el CAPTCHA a mano. 0 = no espera.
  ICP_DOC_TIPO / ICP_DOC_NUM / ICP_NOMBRE / ICP_PAIS / ICP_ANIO
                      (opcionales) tus datos para rellenar el formulario previo
  DEFAULT_PROVINCIAS  (opcional) p. ej. "Alicante,Murcia": lo que vigila al arrancar
  DEFAULT_TRAMITES    (opcional) p. ej. "huellas,nie": claves de trámite al arrancar
  PORT                puerto del servidor de salud (Render lo define solo)

Comportamiento: arranca ACTIVO y vigila sin parar (se reinicia solo si algo falla).
Telegram solo recibe un mensaje cuando aparece una cita (más las respuestas a tus
comandos). CAPTCHA, errores y demás se quedan en los logs.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import random
import re
import threading
import unicodedata
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from playwright.async_api import async_playwright

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #
# La dirección antigua (sede.administracionespublicas.gob.es/icpplus/index.html) ahora solo
# redirige a una página informativa. El formulario real con el desplegable de provincias
# está en icp.administracionelectronica.gob.es. Se puede cambiar con la variable ICP_URL.
INDEX_URL = os.getenv("ICP_URL", "https://icp.administracionelectronica.gob.es/icpplus/index.html").strip()
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")
HEADLESS = os.getenv("HEADLESS", "1") != "0"
WAIT_CAPTCHA = int(os.getenv("WAIT_CAPTCHA_SECONDS", "0") or 0)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

CYCLE_PAUSE = (60, 90)      # pausa entre ciclos completos (segundos)
COMBO_PAUSE = (6, 14)       # pausa entre comprobaciones dentro de un ciclo

# Provincia -> alias (todo normalizado: minúsculas, sin tildes)
PROVINCIAS = {
    "Álava": ["alava", "araba"], "Albacete": ["albacete"],
    "Alicante": ["alicante", "alacant"], "Almería": ["almeria"],
    "Asturias": ["asturias"], "Ávila": ["avila"], "Badajoz": ["badajoz"],
    "Barcelona": ["barcelona"], "Burgos": ["burgos"], "Cáceres": ["caceres"],
    "Cádiz": ["cadiz"], "Cantabria": ["cantabria"],
    "Castellón": ["castellon", "castello"], "Ceuta": ["ceuta"],
    "Ciudad Real": ["ciudad real"], "Córdoba": ["cordoba"],
    "A Coruña": ["a coruna", "la coruna", "coruna"], "Cuenca": ["cuenca"],
    "Girona": ["girona", "gerona"], "Granada": ["granada"],
    "Guadalajara": ["guadalajara"], "Gipuzkoa": ["gipuzkoa", "guipuzcoa"],
    "Huelva": ["huelva"], "Huesca": ["huesca"],
    "Illes Balears": ["illes balears", "baleares"], "Jaén": ["jaen"],
    "La Rioja": ["la rioja", "rioja"], "Las Palmas": ["las palmas"],
    "León": ["leon"], "Lleida": ["lleida", "lerida"], "Lugo": ["lugo"],
    "Madrid": ["madrid"], "Málaga": ["malaga"], "Melilla": ["melilla"],
    "Murcia": ["murcia"], "Navarra": ["navarra"],
    "Ourense": ["ourense", "orense"], "Palencia": ["palencia"],
    "Pontevedra": ["pontevedra"], "Salamanca": ["salamanca"],
    "Santa Cruz de Tenerife": ["santa cruz de tenerife", "tenerife"],
    "Segovia": ["segovia"], "Sevilla": ["sevilla"], "Soria": ["soria"],
    "Tarragona": ["tarragona"], "Teruel": ["teruel"], "Toledo": ["toledo"],
    "Valencia": ["valencia"], "Valladolid": ["valladolid"],
    "Bizkaia": ["bizkaia", "vizcaya"], "Zamora": ["zamora"],
    "Zaragoza": ["zaragoza"],
}

# Se excluyen Canarias, Ceuta y Melilla. "todas" = las 48 restantes.
EXCLUIDAS = {"Las Palmas", "Santa Cruz de Tenerife", "Ceuta", "Melilla"}
PROVINCIAS_VIGILADAS = [p for p in PROVINCIAS if p not in EXCLUIDAS]

# clave -> (nombre legible, palabras que deben aparecer en el texto de la opción)
TRAMITES = {
    "huellas": ("TOMA DE HUELLAS (tarjeta inicial, renovación, duplicado)", ["toma de huellas"]),
    "nie": ("ASIGNACIÓN DE NIE", ["asignacion de nie"]),
    "ue": ("CERTIFICADO DE REGISTRO DE CIUDADANO DE LA UE", ["certificado", "ciudadano"]),
    "regreso": ("AUTORIZACIÓN DE REGRESO", ["autorizacion de regreso"]),
    "recogida": ("RECOGIDA DE TARJETA", ["recogida de tarjeta"]),
    "informacion": ("INFORMACIÓN", ["informacion"]),
}
TRAMITE_SINONIMOS = {
    "huella": "huellas", "tie": "huellas", "certificado": "ue", "ue": "ue",
    "registro": "ue", "info": "informacion", "información": "informacion",
    "tarjeta": "recogida", "regreso": "regreso",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("icp-bot")
# httpx escribe la URL completa (con el token) en INFO: se silencia para no filtrarlo en los logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


# --------------------------------------------------------------------------- #
# Configuración (JSON)
# --------------------------------------------------------------------------- #
_cfg_lock = threading.Lock()
def _env_list(name: str, default: list) -> list:
    raw = os.getenv(name, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] or default


# La vigilancia arranca ACTIVA por defecto. Como el disco de Render free es
# efímero, define DEFAULT_PROVINCIAS / DEFAULT_TRAMITES como variables de entorno
# para que tras cada reinicio siga vigilando lo mismo sin tocar nada.
DEFAULT_CFG = {"chat_id": ENV_CHAT_ID,
               # por defecto: las 48 provincias (sin Canarias, Ceuta ni Melilla)
               "provincias": (PROVINCIAS_VIGILADAS
                              if norm(os.getenv("DEFAULT_PROVINCIAS", "todas")) in ("todas", "todo", "")
                              else _env_list("DEFAULT_PROVINCIAS", PROVINCIAS_VIGILADAS)),
               "tramites": _env_list("DEFAULT_TRAMITES", ["huellas"]),
               "activo": True}


def load_cfg() -> dict:
    with _cfg_lock:
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = {**DEFAULT_CFG, **json.load(f)}
        except (FileNotFoundError, json.JSONDecodeError):
            cfg = dict(DEFAULT_CFG)
        if ENV_CHAT_ID:
            cfg["chat_id"] = ENV_CHAT_ID
        return cfg


def save_cfg(cfg: dict) -> None:
    with _cfg_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
http: httpx.AsyncClient = None  # type: ignore


async def tg(method: str, **data):
    try:
        r = await http.post(f"https://api.telegram.org/bot{TOKEN}/{method}",
                            json=data, timeout=65)
        return r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Telegram %s falló: %s", method, e)
        return {}


async def send(chat_id, text: str):
    if chat_id:
        await tg("sendMessage", chat_id=chat_id, text=text,
                 disable_web_page_preview=True)


async def send_photo(chat_id, png: bytes, caption: str):
    try:
        await http.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto",
                        data={"chat_id": chat_id, "caption": caption[:1000]},
                        files={"photo": ("cita.png", png, "image/png")}, timeout=60)
    except Exception as e:  # noqa: BLE001
        log.warning("sendPhoto falló: %s", e)


def resolve_provincias(arg: str):
    if norm(arg) in ("todas", "todo", "all"):
        return list(PROVINCIAS_VIGILADAS), []   # 48: sin Canarias, Ceuta ni Melilla
    if norm(arg) in ("todas52", "todas las 52"):
        return list(PROVINCIAS), []
    ok, bad = [], []
    for part in re.split(r"[,;/]+", arg):
        p = norm(part)
        if not p:
            continue
        found = None
        for name, aliases in PROVINCIAS.items():
            if p == norm(name) or p in aliases:
                found = name
                break
        if not found:
            for name, aliases in PROVINCIAS.items():
                if any(p in a for a in aliases + [norm(name)]):
                    found = name
                    break
        (ok if found else bad).append(found or part.strip())
    return list(dict.fromkeys(ok)), bad


def resolve_tramites(arg: str):
    if norm(arg) in ("todos", "todas", "todo", "all"):
        return list(TRAMITES), []
    ok, bad = [], []
    for part in re.split(r"[,;/]+", arg):
        p = norm(part)
        if not p:
            continue
        key = p if p in TRAMITES else TRAMITE_SINONIMOS.get(p)
        (ok if key else bad).append(key or part.strip())
    return list(dict.fromkeys(ok)), bad


HELP = (
    "Comandos:\n"
    "/provincia Alicante  (varias: /provincia Alicante, Murcia · todas: /provincia todas)\n"
    "/tramite huellas  (huellas, nie, ue, regreso, recogida, informacion, todos)\n"
    "/activar · /parar · /estado\n"
)


def estado_txt(cfg: dict) -> str:
    n = len(cfg["provincias"]) * len(cfg["tramites"])
    return (f"Estado: {'🟢 ACTIVO' if cfg['activo'] else '🔴 PARADO'}\n"
            f"Provincias ({len(cfg['provincias'])}): {', '.join(cfg['provincias'][:8])}"
            f"{'…' if len(cfg['provincias']) > 8 else ''}\n"
            f"Trámites: {', '.join(cfg['tramites'])}\n"
            f"Combinaciones a vigilar: {n}")


async def handle_message(msg: dict):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    cfg = load_cfg()
    if not cfg["chat_id"]:
        await send(chat_id, f"Tu CHAT ID es: {chat_id}\n"
                            "Ponlo en la variable TELEGRAM_CHAT_ID y reinicia el bot.")
        return
    if chat_id != str(cfg["chat_id"]):
        log.warning("Mensaje ignorado de chat no autorizado %s", chat_id)
        return
    if not text.startswith("/"):
        return
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()

    if cmd in ("/start", "/ayuda", "/help"):
        await send(chat_id, "Bot de avisos de citas de Extranjería.\n" + HELP + "\n" + estado_txt(cfg))
    elif cmd == "/provincia":
        if not arg:
            await send(chat_id, "Uso: /provincia Alicante")
            return
        ok, bad = resolve_provincias(arg)
        if bad or not ok:
            await send(chat_id, f"No reconozco: {', '.join(bad) or arg}")
            return
        cfg["provincias"] = ok
        save_cfg(cfg)
        await send(chat_id, "✅ Provincias: " + (", ".join(ok) if len(ok) < 53 else ""))
    elif cmd == "/tramite":
        if not arg:
            await send(chat_id, "Uso: /tramite huellas\nOpciones: " + ", ".join(TRAMITES) + ", todos")
            return
        ok, bad = resolve_tramites(arg)
        if bad or not ok:
            await send(chat_id, f"No reconozco: {', '.join(bad) or arg}\nOpciones: " + ", ".join(TRAMITES))
            return
        cfg["tramites"] = ok
        save_cfg(cfg)
        await send(chat_id, "✅ Trámites: " + ", ".join(TRAMITES[k][0] for k in ok))
    elif cmd == "/activar":
        cfg["activo"] = True
        save_cfg(cfg)
        n = len(cfg["provincias"]) * len(cfg["tramites"])
        extra = "\n⚠️ Son muchas combinaciones: cada vuelta tardará bastante." if n > 12 else ""
        await send(chat_id, "🟢 Vigilancia activada.\n" + estado_txt(cfg) + extra)
    elif cmd == "/parar":
        cfg["activo"] = False
        save_cfg(cfg)
        await send(chat_id, "🔴 Vigilancia detenida.")
    elif cmd == "/estado":
        await send(chat_id, estado_txt(cfg))
    else:
        await send(chat_id, "Comando no reconocido.\n" + HELP)


async def telegram_loop():
    offset = 0
    while True:
        res = await tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message"])
        for upd in res.get("result", []):
            offset = upd["update_id"] + 1
            if "message" in upd:
                try:
                    await handle_message(upd["message"])
                except Exception:  # noqa: BLE001
                    log.exception("Error gestionando mensaje")
        if not res.get("ok"):
            # token erróneo, 404, sin red...: espera para no entrar en bucle rápido
            await asyncio.sleep(10)


# --------------------------------------------------------------------------- #
# Detección en la web
# --------------------------------------------------------------------------- #
async def settle(page):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=90000)
        await page.wait_for_timeout(800)
    except Exception:  # noqa: BLE001
        pass


async def visible(page, sel: str) -> bool:
    try:
        loc = page.locator(sel).first
        return await loc.count() > 0 and await loc.is_visible()
    except Exception:  # noqa: BLE001
        return False


async def accept_cookies(page):
    for sel in ("button:has-text('Aceptar')", "a:has-text('Aceptar cookies')",
                "button:has-text('Aceptar todas')"):
        try:
            if sel.startswith("button:has-text('Aceptar')") and await visible(page, "#btnAceptar"):
                return  # es el botón del formulario, no de cookies
            if await visible(page, sel):
                await page.locator(sel).first.click(timeout=2000)
                return
        except Exception:  # noqa: BLE001
            pass


async def select_by_text(page, predicate) -> bool:
    """Busca, entre todos los <select>, la primera opción cuyo texto cumpla predicate."""
    selects = page.locator("select")
    for i in range(await selects.count()):
        opts = await selects.nth(i).evaluate(
            "el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))")
        for o in opts:
            if o["v"] and predicate(norm(o["t"])):
                await selects.nth(i).select_option(value=o["v"])
                return True
    return False


def prov_pred(prov: str):
    names = [norm(prov)] + PROVINCIAS[prov]

    def f(t: str) -> bool:
        return t in names or any(t == n or t.startswith(n + " ") for n in names)
    return f


def tram_pred(key: str):
    kws = TRAMITES[key][1]
    return lambda t: all(k in t for k in kws)


async def click_first(page, sels) -> bool:
    for s in sels:
        if await visible(page, s):
            await page.locator(s).first.click()
            await settle(page)
            return True
    return False


async def fill_personal_data(page):
    doc, num = os.getenv("ICP_DOC_TIPO", "NIE").upper(), os.getenv("ICP_DOC_NUM")
    if not num:
        return
    try:
        radio = {"NIE": "#rdbTipoDocNie", "PASAPORTE": "#rdbTipoDocPas", "DNI": "#rdbTipoDocDni"}.get(doc)
        if radio and await visible(page, radio):
            await page.locator(radio).check()
        await page.locator("#txtIdCitado").fill(num)
        for sel, env in (("#txtDesCitado", "ICP_NOMBRE"), ("#txtAnnoCitado", "ICP_ANIO")):
            if os.getenv(env) and await visible(page, sel):
                await page.locator(sel).fill(os.getenv(env))
        if os.getenv("ICP_PAIS") and await visible(page, "#txtPaisNac"):
            await select_by_text(page, lambda t: norm(os.getenv("ICP_PAIS", "")) in t)
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo rellenar el formulario: %s", e)


DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
HOUR_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")


async def classify(page):
    """Devuelve (estado, detalles). Estados: NONE, AVAILABLE, CAPTCHA, UNKNOWN."""
    try:
        text = await page.inner_text("body")
        html = (await page.content()).lower()
    except Exception:  # noqa: BLE001
        return "UNKNOWN", {}
    t = norm(text)
    if "no hay citas disponibles" in t:
        return "NONE", {}

    markers = ("ui-datepicker", "citamap", "seleccione la oficina",
               "seleccione una de las siguientes citas", "huecos libres", "id=\"idsede\"")
    has_calendar = any(m in html for m in markers)
    if has_calendar:
        offices = []
        try:
            offices = await page.evaluate(
                "() => Array.from(document.querySelectorAll('select#idSede option'))"
                ".filter(o => o.value).map(o => o.text.trim())")
        except Exception:  # noqa: BLE001
            pass
        dates = list(dict.fromkeys(DATE_RE.findall(text)))[:8]
        hours = list(dict.fromkeys(m.group(0) for m in HOUR_RE.finditer(text)))[:8]
        return "AVAILABLE", {"oficinas": offices, "fechas": dates, "horas": hours}

    if "captcha" in html or "recaptcha" in html:
        return "CAPTCHA", {}
    return "UNKNOWN", {}


async def check_combo(browser, prov: str, tram: str):
    ctx = await browser.new_context(locale="es-ES", user_agent=UA,
                                    viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    page.set_default_timeout(90000)
    await page.route("**/*", lambda r: r.abort()
                     if r.request.resource_type in ("media", "font") else r.continue_())
    try:
        await page.goto(INDEX_URL, wait_until="domcontentloaded")
        await settle(page)
        await accept_cookies(page)
        if not await select_by_text(page, prov_pred(prov)):
            try:
                snippet = norm(await page.inner_text("body"))[:300]
                title = await page.title()
            except Exception:  # noqa: BLE001
                snippet, title = "", ""
            return "UNKNOWN", {"motivo": (f"provincia '{prov}' no encontrada en el desplegable | "
                                          f"url={page.url} | titulo={title} | texto={snippet}")}
        await click_first(page, ["#btnAceptar", "input[value='Aceptar']"])

        if not await select_by_text(page, tram_pred(tram)):
            return "UNKNOWN", {"motivo": f"trámite '{tram}' no ofrecido en {prov}"}
        await click_first(page, ["#btnAceptar", "input[value='Aceptar']"])

        waited = False
        for _ in range(6):
            status, det = await classify(page)
            if status in ("NONE", "AVAILABLE"):
                if status == "AVAILABLE":
                    det["png"] = await page.screenshot(full_page=True)
                return status, det
            if await click_first(page, ["#btnEntrar"]):
                continue
            if await visible(page, "#txtIdCitado"):
                await fill_personal_data(page)
                if status == "CAPTCHA":
                    if WAIT_CAPTCHA and not HEADLESS and not waited:
                        waited = True
                        log.info("Esperando %ss a que resuelvas el CAPTCHA a mano...", WAIT_CAPTCHA)
                        try:
                            await page.locator("#txtIdCitado").wait_for(
                                state="hidden", timeout=WAIT_CAPTCHA * 1000)
                            await settle(page)
                            continue
                        except Exception:  # noqa: BLE001
                            return "CAPTCHA", {}
                    return "CAPTCHA", {}
                await click_first(page, ["#btnAceptar", "#btnEnviar"])
                continue
            if await click_first(page, ["#btnEnviar", "#btnSiguiente"]):
                continue
            break
        status, det = await classify(page)
        if status == "UNKNOWN":
            det["motivo"] = "página no reconocida (¿cambió la web?)"
        if status == "AVAILABLE":
            det["png"] = await page.screenshot(full_page=True)
        return status, det
    finally:
        await ctx.close()


# --------------------------------------------------------------------------- #
# Bucle de vigilancia
# --------------------------------------------------------------------------- #
last_status: dict = {}  # solo NONE / AVAILABLE; errores y CAPTCHA no lo modifican


async def notify_available(chat_id, prov, tram, det):
    oficinas = ", ".join(det.get("oficinas") or []) or "No detectada (mira la web)"
    fechas = ", ".join(det.get("fechas") or []) or "No detectada"
    horas = ", ".join(det.get("horas") or [])
    cuando = fechas + (f" · {horas}" if horas else "")
    msg = (f"🟢 ¡POSIBLE HUECO DE CITA!\n"
           f"Provincia: {prov}\nOficina: {oficinas}\nTrámite: {TRAMITES[tram][0]}\n"
           f"Fecha/hora detectada: {cuando}\n"
           f"Enlace: {INDEX_URL}\n"
           f"(Entra rápido y elige provincia + trámite; los huecos duran minutos.)")
    if det.get("png"):
        await send_photo(chat_id, det["png"], msg)
    else:
        await send(chat_id, msg)


async def scanner_loop():
    async with async_playwright() as pw:
        browser = None
        while True:
            cfg = load_cfg()
            if not cfg["activo"] or not cfg["chat_id"]:
                await asyncio.sleep(5)
                continue
            combos = [(p, t) for p in cfg["provincias"] for t in cfg["tramites"]]
            log.info("Nuevo ciclo: %d combinaciones", len(combos))
            for prov, tram in combos:
                if not load_cfg()["activo"]:
                    break
                try:
                    if browser is None or not browser.is_connected():
                        browser = await pw.chromium.launch(
                            headless=HEADLESS,
                            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
                    status, det = await check_combo(browser, prov, tram)
                except Exception as e:  # noqa: BLE001
                    log.error("Error comprobando %s/%s: %s", prov, tram, e)
                    status, det = "ERROR", {}
                    try:
                        if browser:
                            await browser.close()
                    except Exception:  # noqa: BLE001
                        pass
                    browser = None

                key = (prov, tram)
                log.info("%-22s %-12s -> %s %s", prov, tram, status,
                         det.get("motivo", "") if status == "UNKNOWN" else "")
                # Telegram SOLO cuando aparece una cita (paso de "sin citas" a "hay cita").
                # CAPTCHA / errores / páginas desconocidas solo van al log.
                if status == "AVAILABLE":
                    if last_status.get(key) != "AVAILABLE":
                        await notify_available(cfg["chat_id"], prov, tram, det)
                    last_status[key] = "AVAILABLE"
                elif status == "NONE":
                    last_status[key] = "NONE"
                elif status == "CAPTCHA":
                    log.warning("CAPTCHA en %s/%s: el bot no lo resuelve; no se puede comprobar", prov, tram)
                await asyncio.sleep(random.uniform(*COMBO_PAUSE))
            pause = random.uniform(*CYCLE_PAUSE)
            log.info("Ciclo terminado. Pausa de %.0fs", pause)
            await asyncio.sleep(pause)


# --------------------------------------------------------------------------- #
# Servidor de salud (Render free necesita un puerto abierto)
# --------------------------------------------------------------------------- #
class Health(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # silencio
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    srv = HTTPServer(("0.0.0.0", port), Health)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Servidor de salud en puerto %d", port)


async def forever(fn, name: str):
    """Reinicia la tarea si falla, para que la vigilancia no se detenga nunca."""
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("La tarea '%s' falló; reiniciando en 15s", name)
            await asyncio.sleep(15)


async def main():
    global http
    if not TOKEN:
        raise SystemExit("Falta la variable de entorno TELEGRAM_TOKEN")
    start_health_server()
    http = httpx.AsyncClient()
    me = await tg("getMe")
    log.info("Bot conectado: %s", me.get("result", {}).get("username"))
    log.info("Vigilancia %s | %s", "ACTIVA" if load_cfg()["activo"] else "PARADA",
             estado_txt(load_cfg()).replace("\n", " | "))
    await asyncio.gather(forever(telegram_loop, "telegram"),
                         forever(scanner_loop, "scanner"))


if __name__ == "__main__":
    asyncio.run(main())
