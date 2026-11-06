import hashlib
import json
import os
import re
import smtplib
import sys
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader
from io import BytesIO

MAIN_URL = "https://www.dian.gov.co/dian/ventasremates/Paginas/Donaciones.aspx"
ADJUDICADOS_URL = "https://www.dian.gov.co/dian/ventasremates/Paginas/Ofrecimiento-donaciones.aspx"
STATE_FILE = Path("data/seen.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; DonacionesDIANMonitor/1.0; +https://github.com)"}
TIMEOUT = 25
MAX_DOC_BYTES = 12 * 1024 * 1024
ALLOWED_HOSTS = {"www.dian.gov.co", "dian.gov.co"}

def clean_url(url):
    url, _ = urldefrag(url)
    return url.strip()

def fetch(url):
    response = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    return response

def get_page_links(url):
    response = fetch(url)
    soup = BeautifulSoup(response.content, "html.parser")
    found = {}
    for a in soup.find_all("a", href=True):
        href = clean_url(urljoin(url, a["href"]))
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in ALLOWED_HOSTS:
            continue
        text = " ".join(a.get_text(" ", strip=True).split()) or Path(parsed.path).name or href
        low = (text + " " + parsed.path).lower()
        is_document = bool(re.search(r"\.(pdf|docx?|xlsx?|pptx?)(?:$|[?#])", parsed.path, re.I))
        is_relevant = is_document or any(word in low for word in (
            "ofrecimiento", "donacion", "donación", "resolucion", "resolución",
            "primer ofrecimiento", "segundo ofrecimiento"
        ))
        if is_relevant:
            found[href] = {"title": text[:240], "url": href}
    return soup, found

def get_pdf_text(url):
    try:
        with requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True) as r:
            r.raise_for_status()
            content = r.raw.read(MAX_DOC_BYTES + 1)
        if len(content) > MAX_DOC_BYTES or not content.startswith(b"%PDF"):
            return ""
        reader = PdfReader(BytesIO(content))
        pieces = []
        for page in reader.pages[:12]:
            pieces.append(page.extract_text() or "")
        return re.sub(r"\s+", " ", " ".join(pieces)).strip()[:4500]
    except Exception as exc:
        print(f"No se pudo leer documento {url}: {exc}")
        return ""

def scan():
    items = {}
    pages = [(MAIN_URL, "Mercancías aprehendidas, decomisadas o abandonadas"),
             (ADJUDICADOS_URL, "Bienes adjudicados a la Nación")]
    for page_url, category in pages:
        soup, links = get_page_links(page_url)
        # Captura documentos y enlaces publicados dentro de las páginas oficiales.
        for url, item in links.items():
            if url in (MAIN_URL, ADJUDICADOS_URL):
                continue
            item["category"] = category
            item["source_page"] = page_url
            item["text"] = ""
            if url.lower().split("?")[0].endswith(".pdf"):
                item["text"] = get_pdf_text(url)
            fingerprint_source = url + "|" + item["title"]
            item["id"] = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
            items[item["id"]] = item

        # La página principal puede tener listados SharePoint con enlaces renderizados en otros elementos.
        # Añade enlaces a páginas DIAN relacionadas con ofrecimientos, incluso si no tienen extensión documental.
        for a in soup.find_all("a", href=True):
            href = clean_url(urljoin(page_url, a["href"]))
            if href in (MAIN_URL, ADJUDICADOS_URL):
                continue
            label = " ".join(a.get_text(" ", strip=True).split())
            if href.startswith("https://www.dian.gov.co/") and any(k in (href + label).lower() for k in ("donacion", "donación", "ofrecimiento")):
                key = hashlib.sha256((href + "|" + label).encode()).hexdigest()
                if key not in items:
                    items[key] = {"id": key, "title": label or href, "url": href,
                                  "category": category, "source_page": page_url, "text": ""}
    return items

def load_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return {"initialized": False, "seen": []}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"initialized": False, "seen": []}

def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise RuntimeError("Falta configurar TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en GitHub Secrets.")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(url, json={"chat_id": chat_id, "text": message, "disable_web_page_preview": False}, timeout=TIMEOUT)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram no confirmó el envío.")

def send_email(subject, body):
    password = os.getenv("GMAIL_APP_PASSWORD", "").strip().replace(" ", "")
    recipient = os.getenv("ALERT_EMAIL", "").strip()
    sender = "katherinecabreraahi@gmail.com"
    if not password:
        raise RuntimeError("Falta configurar GMAIL_APP_PASSWORD en GitHub Secrets.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=TIMEOUT) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)

def format_alert(item):
    text = item.get("text") or "No se pudo extraer automáticamente el contenido del documento. Abre el enlace para revisar los bienes, cantidades y condiciones."
    return (
        "🔔 NUEVA PUBLICACIÓN DE DONACIÓN – DIAN\n\n"
        f"Tipo: {item.get('category', 'Ofrecimiento DIAN')}\n"
        f"Título: {item.get('title', 'Sin título')}\n\n"
        f"Información del documento:\n{text[:3000]}\n\n"
        f"Documento/publicación: {item['url']}\n"
        f"Página DIAN: {item.get('source_page', MAIN_URL)}\n\n"
        "Importante: verifica el plazo, requisitos y destinatarios directamente en el acto oficial. La página DIAN señala que estos ofrecimientos se dirigen a entidades públicas y/o Fuerza Pública, según la modalidad."
    )

def main():
    state = load_state()
    items = scan()
    if not items:
        raise RuntimeError("La revisión no encontró enlaces verificables. No se modifica el registro para evitar perder publicaciones.")
    current_ids = set(items)
    previous = set(state.get("seen", []))
    if not state.get("initialized"):
        state = {"initialized": True, "seen": sorted(current_ids)}
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Primera ejecución: se guardaron {len(current_ids)} publicaciones existentes como línea base. No se envían alertas antiguas.")
        return

    new_ids = sorted(current_ids - previous)
    errors = []
    sent_ids = []
    for item_id in new_ids:
        item = items[item_id]
        message = format_alert(item)
        subject = "Nueva donación DIAN: " + item.get("title", "Publicación nueva")[:120]
        # Envía por ambos canales; registra el elemento solo si ambos envíos se completan.
        try:
            send_telegram(message)
            send_email(subject, message)
            sent_ids.append(item_id)
            print(f"Alerta enviada: {item.get('title')}")
        except Exception as exc:
            errors.append(f"{item.get('title')}: {exc}")
            print(f"ERROR de notificación: {exc}")

    # Solo registrar nuevos ítems notificados correctamente. Si falla un canal, se reintenta en la próxima ejecución.
    updated = previous | set(sent_ids)
    state = {"initialized": True, "seen": sorted(updated)}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    if errors:
        raise RuntimeError("No se pudieron enviar todas las alertas: " + " | ".join(errors))
    print(f"Revisión terminada. Nuevas publicaciones: {len(new_ids)}; alertas enviadas: {len(sent_ids)}.")

if __name__ == "__main__":
    main()
