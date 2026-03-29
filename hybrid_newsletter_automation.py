import os
# Evitar errores de cabeceras técnicas en GitHub Actions
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"

import urllib3
# Silenciar avisos de seguridad de webs con certificados antiguos
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
import google.generativeai as genai
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup
import hashlib
import json

# --- CONFIGURACIÓN PARA GEMINI 2.5 / 3 ---
SOURCES_FILE = 'sources.txt'
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
modelo_final = genai.GenerativeModel("gemini-2.5-flash")

def get_sources(file_path):
    rss_feeds, web_urls = [], []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if line.lower().startswith('rss:'): rss_feeds.append(line[4:].strip())
                    elif line.lower().startswith('web:'): web_urls.append(line[4:].strip())
    except Exception: pass
    return rss_feeds, web_urls

def parse_rss_feed(feed_url):
    events = []
    # No filtramos aquí por fecha de publicación, dejamos que Gemini decida la vigencia
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            fecha_pub = None
            if hasattr(entry, 'published_parsed'):
                fecha_pub = datetime(*entry.published_parsed[:6])
            
            events.append({
                'title': getattr(entry, 'title', 'Sin título'),
                'link': getattr(entry, 'link', 'Sin enlace'),
                'summary': getattr(entry, 'summary', 'Sin descripción'),
                'date_pub': fecha_pub,
                'source': feed_url
            })
    except: pass
    return events

def scrape_web_with_gemini(url):
    events = []
    hoy = datetime.now().strftime('%Y-%m-%d')
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=15, verify=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for s in soup(["script", "style"]): s.extract()
        clean_text = " ".join(soup.get_text().split())[:15000]
        
        prompt = f"""Analiza este texto de una web cultural de Málaga. Hoy es {hoy}.
Extrae los eventos que CUMPLAN ALGUNA de estas condiciones:
1. Eventos que ocurrirán en el futuro.
2. Eventos o exposiciones que ya han comenzado pero que SIGUEN VIGENTES hoy (tienen una duración de varios días/meses).

Devuelve un JSON: [{{'title': '...', 'date_info': '...', 'summary': '...', 'link': '...'}}]
En 'date_info', indica el rango de fechas o la fecha específica.
"""
        
        res = modelo_final.generate_content(prompt)
        data = json.loads(res.text.replace('```json', '').replace('```', '').strip())
        for e in data:
            e['source'], e['type'] = url, 'web'
            events.append(e)
    except: pass
    return events

def generate_hash(e):
    # Generar un hash basado en el título para evitar duplicados exactos
    return hashlib.md5(e['title'].encode()).hexdigest()

def summarize_and_order_events_with_gemini(all_events):
    if not all_events: return "<p>No se han encontrado eventos vigentes esta semana.</p>"
    
    hoy_str = datetime.now().strftime('%d/%m/%Y')
    txt = ""
    for e in all_events:
        date_info = e.get('date_info', e.get('date_pub', 'Consultar web'))
        txt += f"- Evento: {e['title']} | Fechas: {date_info} | Fuente: {e['source']} | Resumen: {e['summary'][:150]}... | Link: {e['link']}\n"

    prompt = f"""Actúa como un editor cultural experto de Málaga. Hoy es {hoy_str}.
Tu misión es redactar una newsletter profesional y amigable.

INSTRUCCIONES DE FILTRADO Y DISEÑO:
1. MANTÉN los eventos futuros Y las exposiciones que ya han empezado pero que TODAVÍA se pueden visitar hoy.
2. ELIMINA cualquier evento que ya haya finalizado por completo antes de hoy ({hoy_str}).
3. ORDENA los eventos por fecha de inicio (de más cercano a más lejano).
4. FORMATO: Devuelve el contenido en HTML elegante.
5. Usa una TABLA (<table>) con columnas: "Fechas", "Evento / Exposición", "Descripción" y "Enlace".
6. Aplica estilos CSS en línea: tabla con bordes colapsados, fondo gris claro para el encabezado, padding en las celdas y fuentes limpias (Arial/sans-serif).
7. Incluye una introducción saludando a los malagueños y una despedida calurosa.

Eventos detectados:
{txt}
"""
    
    try:
        response = modelo_final.generate_content(prompt)
        return response.text.replace('```html', '').replace('```', '').strip()
    except Exception as err:
        return f"<p>ERROR AL GENERAR NEWSLETTER: {str(err)}</p>"

def send_email(subject, content):
    user, pwd = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS")
    if not user or not pwd: return
    msg = MIMEMultipart("alternative")
    msg["From"], msg["To"], msg["Subject"] = user, user, subject
    
    msg.attach(MIMEText(content, "html"))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(user, pwd)
            server.send_message(msg)
        print("Newsletter enviada con éxito!")
    except Exception as e:
        print(f"Error al enviar email: {e}")

if __name__ == "__main__":
    rss_urls, web_urls = get_sources(SOURCES_FILE)
    all_ev, hashes = [], set()
    
    # Procesar RSS
    for u in rss_urls:
        for e in parse_rss_feed(u):
            h = generate_hash(e)
            if h not in hashes: all_ev.append(e); hashes.add(h)
    
    # Procesar Web Scraping
    for u in web_urls:
        for e in scrape_web_with_gemini(u):
            h = generate_hash(e)
            if h not in hashes: all_ev.append(e); hashes.add(h)
    
    # Generar y enviar newsletter
    content = summarize_and_order_events_with_gemini(all_ev)
    send_email("Tu Newsletter de Eventos y Exposiciones en Málaga", content)
