#!/usr/bin/env python3
"""
Bot de avisos de citas de Extranjeria (ICP) -> Telegram.
- SOLO detecta disponibilidad. No resuelve CAPTCHA ni reserva citas.
- Configuracion desde Telegram: /provincia, /tramite, /activar, /parar, /estado.
- Config persistida en config.json.
- SOLO 6 provincias: Navarra, La Rioja, Castellon, Madrid, Barcelona, Valladolid
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

INDEX_URL = os.getenv("ICP_URL", "https://icp.administracionelectronica.gob.es/icpplus/index.html").strip()
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ENV_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
CONFIG_PATH = os.getenv("CONFIG_PATH", "config.json")
HEADLESS = os.getenv("HEADLESS", "1") != "0"
WAIT_CAPTCHA = int(os.getenv("WAIT_CAPTCHA_SECONDS", "0") or 0)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CYCLE_PAUSE = (60, 90)
COMBO_PAUSE = (6, 14)

PROVINCIAS = {
    "Alava": ["alava", "araba"], "Albacete": ["albacete"],
    "Alicante": ["alicante", "alacant"], "Almeria": ["almeria"],
    "Asturias": ["asturias"], "Avila": ["avila"], "Badajoz": ["badajoz"],
    "Barcelona": ["barcelona"], "Burgos": ["burgos"], "Caceres": ["caceres"],
    "Cadiz": ["cadiz"], "Cantabria": ["cantabria"],
    "Castellon": ["castellon", "castello"], "Ceuta": ["ceuta"],
    "Ciudad Real": ["ciudad real"], "Cordoba": ["cordoba"],
    "A Coruna": ["a coruna", "la coruna", "coruna"], "Cuenca": ["cuenca"],
    "Girona": ["girona", "gerona"], "Granada": ["granada"],
    "Guadalajara": ["guadalajara"], "Gipuzkoa": ["gipuzkoa", "guipuzcoa"],
    "Huelva": ["huelva"], "Huesca": ["huesca"],
    "Illes Balears": ["illes balears", "baleares"], "Jaen": ["jaen"],
    "La Rioja": ["la rioja", "rioja"], "Las Palmas": ["las palmas"],
    "Leon": ["leon"], "Lleida": ["lleida", "lerida"], "Lugo": ["lugo"],
    "Madrid": ["madrid"], "Malaga": ["malaga"], "Melilla": ["melilla"],
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
# SOLO TUS 6 PROVINCIAS
MIS_PROVINCIAS = ["Navarra", "La Rioja", "Castellon", "Madrid", "Barcelona", "Valladolid"]
EXCLUIDAS = {"Las Palmas", "Santa Cruz de Tenerife", "Ceuta", "Melilla"}
PROVINCIAS_VIGILADAS = MIS_PROVINCIAS
TRAMITES = {
    "huellas": ("TOMA DE HUELLAS (tarjeta inicial, renovacion, duplicado)", ["toma de huellas"]),
    "nie": ("ASIGNACION DE NIE", ["asignacion de nie"]),
    "ue": ("CERTIFICADO DE REGISTRO DE CIUDADANO DE LA UE", ["certificado", "ciudadano"]),
    "regreso": ("AUTORIZACION DE REGRESO", ["autorizacion de regreso"]),
    "recogida": ("RECOGIDA DE TARJETA", ["recogida de tarjeta"]),
    "informacion": ("INFORMACION", ["informacion"]),
}
TRAMITE_SINONIMOS = {
    "huella": "huellas", "tie": "huellas", "certificado": "ue", "ue": "ue",
    "registro": "ue", "info": "informacion", "informacion": "informacion",
    "tarjeta": "recogida", "regreso": "regreso",
}
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("icp-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()

_cfg_lock = threading.Lock()
def _env_list(name: str, default: list) -> list:
    raw = os.getenv(name, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] or default

DEFAULT_CFG = {"chat_id": ENV_CHAT_ID,
               "provincias": list(MIS_PROVINCIAS),
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
        # Forzar siempre solo tus 6 provincias, aunque config.json viejo tenga 48
        # Si quieres cambiar desde Telegram con /provincia, eso si se respeta en la sesion,
        # pero al arrancar vuelve a tus 6 si el config esta vacio o es viejo.
        # Para que no vuelva a 48 nunca, dejamos esta proteccion:
        if not cfg.get("provincias") or len(cfg.get("provincias", [])) > 6:
            # Solo lo corregimos si venia del config viejo con 48. Si tu pusiste mas de 6 a mano, lo dejamos.
            # Aqui lo dejamos en tus 6 para que no vuelva a vigilar toda Espana.
            cfg["provincias"] = list(MIS_PROVINCIAS)
            # No guardamos aqui para no pisar un cambio que hagas con /provincia en caliente
        return cfg

def save_cfg(cfg: dict) -> None:
    with _cfg_lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)

http: httpx.AsyncClient = None

async def tg(method: str, **data):
    try:
        r = await http.post(f"https://api.telegram.org/bot{TOKEN}/{method}", json=data, timeout=65)
        return r.json()
    except Exception as e:
        log.warning("Telegram %s fallo: %s", method, e)
        return {}

async def send(chat_id, text: str):
    if chat_id:
        await tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True)

async def send_photo(chat_id, png: bytes, caption: str):
    try:
        await http.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data={"chat_id": chat_id, "caption": caption[:1000]}, files={"photo": ("cita.png", png, "image/png")}, timeout=60)
    except Exception as e:
        log.warning("sendPhoto fallo: %s", e)

def resolve_provincias(arg: str):
    if norm(arg) in ("todas", "todo", "all"):
        return list(MIS_PROVINCIAS), []
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

HELP = ("Comandos:\n/provincia Navarra, La Rioja, Castellon, Madrid, Barcelona, Valladolid\n/tramite huellas  (huellas, nie, ue, regreso, recogida, informacion, todos)\n/activar - /parar - /estado\n")

def estado_txt(cfg: dict) -> str:
    n = len(cfg["provincias"]) * len(cfg["tramites"])
    # AHORA SI CON CIRCULO VERDE / ROJO
    icono = "🟢" if cfg["activo"] else "🔴"
    estado = "ACTIVO" if cfg["activo"] else "PARADO"
    return (f"Estado: {icono} {estado}\nProvincias ({len(cfg['provincias'])}): {', '.join(cfg['provincias'])}\nTramites: {', '.join(cfg['tramites'])}\nCombinaciones a vigilar: {n}")

async def handle_message(msg: dict):
    chat_id = str(msg["chat"]["id"])
    text = (msg.get("text") or "").strip()
    cfg = load_cfg()
    if not cfg["chat_id"]:
        await send(chat_id, f"Tu CHAT ID es: {chat_id}\nPonlo en la variable TELEGRAM_CHAT_ID y reinicia el bot.")
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
        await send(chat_id, "Bot de avisos de citas de Extranjeria.\n" + HELP + "\n" + estado_txt(cfg))
    elif cmd == "/provincia":
        if not arg:
            await send(chat_id, "Uso: /provincia Navarra, La Rioja, Castellon, Madrid, Barcelona, Valladolid")
            return
        ok, bad = resolve_provincias(arg)
        if bad or not ok:
            await send(chat_id, f"No reconozco: {', '.join(bad) or arg}")
            return
        cfg["provincias"] = ok
        save_cfg(cfg)
        await send(chat_id, "Provincias: " + (", ".join(ok) if len(ok) < 53 else ""))
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
        await send(chat_id, "Tramites: " + ", ".join(TRAMITES[k][0] for k in ok))
    elif cmd == "/activar":
        cfg["activo"] = True
        save_cfg(cfg)
        await send(chat_id, "Vigilancia activada.\n" + estado_txt(cfg))
    elif cmd == "/parar":
        cfg["activo"] = False
        save_cfg(cfg)
        await send(chat_id, "Vigilancia detenida.")
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
                except Exception:
                    log.exception("Error gestionando mensaje")
        if not res.get("ok"):
            await asyncio.sleep(10)

async def settle(page):
    try:
        await page.wait_for_load_state("commit", timeout=90000)
        await page.wait_for_timeout(800)
    except Exception:
        pass

async def visible(page, sel: str) -> bool:
    try:
        loc = page.locator(sel).first
        return await loc.count() > 0 and await loc.is_visible()
    except Exception:
        return False

async def accept_cookies(page):
    for sel in ("button:has-text('Aceptar')", "a:has-text('Aceptar cookies')", "button:has-text('Aceptar todas')"):
        try:
            if sel.startswith("button:has-text('Aceptar')") and await visible(page, "#btnAceptar"):
                return
            if await visible(page, sel):
                await page.locator(sel).first.click(timeout=2000)
                return
        except Exception:
            pass

async def select_by_text(page, predicate) -> bool:
    selects = page.locator("select")
    for i in range(await selects.count()):
        opts = await selects.nth(i).evaluate("el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))")
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
    except Exception as e:
        log.warning("No se pudo rellenar el formulario: %s", e)

DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
HOUR_RE = re.compile(r"\b([01]?\d|2[0-3]):[0-5]\d\b")

async def classify(page):
    try:
        text = await page.inner_text("body")
        html = (await page.content()).lower()
    except Exception:
        return "UNKNOWN", {}
    t = norm(text)
    if "no hay citas disponibles" in t:
        return "NONE", {}
    markers = ("ui-datepicker", "citamap", "seleccione la oficina", "seleccione una de las siguientes citas", "huecos libres", 'id="idsede"')
    has_calendar = any(m in html for m in markers)
    if has_calendar:
        offices = []
        try:
            offices = await page.evaluate("() => Array.from(document.querySelectorAll('select#idSede option')).filter(o => o.value).map(o => o.text.trim())")
        except Exception:
            pass
        dates = list(dict.fromkeys(DATE_RE.findall(text)))[:8]
        hours = list(dict.fromkeys(m.group(0) for m in HOUR_RE.finditer(text)))[:8]
        return "AVAILABLE", {"oficinas": offices, "fechas": dates, "horas": hours}
    if "captcha" in html or "recaptcha" in html:
        return "CAPTCHA", {}
    return "UNKNOWN", {}

async def check_combo(browser, prov: str, tram: str):
    ctx = await browser.new_context(locale="es-ES", user_agent=UA, viewport={"width": 1280, "height": 900})
    page = await ctx.new_page()
    page.set_default_timeout(90000)
    await page.route("**/*", lambda r: r.abort() if r.request.resource_type in ("media", "font") else r.continue_())
    try:
        _goto_ok = False
        for _intento in range(3):
            try:
                await page.goto(INDEX_URL, wait_until="domcontentloaded", timeout=60000)
                _goto_ok = True
                break
            except Exception:
                if _intento == 2: print(f"{prov} no responde, sigo con la siguiente"); return ("UNKNOWN", {"motivo": "goto timeout"})
                await page.wait_for_timeout(5000)
        assert _goto_ok
        await settle(page)
        await accept_cookies(page)
        if not await select_by_text(page, prov_pred(prov)):
            try:
                snippet = norm(await page.inner_text("body"))[:300]
                title = await page.title()
            except Exception:
                snippet, title = "", ""
            return "UNKNOWN", {"motivo": f"provincia '{prov}' no encontrada | url={page.url} | titulo={title} | texto={snippet}"}
        await click_first(page, ["#btnAceptar", "input[value='Aceptar']"])
        if not await select_by_text(page, tram_pred(tram)):
            return "UNKNOWN", {"motivo": f"tramite '{tram}' no ofrecido en {prov}"}
        await click_first(page, ["#btnAceptar", "input[value='Aceptar']"])
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
                await click_first(page, ["#btnEntrar"])
                continue
            if status == "CAPTCHA":
                if WAIT_CAPTCHA:
                    await page.wait_for_timeout(WAIT_CAPTCHA * 1000)
                    continue
                return "CAPTCHA", {}
            await page.wait_for_timeout(2000)
        return "UNKNOWN", {"motivo": "sin resultado tras intentos"}
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

async def watch_loop():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=HEADLESS)
        try:
            while True:
                cfg = load_cfg()
                if not cfg["activo"]:
                    await asyncio.sleep(10)
                    continue
                for prov in cfg["provincias"]:
                    for tram in cfg["tramites"]:
                        cfg2 = load_cfg()
                        if not cfg2["activo"]:
                            break
                        try:
                            status, det = await check_combo(browser, prov, tram)
                            log.info("Comprobando %s/%s: %s", prov, tram, status)
                            if status == "AVAILABLE":
                                txt = f"🟢 CITA DISPONIBLE en {prov} - {TRAMITES[tram][0]}\nFechas: {', '.join(det.get('fechas', []))}\nHoras: {', '.join(det.get('horas', []))}\nOficinas: {', '.join(det.get('oficinas', [])[:5])}"
                                if cfg2["chat_id"]:
                                    await send(cfg2["chat_id"], txt)
                                    if det.get("png"):
                                        await send_photo(cfg2["chat_id"], det["png"], txt)
                        except Exception:
                            log.exception("Error comprobando %s/%s", prov, tram)
                        await asyncio.sleep(random.uniform(*COMBO_PAUSE))
                await asyncio.sleep(random.uniform(*CYCLE_PAUSE))
        finally:
            try:
                await browser.close()
            except Exception:
                pass

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a):
        pass

def start_health():
    port = int(os.getenv("PORT", "10000"))
    try:
        HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()
    except Exception as e:
        log.warning("Health server fallo: %s", e)

async def main():
    global http
    http = httpx.AsyncClient()
    threading.Thread(target=start_health, daemon=True).start()
    # Al arrancar, forzar config a tus 6 provincias si el config.json es viejo
    try:
        cfg0 = load_cfg()
        # Si venia con 48, lo dejamos en tus 6 y lo guardamos
        if len(cfg0.get("provincias", [])) != 6:
            cfg0["provincias"] = list(MIS_PROVINCIAS)
            save_cfg(cfg0)
            log.info("Provincias: 6")
    except Exception as e:
        log.warning("Error provincias: %s", e)
        async def main():
    global http
    http = httpx.AsyncClient()
    threading.Thread(
        target=start_health,
        daemon=True
    ).start()
    cfg0 = load_cfg()
    cfg0["provincias"] = list(MIS_PROVINCIAS)
    save_cfg(cfg0)
    await asyncio.gather(
        telegram_loop(),
        watch_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
        log.warning("Error provincias: %s", e)
    await asyncio.gather(
        telegram_loop(),
        watch_loop()
    )

if __name__ == "__main__":
    asyncio.run(main())
