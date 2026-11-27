import os
import time
import random
import requests
from playwright.sync_api import sync_playwright

# ================= CONFIGURACIÓN =================
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

DATOS_USUARIO = {
    "tipo_doc": "NIE",  # "NIE" o "PASAPORTE"
    "documento": "Y1234567X",
    "nombre_completo": "NOMBRE APELLIDO1 APELLIDO2",
    "anio_nacimiento": "1990",
    "pais_nacionalidad": "COLOMBIA"  # En mayúsculas tal como sale en la web
}

PROVINCIAS = [
    "Navarra",
    "La Rioja",
    "Valladolid",
    "Castellón",
    "Barcelona"
]

TRAMITE_OBJETIVO = "TOMA DE HUELLAS"
URL_CITA = "https://icp.administracionelectronica.gob.es/icpplus/index.html"
# =================================================

def enviar_telegram(mensaje):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"},
            timeout=15
        )
    except Exception as e:
        print("Error Telegram:", e)

def comprobar_provincia(page, provincia):
    print(f"Revisando {provincia}...")
    try:
        page.goto(URL_CITA, timeout=30000)
        page.wait_for_load_state("networkidle")

        select_prov = page.locator("select#form")
        opciones = select_prov.locator("option").all_inner_texts()
        opcion_exacta = next((op for op in opciones if provincia.lower() in op.lower()), None)
        if not opcion_exacta:
            print(f"No encontrada: {provincia}")
            return

        select_prov.select_option(label=opcion_exacta)
        page.locator("input#btnAceptar").click()
        page.wait_for_load_state("networkidle")
        time.sleep(random.uniform(1.5, 3))

        tramite_ok = False
        for s in page.locator("select").all():
            ops = s.locator("option").all_inner_texts()
            match = next((op for op in ops if TRAMITE_OBJETIVO in op.upper()), None)
            if match:
                s.select_option(label=match)
                tramite_ok = True
                break

        if not tramite_ok:
            print(f"Sin trámite en {provincia}")
            return

        page.locator("input#btnAceptar").click()
        page.wait_for_load_state("networkidle")
        time.sleep(random.uniform(1.5, 3))

        if page.locator("input#btnEntrar").is_visible():
            page.locator("input#btnEntrar").click()
            page.wait_for_load_state("networkidle")
            time.sleep(random.uniform(1.5, 3))

        if DATOS_USUARIO["tipo_doc"] == "PASAPORTE":
            page.locator("input#rdbTipoDocPas").check()
        else:
            page.locator("input#rdbTipoDocNie").check()

        page.locator("input#txtIdCitado").fill(DATOS_USUARIO["documento"])
        page.locator("input#txtDesCitado").fill(DATOS_USUARIO["nombre_completo"])

        if page.locator("input#txtAnnoCitado").is_visible():
            page.locator("input#txtAnnoCitado").fill(DATOS_USUARIO["anio_nacimiento"])

        if page.locator("select#txtPaisNac").is_visible():
            page.locator("select#txtPaisNac").select_option(label=DATOS_USUARIO["pais_nacionalidad"])

        page.locator("input#btnEnviar").click()
        page.wait_for_load_state("networkidle")
        time.sleep(random.uniform(2, 4))

        btn = page.locator("input#btnConsultar")
        if not btn.is_visible():
            btn = page.get_by_role("button", name="Solicitar Cita")
        if btn.is_visible():
            btn.click()
            page.wait_for_load_state("networkidle")
            time.sleep(2)

        html = page.content().lower()
        if "en este momento no hay citas disponibles" in html:
            print(f"{provincia}: Sin citas.")
        elif "se ha producido un error" in html or "too many requests" in html:
            print(f"{provincia}: Bloqueo o error temporal.")
        else:
            enviar_telegram(
                f"🚨 *¡POSIBLE CITA DE HUELLAS!* 🚨\n\n"
                f"📍 *Provincia:* {provincia}\n"
                f"🔗 [Entrar rápido a la web]({URL_CITA})"
            )
            print(f"¡CITA DETECTADA en {provincia}!")

    except Exception as e:
        print(f"Error en {provincia}: {e}")

def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        for prov in PROVINCIAS:
            comprobar_provincia(page, prov)
            time.sleep(random.uniform(6, 12))
        browser.close()

if __name__ == "__main__":
    main()
          
