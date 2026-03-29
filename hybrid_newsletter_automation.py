import os
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

# --- Configuración --- #
# Carga las URLs de los feeds RSS y las URLs de scraping desde un archivo
SOURCES_FILE = 'sources.txt'

# Configurar Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
model = genai.GenerativeModel("gemini-1.5-flash")

# --- Funciones --- #
def get_sources(file_path):
    rss_feeds = []
    web_urls = []
    try:
        with open(file_path, 'r') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    if line.lower().startswith('rss:'):
                        rss_feeds.append(line[4:].strip())
                    elif line.lower().startswith('web:'):
                        web_urls.append(line[4:].strip())
    except FileNotFoundError:
        print(f"Error: El archivo {file_path} no fue encontrado.")
    return rss_feeds, web_urls

def parse_rss_feed(feed_url):
    events = []
    try:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries:
            title = entry.title if hasattr(entry, 'title') else 'Sin título'
            link = entry.link if hasattr(entry, 'link') else 'Sin enlace'
            summary = entry.summary if hasattr(entry, 'summary') else 'Sin descripción'
            published_date = None
            if hasattr(entry, 'published_parsed'):
                try:
                    published_date = datetime(*entry.published_parsed[:6])
                except:
                    pass
            
            events.append({
                'title': title,
                'link': link,
                'summary': summary,
                'date': published_date,
                'source': feed_url,
                'type': 'rss'
            })
    except Exception as e:
        print(f"Error al parsear RSS {feed_url}: {e}")
    return events

def scrape_web_with_gemini(url):
    events = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'}
        response = requests.get(url, headers=headers, timeout=15, verify=False)
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Eliminar scripts y estilos para limpiar el texto
        for script in soup(["script", "style"]):
            script.extract()
        text = soup.get_text()
        
        # Limitar el texto a enviar a Gemini para evitar exceder el límite de tokens
        clean_text = " ".join(text.split())
        
        # Prompt para Gemini para extraer eventos
        prompt = f"""Analiza el siguiente texto de una página web: 
{clean_text[:15000]}

Extrae todos los eventos futuros que encuentres. Para cada evento, necesito la siguiente información en formato JSON, dentro de un array de objetos: 
{{"title": "Título del evento", "date": "YYYY-MM-DD", "summary": "Breve descripción", "link": "URL del evento"}}

Si no encuentras una fecha clara, usa "Fecha por confirmar". Si no hay un enlace específico para el evento, usa la URL de la página principal: {url}. Si no hay eventos, devuelve un array vacío.

Ejemplo de salida JSON:
[
  {{"title": "Concierto de Jazz", "date": "2026-04-15", "summary": "Noche de jazz en vivo con artistas locales.", "link": "https://ejemplo.com/concierto"}},
  {{"title": "Exposición de Arte Moderno", "date": "2026-05-01", "summary": "Muestra de obras de artistas emergentes.", "link": "https://ejemplo.com/exposicion"}}
]
"""
        
        gemini_response = model.generate_content(prompt)
        response_text = gemini_response.text.strip()
        
        # Intentar parsear la respuesta JSON de Gemini
        try:
            extracted_events = json.loads(response_text)
            for event in extracted_events:
                # Asegurarse de que la fecha sea un objeto datetime si es posible
                if event.get('date') and event['date'] != 'Fecha por confirmar':
                    try:
                        event['date'] = datetime.strptime(event['date'], '%Y-%m-%d')
                    except ValueError:
                        event['date'] = None # No se pudo parsear la fecha
                else:
                    event['date'] = None
                event['source'] = url
                event['type'] = 'web_scrape'
                events.append(event)
        except json.JSONDecodeError:
            print(f"Gemini no devolvió JSON válido para {url}: {response_text[:200]}...")
            # Si Gemini no devuelve JSON, intentar un parseo más simple o registrar el error
            # Para este tutorial, simplemente ignoramos los eventos no JSON

    except Exception as e:
        print(f"Error al hacer web scraping en {url}: {e}")
    return events

def generate_event_hash(event):
    # Genera un hash único para cada evento para la deduplicación
    # Considera título, fecha (si existe) y una parte del resumen
    date_str = event['date'].strftime('%Y-%m-%d') if event['date'] else 'NODATE'
    # Usar los primeros 100 caracteres del resumen para el hash
    summary_part = event['summary'][:100] if event['summary'] else 'NOSUMMARY'
    unique_string = f"{event['title']}-{date_str}-{summary_part}"
    return hashlib.md5(unique_string.encode('utf-8')).hexdigest()

def summarize_and_order_events_with_gemini(all_events):
    if not all_events:
        return "No se encontraron eventos para resumir."

    # Formatear eventos para el prompt de Gemini
    events_text = ""
    for event in all_events:
        date_str = event['date'].strftime('%Y-%m-%d') if event['date'] else 'Fecha desconocida'
        events_text += f"- Título: {event['title']}\n  Fuente: {event['source']}\n  Fecha: {date_str}\n  Resumen: {event['summary']}\n  Enlace: {event['link']}\n\n"

    prompt = f"""Eres un asistente experto en eventos culturales. A continuación te proporciono una lista de eventos extraídos de diversas fuentes (RSS y web scraping). Tu tarea es: 
1.  Consolidar y resumir los eventos de manera concisa.
2.  **Eliminar eventos duplicados o muy similares.**
3.  Eliminar eventos irrelevantes (ej. noticias generales que no son eventos).
4.  Ordenar los eventos por fecha, del más próximo al más lejano.
5.  Presentar el resumen en un formato amigable para una newsletter, incluyendo Título, Fecha, una breve descripción y el Enlace original. Si no hay fecha clara, puedes omitir el evento o indicarlo como 'Fecha por confirmar'.

Eventos:
{events_text}

Formato de salida deseado:

**Título de la Newsletter: Resumen de Eventos Culturales en Málaga**

¡Hola!

Aquí tienes un resumen de los próximos eventos culturales en Málaga:

--- 

**[Fecha del Evento] - [Título del Evento]**
[Breve descripción del evento].
[Enlace: [URL del Evento]]

--- 

**[Fecha del Evento] - [Título del Evento]**
[Breve descripción del evento].
[Enlace: [URL del Evento]]

... y así sucesivamente para cada evento.

"""
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error al generar resumen con Gemini: {e}"

def send_email(subject, content):
    remitente = os.environ.get("EMAIL_USER")
    destinatario = os.environ.get("EMAIL_USER") # Se envía a sí mismo por defecto
    password = os.environ.get("EMAIL_PASS")

    if not remitente or not password:
        print("Error: Las variables de entorno EMAIL_USER o EMAIL_PASS no están configuradas.")
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = remitente
    msg["To"] = destinatario
    msg["Subject"] = subject

    # Adjuntar el contenido como HTML para un formato más rico
    part1 = MIMEText(content, "html")
    msg.attach(part1)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(remitente, password)
            server.send_message(msg)
        print("Newsletter enviada con éxito!")
    except Exception as e:
        print(f"Error al enviar el correo: {e}")

# --- Ejecución Principal --- #
if __name__ == "__main__":
    rss_feed_urls, web_scrape_urls = get_sources(SOURCES_FILE)
    
    all_events = []
    processed_hashes = set()

    # Procesar RSS feeds
    for url in rss_feed_urls:
        print(f"Procesando RSS: {url}")
        events_from_feed = parse_rss_feed(url)
        for event in events_from_feed:
            event_hash = generate_event_hash(event)
            if event_hash not in processed_hashes:
                all_events.append(event)
                processed_hashes.add(event_hash)

    # Procesar URLs de web scraping
    for url in web_scrape_urls:
        print(f"Haciendo web scraping en: {url}")
        events_from_scrape = scrape_web_with_gemini(url)
        for event in events_from_scrape:
            event_hash = generate_event_hash(event)
            if event_hash not in processed_hashes:
                all_events.append(event)
                processed_hashes.add(event_hash)
        
    # Ordenar eventos por fecha antes de enviar a Gemini
    all_events.sort(key=lambda x: x['date'] if x['date'] else datetime.max)

    print("Generando resumen con Gemini...")
    newsletter_content = summarize_and_order_events_with_gemini(all_events)
    
    print("Enviando newsletter...")
    send_email("Tu Resumen de Eventos Culturales en Málaga", newsletter_content)
    
