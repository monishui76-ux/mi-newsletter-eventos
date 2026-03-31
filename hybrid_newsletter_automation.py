import os
import time
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
from urllib.parse import urljoin, urlparse
import hashlib
import json

# --- CONFIGURACIÓN PARA GEMINI 2.5 / 3 ---
SOURCES_FILE = 'sources.txt'
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
modelo_final = genai.GenerativeModel("gemini-2.5-flash")

# Lista de palabras clave para identificar enlaces de agenda/eventos
EVENT_KEYWORDS = ['agenda', 'eventos', 'programacion', 'exposiciones', 'actividades', 'calendario']

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

def find_event_links(base_url, soup, visited_urls):
    found_links = set()
    base_domain = urlparse(base_url).netloc

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        full_url = urljoin(base_url, href)
        parsed_url = urlparse(full_url)

        # Asegurarse de que el enlace es del mismo dominio y no ha sido visitado
        if parsed_url.netloc == base_domain and full_url not in visited_urls:
            # Comprobar si el texto del enlace o la URL contiene palabras clave
            if any(keyword in a_tag.get_text().lower() for keyword in EVENT_KEYWORDS) or \
               any(keyword in full_url.lower() for keyword in EVENT_KEYWORDS):
                found_links.add(full_url)
    return list(found_links)

def scrape_web_with_gemini(url):
    events = []
    hoy = datetime.now().strftime('%Y-%m-%d')
    visited_urls = set()
    urls_to_scrape = [url]
    all_text_content = ""
    all_image_urls = []
    
    MAX_DEPTH = 1
    current_depth = 0

    while urls_to_scrape and current_depth <= MAX_DEPTH:
        current_url = urls_to_scrape.pop(0)
        if current_url in visited_urls: continue
        visited_urls.add(current_url)

        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(current_url, headers=headers, timeout=15, verify=False)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Extraer URLs de imágenes
            for img_tag in soup.find_all('img', src=True):
                img_src = img_tag['src']
                full_img_url = urljoin(current_url, img_src)
                # Filtrar imágenes pequeñas o irrelevantes (ej. iconos)
                if not any(ext in full_img_url.lower() for ext in ['.gif', '.svg', '.ico']) and \
                   'logo' not in full_img_url.lower() and 'icon' not in full_img_url.lower():
                    all_image_urls.append(full_img_url)

            for s in soup(["script", "style", "header", "footer", "nav"]): s.extract()
            clean_text = " ".join(soup.get_text().split())
            all_text_content += clean_text + "\n\n"

            if current_depth < MAX_DEPTH:
                new_links = find_event_links(current_url, soup, visited_urls)
                urls_to_scrape.extend(new_links)
                current_depth += 1
            
            time.sleep(1)

        except Exception as e:
            print(f"Error scraping {current_url}: {e}")
            continue

    MAX_GEMINI_INPUT_LENGTH = 25000
    text_chunks = [all_text_content[i:i + MAX_GEMINI_INPUT_LENGTH] for i in range(0, len(all_text_content), MAX_GEMINI_INPUT_LENGTH)]
    
    # Preparar partes para Gemini (texto e imágenes)
    gemini_parts = []
    for chunk in text_chunks:
        gemini_parts.append(chunk)
    
    # Añadir imágenes, limitando para no exceder el contexto de Gemini
    # Gemini 2.5 Flash tiene un límite de contexto muy alto, pero es bueno ser precavido
    # Limitar a 5 imágenes por URL principal para evitar sobrecarga
    for img_url in all_image_urls[:5]: 
        gemini_parts.append(genai.upload_file(img_url))

    prompt_text = f"""Analiza este contenido (texto e imágenes) de una web cultural de Málaga. Hoy es {hoy}.
        Extrae los eventos que CUMPLAN ALGUNA de estas condiciones:
        1. Eventos que ocurrirán en el futuro.
        2. Eventos o exposiciones que ya han comenzado pero que SIGUEN VIGENTES hoy (tienen una duración de varios días/meses).
        
        Devuelve un JSON: [{{\'title\': \'...\', \'date_info\': \'...\', \'summary\': \'...\', \'link\': \'...\'}}]
        En \'date_info\', indica el rango de fechas o la fecha específica. Si no hay enlace específico para el evento, usa el enlace de la web principal.
        Si la información del evento proviene de una imagen, indícalo en el resumen.
        
        Contenido a analizar:
        """
    
    # Combinar el prompt con las partes de texto e imagen
    full_gemini_input = [prompt_text] + gemini_parts

    try:
        res = modelo_final.generate_content(full_gemini_input)
        data = json.loads(res.text.replace('```json', '').replace('```', '').strip())
        for e in data:
            e['source'], e['type'] = url, 'web'
            events.append(e)
    except Exception as e:
        print(f"Error con Gemini en scraping multimodal: {e}")
        # Intentar con solo texto si falla el multimodal
        try:
            prompt_text_fallback = f"""Analiza este texto de una web cultural de Málaga. Hoy es {hoy}.
                Extrae los eventos que CUMPLAN ALGUNA de estas condiciones:
                1. Eventos que ocurrirán en el futuro.
                2. Eventos o exposiciones que ya han comenzado pero que SIGUEN VIGENTES hoy (tienen una duración de varios días/meses).
                
                Devuelve un JSON: [{{\'title\': \'...\', \'date_info\': \'...\', \'summary\': \'...\', \'link\': \'...\'}}]
                En \'date_info\', indica el rango de fechas o la fecha específica. Si no hay enlace específico para el evento, usa el enlace de la web principal.
                
                Texto a analizar:
                {all_text_content[:MAX_GEMINI_INPUT_LENGTH]}
                """
            res_fallback = modelo_final.generate_content(prompt_text_fallback)
            data_fallback = json.loads(res_fallback.text.replace('```json', '').replace('```', '').strip())
            for e in data_fallback:
                e['source'], e['type'] = url, 'web'
                events.append(e)
        except Exception as e_fallback:
            print(f"Error con Gemini en scraping solo texto (fallback): {e_fallback}")
            pass
    return events

def generate_hash(e):
    date_info = e.get('date_info', '')
    return hashlib.md5(f"{e['title']}-{date_info}".encode()).hexdigest()

def summarize_and_order_events_with_gemini(all_events):
    if not all_events: return "<p>No se han encontrado eventos vigentes esta semana.</p>"
    
    hoy_str = datetime.now().strftime('%d/%m/%Y')
    txt = ""
    for e in all_events:
        date_info = e.get('date_info', 'Consultar web')
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
    
    for u in rss_urls:
        for e in parse_rss_feed(u):
            h = generate_hash(e)
            if h not in hashes: all_ev.append(e); hashes.add(h)
    
    for u in web_urls:
        for e in scrape_web_with_gemini(u):
            h = generate_hash(e)
            if h not in hashes: all_ev.append(e); hashes.add(h)
    
    all_ev.sort(key=lambda x: x['date_pub'] if x['date_pub'] else datetime.max) # Ordenar por fecha de publicación para RSS
    
    content = summarize_and_order_events_with_gemini(all_ev)
    send_email("Tu Newsletter de Eventos y Exposiciones en Málaga", content)
