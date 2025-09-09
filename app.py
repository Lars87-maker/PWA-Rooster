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
    - normaliseer regeleinden
    """
    s = (
        s.replace("\u00A0", " ")
         .replace("\u2013", "-")
         .replace("\u2014", "-")
    )
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

def _clean_text_short(s: str, limit: int = 80) -> str:
    s = s.strip(" :/.-–—\t\n\r")
    s = re.sub(r"\s+", " ", s)
    return s[:limit]

def _service_title(service_raw: str) -> str:
    """
    Maak een nette servicetitel:
      - 'CONSIG' → 'Consig'
      - varianten met 'DIENST' → 'Dienst'
      - andere woorden → Title Case
    """
    t = _clean_text_short(service_raw)
    up = t.upper()
    if "CONSIG" in up:
        return "Consig"
    if "DIENST" in up:
        return "Dienst"
    if "RUST" in up:
        return "Rust"
    return t.title()

def _activity_tag_from_text(text: str) -> str:
    """
    Haal een korte activity tag uit tekst (bv. 'wijkzorg', 'achterwacht', 'surveilleren', ...).
    We letten op 'Memo: Activiteit: ...' en op werkwoord/keywords in regels zoals
    'Uitvoeren wijkzorg Zandvoort'.
    """
    # 1) Memo: Activiteit: <...>
    m = re.search(r"(?i)Memo:\s*Activiteit\s*:\s*(.+)", text)
    if m:
        val = _clean_text_short(m.group(1).lower())
        # vaak staat er 'wijkzorg', 'achterwacht', 'operationeel coordinator', ...
        # neem het eerste betekenisvolle woord of tweetal
        # prioriteer bekende keywords
        known = [
            "wijkzorg", "achterwacht", "surveilleren", "operationeel coördineren",
            "operationeel coordinator", "toezicht houden", "trainen", "werkverdelen",
            "monitoren", "evenementen"
        ]
        for k in known:
            if k in val:
                return k.title()
        # anders: pak eerste 2 woorden
        return " ".join(val.split()[:2]).title()

    # 2) Regels met 'Uitvoeren wijkzorg', 'Surveilleren', 'Toezicht houden', ...
    verbs = [
        r"(?i)\buitvoeren\s+wijkzorg\b",
        r"(?i)\bsurveilleren\b",
        r"(?i)\bwerkverdelen(?:\s+en\s+monitoren)?\b",
        r"(?i)\bmonitoren\b",
        r"(?i)\boperationeel\s+co[oö]rdineren\b",
        r"(?i)\btoezicht\s+houden\b",
        r"(?i)\btrainen\b",
        r"(?i)\bevenementen\b",
        r"(?i)\bachterwacht\b",
        r"(?i)\wijkzorg\b",
    ]
    for rx in verbs:
        m2 = re.search(rx, text)
        if m2:
            ph = m2.group(0)
            # normaliseer aantal woorden
            ph = _clean_text_short(ph.title())
            # 'Uitvoeren Wijkzorg' → 'Wijkzorg'
            ph = re.sub(r"(?i)^Uitvoeren\s+", "", ph).strip()
            return ph

    return ""

# =========================
# PARSER (per datumblok, neem ALLE diensten en bepaal Type/Activiteit)
# =========================

def extract_events_from_text(raw_text: str):
    """
    Parser die ALLE diensten per dag meeneemt, en de ICS-titel bouwt met Type/Activiteit:
      - SUMMARY = 'Consig' als servicetype CONSIG is,
                  anders SUMMARY = <ActivityTag> (bijv. 'Wijkzorg'),
                  anders 'Dienst'.
      - DESCRIPTION bevat Type + Activiteit + Datum + Tijd.
      - GEEN weekdag in de titel.
    """
    text = _normalize_text(raw_text)
    events = []

    # Vind alle datummatches
    date_iter = list(re.finditer(DATE_RE, text))
    if not date_iter:
        return events

    # Service match:
    #   - CONSIG
    #   - [Rust] (negeren we later)
    #   - woorden met 'DIENST' (bv. 'AVOND DIENST', 'DIENST')
    SERVICE = r"(CONSIG|\[?\s*Rust\s*\]?|[A-Za-zÀ-ÖØ-öø-ÿ\s]{0,20}DIENST[A-Za-zÀ-ÖØ-öø-ÿ\s]{0,10})"

    service_re = re.compile(
        rf"(?i)\b{SERVICE}\b[^0-9]{{0,60}}(\d{{2}}:\d{{2}})\s*{DASH}\s*(\d{{2}}:\d{{2}})",
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

        # Voor activiteit-detectie werken we naast substring ook met een lokale context
        lines = [ln.strip() for ln in chunk.split("\n")]

        def find_activity_near(pos_start: int, pos_end: int) -> str:
            """
            Zoek naar 'Activiteit' rond de match (±400 tekens om de match).
            """
            ctx_before = chunk[max(0, pos_start - 400):pos_start]
            ctx_after  = chunk[pos_end: min(len(chunk), pos_end + 400)]

            # 1) Meest voorkomend: na de dienst staat 'Memo: Activiteit: ...'
            tag = _activity_tag_from_text(ctx_after)
            if tag:
                return tag

            # 2) Soms staat 'Activiteit' er vlak vóór
            tag = _activity_tag_from_text(ctx_before)
            if tag:
                return tag

            # 3) Heel lokale fallback: pak één regel na de match
            line_start = chunk[:pos_start].count("\n")
            for j in range(line_start, min(line_start + 6, len(lines))):
                t = _activity_tag_from_text(lines[j])
                if t:
                    return t
            return ""

        # Verzamel ALLE services binnen dit datumblok
        seen = set()
        for m in service_re.finditer(chunk):
            service_raw = m.group(1)
            start_s, end_s = _fix_2400(m.group(2)), _fix_2400(m.group(3))

            # [Rust] negeren
            if re.search(r"(?i)rust", service_raw):
                continue

            try:
                sdt = datetime.combine(d, datetime.strptime(start_s, "%H:%M").time())
                edt = datetime.combine(d, datetime.strptime(end_s,   "%H:%M").time())
            except ValueError:
                continue

            if edt <= sdt:
                edt += timedelta(days=1)

            key = (sdt.isoformat(), edt.isoformat(), service_raw.upper())
            if key in seen:
                continue
            seen.add(key)

            service_kind = _service_title(service_raw)  # 'Consig' / 'Dienst' / ...
            activity_tag = find_activity_near(m.start(), m.end())  # bv. 'Wijkzorg'

            # --- SUMMARY keuze ---
            # 1) CONSIG domineert
            if service_kind.lower() == "consig":
                summary = "Consig"
            # 2) Anders, als we een activiteit hebben (bv. Wijkzorg), gebruik die
            elif activity_tag:
                summary = activity_tag
            # 3) Anders generiek
            else:
                summary = "Dienst"

            # Beschrijving (geen weekdag in titel; hier mag datum/tijd)
            desc_parts = [f"Type: {service_kind}"]
            if activity_tag:
                desc_parts.append(f"Activiteit: {activity_tag}")
            desc_parts.append(f"Datum: {d.strftime('%d-%m-%Y')}")
            desc_parts.append(f"Tijd: {start_s} - {end_s}")
            description = "\n".join(desc_parts)

            events.append({
                "summary": summary,
                "description": description,
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
        if ev.get("description"):
            ical_ev.add("description", ev["description"])
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
        if events:
            print("[DEBUG] Voorbeeld:", events[0]["summary"], "|", events[0].get("description","")[:80])

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
