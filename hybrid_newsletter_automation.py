import os
os.environ["GRPC_VERBOSITY"] = "ERROR"
os.environ["GLOG_minloglevel"] = "2"

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import requests
import google.generativeai as genai
from google.generativeai.types import RequestOptions
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import feedparser
from datetime import datetime
from bs4 import BeautifulSoup
import hashlib
import json

# --- CONFIGURACIÓN TOTAL ---
SOURCES_FILE = 'sources.txt'
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

# Forzamos la versión v1 de la API de forma global
client_opts = RequestOptions(api_version="v1")

# Usamos el modelo más compatible con la API v1
modelo_final = genai.GenerativeModel(model_name="gemini-1.5-flash-latest")

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
            events.append({
                'title': getattr(entry, 'title', 'Sin título'),
                'link': getattr(entry, 'link', 'Sin enlace'),
                'summary': getattr(entry, 'summary', 'Sin descripción'),
                'date': datetime(*entry.published_parsed[:6]) if hasattr(entry, 'published_parsed') else None,
                'source': feed_url
            })
    except: pass
    return events

def scrape_web_with_gemini(url):
    events = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(url, headers=headers, timeout=15, verify=False)
        soup = BeautifulSoup(resp.text, 'html.parser')
        for s in soup(["script", "style"]): s.extract()
        clean_text = " ".join(soup.get_text().split())[:12000]
        
        prompt = f"Extrae eventos de este texto en JSON: {clean_text}. Formato: [{{'title': '...', 'date': 'YYYY-MM-DD', 'summary': '...', 'link': '...'}}]"
        
        # LLAMADA CRÍTICA: Forzamos v1 aquí también
        res = modelo_final.generate_content(prompt, request_options=client_opts)
        data = json.loads(res.text.replace('```json', '').replace('```', '').strip())
        for e in data:
            e['date'] = datetime.strptime(e['date'], '%Y-%m-%d') if e.get('date') and e['date'] != 'Fecha por confirmar' else None
            e['source'], e['type'] = url, 'web'
            events.append(e)
    except: pass
    return events

def generate_hash(e):
    d = e['date'].strftime('%Y-%m-%d') if e['date'] else 'NA'
    return hashlib.md5(f"{e['title']}-{d}".encode()).hexdigest()

def summarize_and_order_events_with_gemini(all_events):
    if not all_events: return "No hay eventos."
    txt = ""
    for e in all_events:
        txt += f"- {e['title']} ({e['date']}): {e['link']}\n"

    prompt = f"Resume y ordena estos eventos de Málaga por fecha, sin duplicados:\n\n{txt}"
    
    try:
        # LLAMADA CRÍTICA FINAL: Forzamos v1
        response = modelo_final.generate_content(prompt, request_options=client_opts)
        return response.text
    except Exception as err:
        return f"ERROR CRÍTICO GEMINI: {str(err)}"

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
    except: pass

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
    
    all_ev.sort(key=lambda x: x['date'] if x['date'] else datetime.max)
    content = summarize_and_order_events_with_gemini(all_ev)
    send_email("Newsletter Eventos Málaga", content)
