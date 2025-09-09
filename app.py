from flask import Flask, render_template, send_from_directory, request, send_file, jsonify
from datetime import datetime, timedelta, date
from icalendar import Calendar, Event
import fitz  # PyMuPDF
import re
import io
import os

app = Flask(__name__, static_folder="static", template_folder="templates")

# =========================
# PWA ROUTES
# =========================

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/json")

@app.route("/icon-192.png")
def icon192():
    return send_from_directory("static", "icon-192.png")

@app.route("/icon-512.png")
def icon512():
    return send_from_directory("static", "icon-512.png")

@app.route("/service-worker.js")
def service_worker():
    return send_from_directory("static", "service-worker.js", mimetype="application/javascript")


# =========================
# PDF → TEXT
# =========================

def extract_text_from_pdf(file_storage) -> str:
    """
    Leest het PDF-bestand met PyMuPDF en geeft platte tekst terug.
    """
    data = file_storage.read()
    with fitz.open(stream=data, filetype="pdf") as doc:
        parts = [page.get_text() for page in doc]
    return "\n".join(parts)


# =========================
# PARSER HELPERS
# =========================

# accepteer 10-09-2025, 10/09/2025, 10-09-25, 10/09/25
DATE_RE = r"(\d{2}[/-]\d{2}[/-](?:\d{2}|\d{4}))"
# streepje in tijden kan '-', en-dash '–' of em-dash '—' zijn
DASH = r"[-–—]"

def _normalize_text(s: str) -> str:
    """
    Normaliseert speciale tekens die vaak uit PDFs komen.
    - NBSP → spatie
    - en-dash/em-dash → '-'
    """
    s = (
        s.replace("\u00A0", " ")
         .replace("\u2013", "-")
         .replace("\u2014", "-")
    )
    # Optioneel: CRLF → LF
    return s.replace("\r\n", "\n").replace("\r", "\n")

def _parse_flexible_date(date_str: str) -> date | None:
    """
    Parseert dd-mm-yyyy / dd/mm/yyyy en dd-mm-yy / dd/mm/yy.
    """
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def _fix_2400(t: str) -> str:
    # Sommige roosters gebruiken 24:00; zet om naar 23:59.
    return "23:59" if t == "24:00" else t


# =========================
# PARSER (per datumblok, neem ALLE diensten)
# =========================

def extract_events_from_text(raw_text: str):
    """
    Parser die ALLE diensten per dag meeneemt.

    Werkwijze:
      1) Vind elke datum (dd-mm[-yy] of dd/mm[-yy]).
      2) Neem de tekst van deze datum t/m de volgende datum (datumblok).
      3) Binnen het blok: vind ALLE matches van '(...DIENST...) HH:MM–HH:MM' (tolerant voor en-/em-dash).
         - We matchen op woorden die 'DIENST' bevatten (bv. AVONDDIENST, NACHTDIENST) én exact 'DIENST'.
      4) Maak per match een event.
      5) Dedupliceer identieke tijdvakken binnen dezelfde dag.
    """
    text = _normalize_text(raw_text)
    events = []

    # Vind alle datummatches
    date_iter = list(re.finditer(DATE_RE, text))
    if not date_iter:
        return events

    # Regex voor service-lijnen binnen een datumblok:
    #  - sta bv. "DIENST", "AVONDDIENST", "NACHTDIENST" toe met \w*DIENST\w*
    #  - optionele dubbelepunt na (A(VOND)?|NACHT)?DIENST
    #  - max ~60 niet-cijfertekens tot de tijd ("veiligheidsbuffer" ivm kolomsprongen)
    service_re = re.compile(
        rf"(?i)\b\w*DIENST\w*\b[^0-9]{{0,60}}(\d{{2}}:\d{{2}})\s*{DASH}\s*(\d{{2}}:\d{{2}})",
        re.DOTALL
    )

    for i, dm in enumerate(date_iter):
        date_str = dm.group(1)
        d = _parse_flexible_date(date_str)
        if d is None:
            continue

        start_idx = dm.end()
        end_idx = date_iter[i + 1].start() if i + 1 < len(date_iter) else len(text)
        chunk = text[start_idx:end_idx]

        # Verzamel ALLE matches binnen dit datumblok
        seen = set()  # voor deduplicatie binnen dezelfde dag
        for m in service_re.finditer(chunk):
            start_s, end_s = _fix_2400(m.group(1)), _fix_2400(m.group(2))

            try:
                sdt = datetime.combine(d, datetime.strptime(start_s, "%H:%M").time())
                edt = datetime.combine(d, datetime.strptime(end_s,   "%H:%M").time())
            except ValueError:
                # Onverwachte tijdnotatie: sla over
                continue

            # Over-middernacht
            if edt <= sdt:
                edt += timedelta(days=1)

            key = (sdt.isoformat(), edt.isoformat())
            if key in seen:
                continue
            seen.add(key)

            events.append({
                # Als je het type (AVOND-/NACHT-/...) wilt meenemen: haal m.group(0) op en extraheren.
                "summary": "DIENST",
                "start": sdt,
                "end": edt,
            })

    return events


# =========================
# ICS GENERATOR
# =========================

def create_ics(events):
    cal = Calendar()
    cal.add("prodid", "-//Rooster Webtool//NL")
    cal.add("version", "2.0")

    for ev in events:
        ical_ev = Event()
        ical_ev.add("summary", ev["summary"])
        ical_ev.add("dtstart", ev["start"])
        ical_ev.add("dtend", ev["end"])
        ical_ev.add("dtstamp", datetime.utcnow())
        cal.add_component(ical_ev)

    return cal.to_ical()


# =========================
# UPLOAD ENDPOINT
# =========================

@app.route("/upload", methods=["POST"])
def upload():
    try:
        f = request.files.get("file")
        if not f:
            return jsonify(error="Geen bestand ontvangen."), 400

        raw_text = extract_text_from_pdf(f)
        events = extract_events_from_text(raw_text)

        # Debug naar Render logs (handig bij issues)
        print(f"[DEBUG] PDF-tekst: {len(raw_text)} chars | gevonden diensten: {len(events)}")

        if not events:
            return jsonify(error="Geen diensten gevonden in dit PDF-bestand."), 400

        ics_data = create_ics(events)
        return send_file(
            io.BytesIO(ics_data),
            as_attachment=True,
            download_name="rooster.ics",
            mimetype="text/calendar"
        )

    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        return jsonify(error=f"Verwerken mislukt ({type(e).__name__})."), 500


# =========================
# MAIN
# =========================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
