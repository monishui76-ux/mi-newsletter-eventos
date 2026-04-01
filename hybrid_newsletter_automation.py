import os
import time
import re
import hashlib
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin, urlparse, quote

import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime

# --- Configuración de Logging y Advertencias ---
# Evitar errores de cabeceras técnicas en GitHub Actions y entornos ruidosos
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"

# Silenciar avisos de seguridad de webs con certificados antiguos o inválidos.
# ADVERTENCIA: Esto reduce la seguridad. Usar con precaución y solo si es estrictamente necesario.
# Considerar configurar certificados de forma adecuada en entornos de producción.
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# --- Configuración de la API de Gemini ---
# Asegúrate de que la variable de entorno GEMINI_API_KEY esté configurada
try:
    import google.generativeai as genai
    genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))
    # Se recomienda usar modelos más capaces para tareas complejas como la generación de newsletters
    # gemini-2.5-flash es rápido pero puede tener limitaciones en contexto largo o razonamiento complejo.
    # gemini-2.5-pro o gemini-1.5-pro podrían ser alternativas si se requiere mayor calidad.
    MODELO_FINAL = genai.GenerativeModel("gemini-2.5-flash")
    IS_GEMINI_AVAILABLE = True
except ImportError:
    print("Advertencia: La librería google.generativeai no está instalada. La funcionalidad de IA estará deshabilitada.")
    IS_GEMINI_AVAILABLE = False
except Exception as e:
    print(f"Error al configurar Google Generative AI: {e}. La funcionalidad de IA estará deshabilitada.")
    IS_GEMINI_AVAILABLE = False

# --- Constantes de Configuración ---
SOURCES_FILE = 'sources.txt'
HISTORY_FILE = 'history.json'
EVENT_KEYWORDS = ['agenda', 'eventos', 'programacion', 'exposiciones', 'actividades', 'calendario', 'concierto', 'teatro', 'museo']
MAX_WEB_SCRAPE_DEPTH = 1  # Profundidad máxima de navegación en sitios web
MAX_CHUNK_SIZE = 15000    # Tamaño máximo de cada trozo de texto para enviar a Gemini
REQUEST_TIMEOUT = 30      # Segundos de espera para peticiones HTTP
HISTORY_MAX_SIZE = 500    # Número máximo de hashes de eventos a guardar en el historial

# --- Configuración de Email ---
EMAIL_SMTP_SERVER = "smtp.gmail.com"
EMAIL_SMTP_PORT = 465
EMAIL_USER = os.environ.get("EMAIL_USER")
EMAIL_PASS = os.environ.get("EMAIL_PASS")

# --- Funciones de Utilidad ---

def log_info(message):
    """Imprime un mensaje informativo con timestamp."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: {message}")

def log_warning(message):
    """Imprime un mensaje de advertencia con timestamp."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] WARN: {message}")

def log_error(message):
    """Imprime un mensaje de error con timestamp."""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] ERROR: {message}")

def load_history(file_path=HISTORY_FILE):
    """Carga el historial de eventos procesados desde un archivo JSON."""
    if not os.path.exists(file_path):
        log_info(f"Archivo de historial '{file_path}' no encontrado. Se creará uno nuevo.")
        return []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            history_data = json.load(f)
            # Asegurarse de que sea una lista y que los elementos sean strings (hashes)
            if isinstance(history_data, list):
                return [str(h) for h in history_data if isinstance(h, str)]
            else:
                log_warning(f"El archivo de historial '{file_path}' no contiene una lista válida. Se creará uno nuevo.")
                return []
    except json.JSONDecodeError:
        log_error(f"El archivo de historial '{file_path}' no es un JSON válido. Se creará uno nuevo.")
        return []
    except Exception as e:
        log_error(f"Error inesperado cargando historial de '{file_path}': {e}")
        return []

def save_history(history_list, file_path=HISTORY_FILE):
    """Guarda el historial de eventos procesados en un archivo JSON."""
    try:
        # Mantener solo los últimos N elementos para evitar archivos excesivamente grandes
        recent_history = history_list[-HISTORY_MAX_SIZE:]
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(recent_history, f, ensure_ascii=False, indent=2)
        log_info(f"Historial guardado en '{file_path}'. Se conservan los últimos {len(recent_history)} eventos.")
    except Exception as e:
        log_error(f"Error guardando historial en '{file_path}': {e}")

def get_sources(file_path=SOURCES_FILE):
    """Lee las fuentes de RSS y URLs web desde un archivo de texto."""
    rss_feeds, web_urls = [], []
    if not os.path.exists(file_path):
        log_warning(f"Archivo de fuentes '{file_path}' no encontrado. No se procesarán fuentes.")
        return rss_feeds, web_urls

    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'): # Ignorar líneas vacías y comentarios
                    if line.lower().startswith('rss:'):
                        rss_feeds.append(line[4:].strip())
                    elif line.lower().startswith('web:'):
                        web_urls.append(line[4:].strip())
                    else:
                        log_warning(f"Línea no reconocida en '{file_path}': '{line}'. Se ignorará.")
        log_info(f"Fuentes cargadas: {len(rss_feeds)} RSS, {len(web_urls)} Web.")
    except Exception as e:
        log_error(f"Error leyendo fuentes de '{file_path}': {e}")
    return rss_feeds, web_urls

def parse_rss_feed(feed_url):
    """Parsea un feed RSS y extrae información de eventos."""
    events = []
    log_info(f"Procesando feed RSS: {feed_url}")
    try:
        feed = feedparser.parse(feed_url)
        if feed.bozo:
            log_warning(f"Feed RSS '{feed_url}' puede estar mal formado. Bozo: {feed.bozo_exception}")

        for entry in feed.entries:
            fecha_pub = None
            # Intentar obtener la fecha de publicación parseada
            if hasattr(entry, 'published_parsed') and entry.published_parsed:
                try:
                    fecha_pub = datetime(*entry.published_parsed[:6])
                except (TypeError, ValueError) as e:
                    log_warning(f"No se pudo parsear la fecha de publicación para un ítem en '{feed_url}': {e}")
            elif hasattr(entry, 'updated_parsed') and entry.updated_parsed: # Fallback a fecha de actualización
                 try:
                    fecha_pub = datetime(*entry.updated_parsed[:6])
                 except (TypeError, ValueError) as e:
                    log_warning(f"No se pudo parsear la fecha de actualización para un ítem en '{feed_url}': {e}")

            # Extraer datos de forma segura, proporcionando valores por defecto
            events.append({
                'title': getattr(entry, 'title', 'Sin título').strip(),
                'link': getattr(entry, 'link', '#').strip(),
                'summary': getattr(entry, 'summary', 'Sin descripción').strip(),
                'date_pub': fecha_pub, # Puede ser None
                'source': feed_url
            })
        log_info(f"Feed RSS '{feed_url}' procesado. {len(events)} eventos encontrados.")
    except Exception as e:
        log_error(f"Error parseando RSS '{feed_url}': {e}")
    return events

def find_event_links(base_url, soup, visited_urls):
    """Encuentra enlaces a eventos dentro de una página HTML, filtrando por dominio y palabras clave."""
    found_links = set()
    base_domain = urlparse(base_url).netloc

    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href'].strip()
        if not href or href.startswith('#') or href.lower().startswith('mailto:') or href.lower().startswith('tel:'):
            continue # Ignorar enlaces vacíos, anclas, email o teléfono

        full_url = urljoin(base_url, href)
        parsed_url = urlparse(full_url)

        # Solo considerar enlaces dentro del mismo dominio y que no hayan sido visitados
        if parsed_url.netloc == base_domain and full_url not in visited_urls:
            link_text = a_tag.get_text().lower()
            # Comprobar si el texto del enlace o la URL contienen palabras clave de eventos
            if any(keyword in link_text for keyword in EVENT_KEYWORDS) or \
               any(keyword in full_url.lower() for keyword in EVENT_KEYWORDS):
                found_links.add(full_url)
    return list(found_links)

def clean_json_response(text):
    """Limpia la respuesta de Gemini para extraer solo el bloque JSON (lista de diccionarios)."""
    if not text:
        log_warning("Respuesta de Gemini vacía.")
        return []

    # Intentar encontrar un bloque JSON que empiece con '[' y termine con ']'
    # Se usa re.DOTALL para que '.' coincida con saltos de línea
    match = re.search(r'\[.*?\]', text, re.DOTALL)
    if match:
        json_str = match.group(0)
        try:
            # Intentar parsear el JSON
            data = json.loads(json_str)
            # Asegurarse de que sea una lista y que cada elemento sea un diccionario
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
            else:
                log_warning(f"La respuesta JSON no es una lista. Tipo: {type(data)}. Contenido: {json_str[:200]}...")
                return []
        except json.JSONDecodeError as e:
            log_error(f"Error al decodificar JSON de la respuesta de Gemini: {e}. JSON string: {json_str[:500]}...")
            return []
    else:
        log_warning(f"No se encontró un bloque JSON válido (iniciando con '[' y terminando con ']') en la respuesta de Gemini.\nRespuesta completa: {text[:500]}...")
        return []

def scrape_web_with_gemini(url):
    """
    Raspa una URL web, extrae texto e imágenes, y utiliza Gemini para identificar eventos.
    Navega hasta una profundidad limitada para encontrar enlaces relevantes.
    """
    events = []
    hoy_str = datetime.now().strftime('%Y-%m-%d')
    visited_urls = set()
    urls_to_scrape = [url]
    all_text_content = ""
    all_image_urls = []
    
    log_info(f"Iniciando scraping web en: {url} hasta profundidad {MAX_WEB_SCRAPE_DEPTH}")

    while urls_to_scrape and len(visited_urls) <= MAX_WEB_SCRAPE_DEPTH * 5: # Límite arbitrario para evitar bucles infinitos
        current_url = urls_to_scrape.pop(0)
        if current_url in visited_urls:
            continue
        visited_urls.add(current_url)

        log_info(f"Raspando: {current_url}")
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept-Language': 'es-ES,es;q=0.9',
                'Referer': 'https://www.google.com/' # Simular referer para evitar bloqueos
            }
            # Usar verify=False es un riesgo de seguridad. Si es posible, configurar certificados.
            resp = requests.get(current_url, headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
            resp.raise_for_status() # Lanza una excepción para códigos de estado de error (4xx o 5xx)

            soup = BeautifulSoup(resp.text, 'html.parser')
            
            # Extraer URLs de imágenes relevantes
            for img_tag in soup.find_all('img', src=True):
                img_src = img_tag['src'].strip()
                full_img_url = urljoin(current_url, img_src)
                # Filtrar imágenes pequeñas, iconos o logos
                if not any(ext in full_img_url.lower() for ext in ['.gif', '.svg', '.ico', '.webp']) and \
                   not any(keyword in full_img_url.lower() for keyword in ['logo', 'icon', 'banner', 'ad', 'ads']):
                    all_image_urls.append(full_img_url)

            # Limpiar el contenido HTML: eliminar scripts, estilos, metadatos y elementos de navegación/pie de página
            for s in soup(["script", "style", "header", "footer", "nav", "aside", "form", "noscript", "meta", "link", "title"]):
                s.extract()
            
            # Obtener texto limpio y añadirlo al contenido general
            clean_text = " ".join(soup.get_text(separator=' ', strip=True).split())
            if clean_text: # Solo añadir si hay texto útil
                all_text_content += f"\n--- CONTENIDO DE {current_url} ---\n{clean_text}\n"

            # Si no hemos alcanzado la profundidad máxima, buscar nuevos enlaces
            current_depth = len(visited_urls) # Aproximación de la profundidad
            if current_depth < MAX_WEB_SCRAPE_DEPTH:
                new_links = find_event_links(current_url, soup, visited_urls)
                # Añadir solo enlaces que no estén ya en la cola o visitados
                for link in new_links:
                    if link not in urls_to_scrape and link not in visited_urls:
                        urls_to_scrape.append(link)
                log_info(f"Encontrados {len(new_links)} nuevos enlaces potenciales en {current_url}. Cola: {len(urls_to_scrape)}")
            
            time.sleep(1) # Pequeña pausa para no sobrecargar el servidor

        except requests.exceptions.Timeout:
            log_warning(f"Timeout al raspar {current_url}. Saltando.")
        except requests.exceptions.RequestException as e:
            log_error(f"Error de conexión al raspar {current_url}: {e}")
        except Exception as e:
            log_error(f"Error general al raspar {current_url}: {e}")
        
        # Limitar el número de URLs a raspar para evitar tiempos de ejecución excesivos
        if len(visited_urls) > MAX_WEB_SCRAPE_DEPTH * 10: # Límite más estricto
            log_warning(f"Se alcanzó el límite de URLs a raspar ({len(visited_urls)}). Deteniendo scraping web.")
            break

    if not all_text_content:
        log_warning(f"No se pudo extraer contenido de texto útil de {url}.")
        return []

    # Dividir el contenido en trozos para enviarlos a Gemini
    text_chunks = [all_text_content[i:i + MAX_CHUNK_SIZE] for i in range(0, len(all_text_content), MAX_CHUNK_SIZE)]
    log_info(f"Contenido web dividido en {len(text_chunks)} trozos para procesar con Gemini.")
    
    for i, chunk in enumerate(text_chunks):
        log_info(f"Procesando trozo {i+1}/{len(text_chunks)} con Gemini...")
        
        # Preparar los 'parts' para Gemini: texto y hasta 5 imágenes por trozo
        gemini_parts = [f"Analiza este contenido de una web cultural de Málaga. Hoy es {hoy_str}. Extrae TODOS los eventos futuros o vigentes sin excepción.\n\nTexto:\n{chunk}"]
        
        # Añadir imágenes solo al primer trozo para evitar duplicidad y sobrecarga
        if i == 0:
            images_to_upload = all_image_urls[:5] # Limitar a 5 imágenes por llamada
            for img_url in images_to_upload:
                try:
                    log_info(f"Subiendo imagen para Gemini: {img_url}")
                    gemini_parts.append(genai.upload_file(img_url))
                except Exception as e:
                    log_warning(f"Error subiendo imagen '{img_url}' a Gemini: {e}")
                    # Continuar incluso si falla la carga de una imagen

        # Prompt para Gemini: solicitar un JSON con estructura específica
        prompt_gemini = """Devuelve un JSON con esta estructura exacta: [{'title': '...', 'date_info': '...', 'summary': '...', 'link': '...'}]
        INSTRUCCIÓN CRUCIAL: No resumas. Extrae CADA evento que encuentres en el texto. No incluyas texto fuera del JSON.
        Si no encuentras eventos, devuelve una lista vacía [].
        """
        
        try:
            # Combinar el texto del trozo con el prompt final
            full_prompt = gemini_parts + [prompt_gemini]
            res = MODELO_FINAL.generate_content(full_prompt)
            
            # Limpiar y parsear la respuesta de Gemini
            data = clean_json_response(res.text)
            for e in data:
                # Añadir la URL de origen del scraping a cada evento encontrado
                e['source'] = url
                events.append(e)
            log_info(f"Trozo {i+1} procesado. {len(data)} eventos encontrados en este trozo.")
            
        except Exception as e:
            log_error(f"Error con Gemini en trozo {i+1}: {e}\nRespuesta cruda (primeros 500 chars): {getattr(res, 'text', 'N/A')[:500]}...")
            
        time.sleep(1) # Pequeña pausa entre llamadas a Gemini

    log_info(f"Scraping web completado para {url}. Total de eventos encontrados: {len(events)}.")
    return events

def generate_event_hash(event_data):
    """Genera un hash MD5 único para un evento basado en sus campos clave."""
    # Usar campos que definen la unicidad del evento para evitar duplicados
    title = event_data.get('title', '').strip()
    date_info = event_data.get('date_info', '').strip() # Usar date_info si está disponible, sino date_pub
    if not date_info and event_data.get('date_pub'):
        try:
            date_info = event_data['date_pub'].strftime('%Y-%m-%d %H:%M:%S')
        except AttributeError: # Si date_pub no es un objeto datetime
            date_info = str(event_data.get('date_pub', ''))

    source = event_data.get('source', '').strip()
    link = event_data.get('link', '').strip() # Incluir link para mayor precisión

    # Concatenar campos y codificar para hash
    event_string = f"{title}-{date_info}-{source}-{link}".encode('utf-8')
    return hashlib.md5(event_string).hexdigest()

def summarize_and_order_events_with_gemini(all_events, history):
    """
    Genera una newsletter en HTML utilizando Gemini, ordenando y filtrando eventos.
    """
    if not IS_GEMINI_AVAILABLE:
        log_error("Google Generative AI no está disponible. No se puede generar la newsletter.")
        return "<p>Error: Servicio de IA no disponible.</p>"

    if not all_events:
        log_info("No se encontraron eventos para generar la newsletter.")
        return "<p>No se han encontrado eventos vigentes esta semana.</p>"
    
    hoy = datetime.now()
    hoy_str_display = hoy.strftime('%d/%m/%Y')
    hoy_str_iso = hoy.strftime('%Y-%m-%d')
    
    processed_events = []
    current_hashes = set()

    # 1. Filtrar y preparar eventos para el prompt
    for e in all_events:
        event_hash = generate_event_hash(e)
        current_hashes.add(event_hash) # Añadir a los hashes actuales para el historial

        # Intentar obtener una fecha de evento para filtrar por vigencia
        event_date = None
        if e.get('date_info'):
            # Intentar parsear la fecha de 'date_info' si es posible
            try:
                # Asumir formato común como 'DD/MM/YYYY' o 'YYYY-MM-DD'
                # Esto puede requerir ajustes si los formatos varían mucho
                date_str = e['date_info'].split(' - ')[0].strip() # Tomar la primera parte si hay rangos
                if re.match(r'\d{1,2}/\d{1,2}/\d{4}', date_str):
                    event_date = datetime.strptime(date_str, '%d/%m/%Y')
                elif re.match(r'\d{4}-\d{2}-\d{2}', date_str):
                    event_date = datetime.strptime(date_str, '%Y-%m-%d')
                elif re.match(r'\d{1,2} de \w+ de \d{4}', date_str, re.IGNORECASE):
                    # Manejar formatos como "15 de mayo de 2024"
                    # Esto es más complejo y puede requerir librerías adicionales o lógica robusta
                    pass # Simplificado por ahora
            except (ValueError, TypeError, IndexError):
                pass # No se pudo parsear la fecha de date_info

        # Si no se pudo parsear de date_info, usar date_pub (si existe y es datetime)
        if event_date is None and isinstance(e.get('date_pub'), datetime):
            event_date = e['date_pub']

        # Filtrar eventos que ya han finalizado (basado en la fecha de hoy)
        # Si no hay fecha clara, se asume que es vigente para no perder información
        if event_date and event_date.date() < hoy.date():
            log_info(f"Evento finalizado (fecha: {event_date.strftime('%Y-%m-%d')}): {e.get('title', 'Sin título')}")
            continue # Saltar este evento

        # Determinar estado (Novedad vs Recordatorio)
        estado = "✨ Novedad" if event_hash not in history else "📌 Recordatorio"
        
        # Preparar descripción corta
        summary = e.get('summary', 'Sin descripción').strip()
        # Limitar la descripción para el prompt, pero mantenerla informativa
        max_desc_len = 150
        if len(summary) > max_desc_len:
            summary = summary[:max_desc_len] + "..."

        # Formatear enlace de calendario (Google Calendar)
        # Codificar parámetros para la URL
        title_encoded = quote(e.get('title', 'Evento Cultural Málaga'))
        link_encoded = quote(e.get('link', '#'))
        # Usar la fecha de evento si está disponible, sino la fecha de hoy como fallback
        event_start_date = event_date.strftime('%Y%m%dT%H%M%SZ') if event_date else hoy_str_iso + 'T090000Z'
        
        # URL de Google Calendar: https://www.google.com/calendar/render?action=TEMPLATE&text=[TITULO]&dates=[FECHA_INICIO]/[FECHA_FIN]&details=[DETALLES]&location=Málaga
        # Para simplificar, usamos solo la fecha de inicio y un enlace genérico
        calendar_link = f"https://www.google.com/calendar/render?action=TEMPLATE&text={title_encoded}&dates={event_start_date}&details={link_encoded}&location=Málaga"

        # Asignar categorías con emojis (ejemplo básico)
        categoria_emoji = "🌟" # Default
        title_lower = e.get('title', '').lower()
        if any(kw in title_lower for kw in ['concierto', 'música', 'festival']): categoria_emoji = "🎶"
        elif any(kw in title_lower for kw in ['exposición', 'arte', 'pintura', 'escultura', 'galería']): categoria_emoji = "🎨"
        elif any(kw in title_lower for kw in ['teatro', 'obra', 'espectáculo']): categoria_emoji = "🎭"
        elif any(kw in title_lower for kw in ['cine', 'película', 'festival de cine']): categoria_emoji = "🎬"
        elif any(kw in title_lower for kw in ['libro', 'presentación', 'charla', 'literatura']): categoria_emoji = "📚"
        elif any(kw in title_lower for kw in ['museo', 'historia', 'patrimonio']): categoria_emoji = "🏛️"
        elif any(kw in title_lower for kw in ['familiar', 'niños', 'infantil']): categoria_emoji = "👨‍👩‍👧‍👦"
        elif any(kw in title_lower for kw in ['gastronomía', 'vino', 'cata']): categoria_emoji = "🍷"

        processed_events.append({
            'estado': estado,
            'categoria': categoria_emoji,
            'fecha_display': e.get('date_info', 'Consultar web').split(' - ')[0].strip() if e.get('date_info') else 'Consultar', # Mostrar solo la fecha de inicio
            'titulo': e.get('title', 'Evento sin título').strip(),
            'descripcion': summary,
            'link': e.get('link', e.get('source', '#')).strip(),
            'calendar_link': calendar_link
        })

    # Ordenar eventos por fecha de inicio (si se pudo determinar)
    # Los eventos sin fecha clara irán al final
    processed_events.sort(key=lambda x: datetime.strptime(x['fecha_display'], '%d/%m/%Y') if re.match(r'\d{1,2}/\d{1,2}/\d{4}', x['fecha_display']) else datetime.max)

    # Construir el texto para el prompt de Gemini
    txt_for_gemini = ""
    for e in processed_events:
        txt_for_gemini += f"[{e['estado']}] {e['titulo']} | {e['fecha_display']} | {e.get('source', 'Web')} | {e['descripcion']} | {e['link']}\n"

    # Prompt para la generación de la newsletter HTML
    prompt_newsletter = f"""Actúa como un editor cultural experto de Málaga. Hoy es {hoy_str_display}.
    Genera una newsletter profesional e interactiva en HTML con una tabla elegante.
    
    REGLAS ESTRICTAS DE EXHAUSTIVIDAD Y FORMATO:
    1. SIN LÍMITES: Incluye absolutamente TODOS los eventos listados a continuación. No omitas ninguno por longitud de la lista.
    2. INTRODUCCIÓN: Saludo amigable a los malagueños, mencionando la fecha actual.
    3. TABLA HTML: Tabla (<table>) con estilos CSS INLINE para asegurar compatibilidad.
    4. COLUMNAS: "Estado", "Categoría", "Fechas", "Evento / Exposición", "Descripción", "Enlace" y "Calendario".
    5. ESTADO: Usa los emojis proporcionados: "✨ Novedad" o "📌 Recordatorio".
    6. CATEGORÍA: Usa los emojis proporcionados (🎭, 🎨, 🎶, 🎬, 📚, 🏛️, 👨‍👩‍👧‍👦, 🍷, 🌟).
    7. CALENDARIO: Enlace clicable "📅 Añadir" a Google Calendar. El enlace ya está pre-formateado.
    8. FILTRADO: Ya se han filtrado eventos finalizados antes de hoy ({hoy_str_display}). Asegúrate de que no se reintroduzcan.
    9. ORDEN: Los eventos ya están ordenados por fecha de inicio (de más cercano a más lejano). Mantén este orden.
    10. DESPEDIDA: Cálida y profesional.
    
    Lista completa de eventos a procesar:
    {txt_for_gemini}
    """
    
    log_info("Generando contenido de la newsletter con Gemini...")
    try:
        # Usamos un modelo con mayor capacidad de respuesta para listas largas
        response = MODELO_FINAL.generate_content(prompt_newsletter)
        # Limpiar posibles bloques de código markdown
        clean_res = response.text.replace('```html', '').replace('```', '').strip()
        log_info("Newsletter generada con éxito.")
        return clean_res
    except Exception as err:
        log_error(f"Error al generar newsletter con Gemini: {err}\nRespuesta cruda (primeros 500 chars): {getattr(response, 'text', 'N/A')[:500]}...")
        return f"<p>ERROR AL GENERAR NEWSLETTER: {str(err)}</p>"

def send_email(subject, content, sender_email=EMAIL_USER, recipient_email=EMAIL_USER):
    """Envía un email HTML utilizando SMTP."""
    if not EMAIL_USER or not EMAIL_PASS:
        log_error("Credenciales de email (EMAIL_USER, EMAIL_PASS) no configuradas. No se puede enviar el email.")
        return

    if not content or "<p>ERROR" in content or "<p>No se han encontrado eventos" in content:
        log_warning("Contenido del email vacío o contiene errores. No se enviará.")
        return

    msg = MIMEMultipart("alternative")
    msg["From"] = sender_email
    msg["To"] = recipient_email
    msg["Subject"] = subject
    
    # Adjuntar el contenido HTML
    msg.attach(MIMEText(content, "html", "utf-8"))
    
    log_info(f"Intentando enviar email a {recipient_email} con asunto: {subject}")
    try:
        with smtplib.SMTP_SSL(EMAIL_SMTP_SERVER, EMAIL_SMTP_PORT) as server:
            server.login(EMAIL_USER, EMAIL_PASS)
            server.send_message(msg)
        log_info("Newsletter enviada con éxito!")
    except smtplib.SMTPAuthenticationError:
        log_error("Error de autenticación SMTP. Verifica tu usuario y contraseña de email.")
    except Exception as e:
        log_error(f"Error al enviar email: {e}")

# --- Bloque Principal de Ejecución ---
if __name__ == "__main__":
    log_info("--- Inicio del Proceso de Generación de Newsletter de Eventos ---")

    # 1. Cargar historial de eventos procesados
    history = load_history()
    log_info(f"Historial cargado: {len(history)} eventos previos.")

    # 2. Obtener fuentes de datos
    rss_urls, web_urls = get_sources()

    all_found_events = []
    current_event_hashes = set() # Para rastrear eventos únicos en esta ejecución

    # 3. Procesar feeds RSS
    if rss_urls:
        log_info("Iniciando procesamiento de feeds RSS...")
        for u in rss_urls:
            events_from_rss = parse_rss_feed(u)
            for e in events_from_rss:
                h = generate_event_hash(e)
                if h not in current_event_hashes:
                    all_found_events.append(e)
                    current_event_hashes.add(h)
        log_info(f"Procesamiento RSS completado. {len(all_found_events)} eventos únicos encontrados hasta ahora.")
    else:
        log_info("No se especificaron feeds RSS.")

    # 4. Procesar URLs web (scraping)
    if web_urls and IS_GEMINI_AVAILABLE:
        log_info("Iniciando procesamiento de Web Scraping...")
        for u in web_urls:
            events_from_web = scrape_web_with_gemini(u)
            for e in events_from_web:
                h = generate_event_hash(e)
                if h not in current_event_hashes:
                    all_found_events.append(e)
                    current_event_hashes.add(h)
        log_info(f"Procesamiento Web Scraping completado. {len(all_found_events)} eventos únicos totales.")
    elif not IS_GEMINI_AVAILABLE:
        log_warning("La funcionalidad de Web Scraping con Gemini está deshabilitada porque la librería de Google AI no está disponible o configurada.")
    else:
        log_info("No se especificaron URLs web para scraping.")
    
    # 5. Generar contenido de la newsletter
    log_info("Generando contenido de la newsletter con Gemini...")
    newsletter_content = summarize_and_order_events_with_gemini(all_found_events, history)
    
    # 6. Enviar la newsletter por email
    if newsletter_content:
        send_email("Tu Newsletter de Eventos en Málaga - ¡Novedades y Recordatorios!", newsletter_content)
    else:
        log_warning("No se generó contenido para la newsletter. No se enviará email.")
    
    # 7. Actualizar historial
    # Combinar historial antiguo con los hashes de esta ejecución y mantener el tamaño máximo
    updated_history = list(set(history + list(current_event_hashes)))
    save_history(updated_history)
    
    log_info("--- Proceso de Generación de Newsletter Finalizado ---")
    
