import os
import time
import re

# Evitar errores de cabeceras técnicas
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
import json

# --- CONFIGURACIÓN PARA GEMINI 2.5 / 3 ---
SOURCES_FILE = 'sources.txt'
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
modelo_final = genai.GenerativeModel("gemini-2.5-flash")

EVENT_KEYWORDS = ['agenda', 'eventos', 'programacion', 'exposiciones', 'actividades', 'calendario']

def get_sources(file_path):
    rss_feeds, web_urls = [], []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if line.lower().startswith('rss:'): rss_feeds.append(line[4:].strip())
                    elif line.lower().startswith('web:'): web_urls.append(line[4:].strip())
    except Exception as e:
        print(f"Error leyendo sources.txt: {e}")
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
    except Exception as e:
        print(f"Error parseando RSS {feed_url}: {e}")
    return events

def find_event_links(base_url, soup, visited_urls):
    found_links = set()
    base_domain = urlparse(base_url).netloc

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        full_url = urljoin(base_url, href)
        parsed_url = urlparse(full_url)

        if parsed_url.netloc == base_domain and full_url not in visited_urls:
            link_text = a_tag.get_text().lower()
            if any(keyword in link_text for keyword in EVENT_KEYWORDS) or \
               any(keyword in full_url.lower() for keyword in EVENT_KEYWORDS):
                found_links.add(full_url)
    return list(found_links)

def clean_json_response(text):
    """Limpia la respuesta de Gemini para extraer solo el bloque JSON."""
    match = re.search(r'\s*\[.*?\]\s*', text, re.DOTALL)
    if match:
        json_str = match.group(0)
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"Error al decodificar JSON: {e}\nJSON string: {json_str[:500]}...")
    else:
        print(f"No se encontró un bloque JSON válido en la respuesta.\nRespuesta completa: {text[:500]}...")
    return []

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
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'es-ES,es;q=0.9',
                'Referer': 'https://www.google.com/'
            }
            resp = requests.get(current_url, headers=headers, timeout=30, verify=False)
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            for img_tag in soup.find_all('img', src=True):
                img_src = img_tag['src']
                full_img_url = urljoin(current_url, img_src)
                if not any(ext in full_img_url.lower() for ext in ['.gif', '.svg', '.ico']) and \
                   'logo' not in full_img_url.lower() and 'icon' not in full_img_url.lower():
                    all_image_urls.append(full_img_url)

            for s in soup(["script", "style", "header", "footer", "nav", "aside", "form", "noscript", "meta", "link"]): s.extract()
            clean_text = " ".join(soup.get_text().split())
            all_text_content += f"\n--- CONTENIDO DE {current_url} ---\n{clean_text}\n"

            if current_depth < MAX_DEPTH:
                new_links = find_event_links(current_url, soup, visited_urls)
                urls_to_scrape.extend(new_links)
                current_depth += 1
            
            time.sleep(1) # Pausa para evitar bloqueos

        except requests.exceptions.Timeout:
            print(f"Timeout al raspar {current_url}. Saltando.")
        except requests.exceptions.RequestException as e:
            print(f"Error de conexión al raspar {current_url}: {e}")
        except Exception as e:
            print(f"Error general al raspar {current_url}: {e}")
        continue

    MAX_CHUNK_SIZE = 15000
    text_chunks = [all_text_content[i:i + MAX_CHUNK_SIZE] for i in range(0, len(all_text_content), MAX_CHUNK_SIZE)]
    
    for i, chunk in enumerate(text_chunks):
        gemini_parts = [f"Analiza este contenido de una web cultural de Málaga. Hoy es {hoy}. Extrae TODOS los eventos futuros o vigentes sin excepción.\n\nTexto:\n{chunk}"]
        
        if i == 0:
            for img_url in all_image_urls[:5]: # Limitar imágenes para ahorrar tokens
                try:
                    gemini_parts.append(genai.upload_file(img_url))
                except Exception as e:
                    print(f"Error subiendo imagen {img_url}: {e}")
                    pass

        prompt = """Devuelve un JSON con esta estructura exacta: [{\'title\': \'...\', \'date_info\': \'...\', \'summary\': \'...\', \'link\': \'...\'}]\nINSTRUCCIÓN CRUCIAL: No resumas. Extrae CADA evento que encuentres en el texto. No incluyas texto fuera del JSON."""
        
        try:
            res = modelo_final.generate_content(gemini_parts + [prompt])
            data = clean_json_response(res.text)
            for e in data:
                e['source'] = url
                events.append(e)
        except Exception as e:
            print(f"Error con Gemini en chunk {i}: {e}\nRespuesta cruda: {getattr(res, 'text', 'N/A')[:500]}...")
        
        time.sleep(5) # Pausa para respetar límites de la API gratuita
            
    return events

def summarize_and_order_events_with_gemini(all_events):
    # --- VERSIÓN NEWSLETTER: V5.0 - MANUAL Y EXHAUSTIVA ---
    if not all_events: return "<p>No se han encontrado eventos vigentes esta semana.</p>"
    
    hoy_str = datetime.now().strftime('%d/%m/%Y')
    
    # Formatear eventos para el prompt de forma compacta
    txt = ""
    for e in all_events:
        date_info = e.get('date_info', 'Consultar web')
        link = e.get('link', e.get('source', '#'))
        summary = e.get('summary', '')
        title = e.get('title', 'Evento sin título')
        txt += f"- Título: {title} | Fechas: {date_info} | Fuente: {e.get('source', 'Web')} | Resumen: {summary[:100]}... | Enlace: {link}\n"

    prompt = f"""Actúa como un editor cultural experto de Málaga. Hoy es {hoy_str}.
    Genera una newsletter profesional en HTML con una tabla elegante.
    
    REGLAS ESTRICTAS DE EXHAUSTIVIDAD Y FORMATO:
    1. SIN LÍMITES: Incluye absolutamente TODOS los eventos listados a continuación. No omitas ninguno por longitud de la lista.
    2. INTRODUCCIÓN: Saludo amigable a los malagueños.
    3. TABLA HTML: Tabla (<table>) con estilos CSS INLINE, bordes suaves y fuentes limpias.
    4. COLUMNAS: "Fechas", "Evento / Exposición", "Descripción", "Enlace".
    5. FILTRADO: Mantén eventos futuros y exposiciones vigentes. Elimina lo finalizado antes de hoy ({hoy_str}).
    6. ORDEN: Ordena los eventos por fecha de inicio (de más cercano a más lejano).
    7. DESPEDIDA: Cálida y profesional.
    
    Lista completa de eventos a procesar:
    {txt}
    """
    
    try:
        response = modelo_final.generate_content(prompt)
        clean_res = response.text.replace('```html', '').replace('```', '').strip()
        if not clean_res.startswith('<'):
            clean_res = f"<p>Newsletter de Málaga - {hoy_str}</p>" + clean_res
        return clean_res
    except Exception as err:
        return f"<p>ERROR AL GENERAR NEWSLETTER: {str(err)}</p>"

def send_email(subject, content):
    user, pwd = os.environ.get("EMAIL_USER"), os.environ.get("EMAIL_PASS")
    if not user or not pwd: 
        print("Error: EMAIL_USER o EMAIL_PASS no configurados.")
        return
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
    all_ev = []
    
    print("Iniciando procesamiento de RSS...")
    for u in rss_urls:
        for e in parse_rss_feed(u):
            all_ev.append(e)
    print(f"RSS procesados. Eventos encontrados: {len(all_ev)}")

    print("Iniciando procesamiento de Web Scraping...")
    for u in web_urls:
        for e in scrape_web_with_gemini(u):
            all_ev.append(e)
    print(f"Web Scraping procesado. Eventos totales: {len(all_ev)}")
    
    # Eliminar duplicados después de recolectar todo
    unique_events = {}
    for event in all_ev:
        title = event.get('title', '').strip().lower()
        date_info = event.get('date_info', '').strip().lower()
        # Usar una combinación de título y fecha para la deduplicación
        key = f"{title}-{date_info}"
        if key not in unique_events:
            unique_events[key] = event
    all_ev = list(unique_events.values())

    # Filtrar eventos pasados y ordenar
    filtered_events = []
    hoy = datetime.now().date()
    for event in all_ev:
        # Intentar parsear la fecha para filtrar
        date_str = event.get('date_info', '')
        event_date = None
        try:
            # Asumimos que date_info puede ser 'DD/MM/YYYY' o 'YYYY-MM-DD'
            if '/' in date_str: event_date = datetime.strptime(date_str.split(' ')[0], '%d/%m/%Y').date()
            elif '-' in date_str: event_date = datetime.strptime(date_str.split(' ')[0], '%Y-%m-%d').date()
        except ValueError: pass # Si no se puede parsear, se asume vigente

        if event_date is None or event_date >= hoy:
            filtered_events.append(event)
    
    filtered_events.sort(key=lambda x: x.get('date_pub') if x.get('date_pub') else datetime.max)
    
    print("Generando contenido de la newsletter con Gemini...")
    content = summarize_and_order_events_with_gemini(filtered_events)
    send_email("Tu Newsletter de Eventos en Málaga - V5.0 Manual & Exhaustiva", content)
    
    print("Proceso completado.")
