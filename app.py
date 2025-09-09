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

def _clean_label(label: str) -> str:
    """
    Schoon de gevonden dienstnaam op (Avonddienst/Nachtdienst/Dienst/...).
    """
    label = label.strip(" :/.-–—\t\n\r")
    label = re.sub(r"\s+", " ", label)
    pretty = label.title()
    return pretty if pretty else "Dienst"

def _clean_activity(activity: str) -> str:
    """
    Schoon de gevonden activiteit op.
    Neemt de eerste 'zin' of kolomwaarde na 'Activiteit'.
    """
    if not activity:
        return ""
    # pak slechts 80 tekens om uitwaaieren te voorkomen
    activity = activity.strip(" :/.-–—\t\n\r")[:80]
    activity = re.sub(r"\s+", " ", activity)
    return activity


# =========================
# PARSER (per datumblok, neem ALLE diensten + label + activiteit)
# =========================

def extract_events_from_text(raw_text: str):
    """
    Parser die ALLE diensten per dag meeneemt, het type dienst én de activiteit koppelt.

    Strategie:
      1) Vind elke datum (dd-mm[-yy] of dd/mm[-yy]).
      2) Neem de tekst van deze datum t/m de volgende datum (datumblok).
      3) Binnen het blok: vind ALLE matches van '<LABEL met DIENST> HH:MM–HH:MM'.
         - <LABEL> matcht 'DIENST', 'AVONDDIENST', 'AVOND DIENST', 'NACHTDIENST', etc.
      4) Voor elke match:
         - zoek in de nabijheid (context window) naar 'ACTIVITEIT: <...>' en neem de waarde als activiteit.
           (We zoeken zowel vóór als ná de dienst-match, met tolerantie voor kolomsprongen.)
      5) Dedupliceer identieke tijdvakken binnen dezelfde dag.
    """
    text = _normalize_text(raw_text)
    events = []

    date_iter = list(re.finditer(DATE_RE, text))
    if not date_iter:
        return events

    # LABEL bevat DIENST-woord(groep); laat samenstellingen toe en spaties ertussen.
    LABEL = r"([A-Za-zÀ-ÖØ-öø-ÿ]{0,20}(?:\s*[A-Za-zÀ-ÖØ-öø-ÿ]{0,20})?\s*DIENST(?:\s*[A-Za-zÀ-ÖØ-öø-ÿ]{0,20})?)"

    # Match dienstlabel + tijden
    service_re = re.compile(
        rf"(?i)\b{LABEL}\b[^0-9]{{0,60}}(\d{{2}}:\d{{2}})\s*{DASH}\s*(\d{{2}}:\d{{2}})",
        re.DOTALL
    )

    # Activiteit-patronen (meerdere varianten)
    activity_line_res = [
        re.compile(r"(?i)ACTIVITEIT\s*[:\-]?\s*(.+)"),  # Activiteit: Balie SEH
        re.compile(r"(?i)ACTIVITEIT\s*$"),              # 'Activiteit' op zichzelf → volgende niet-lege regel oppakken (doen we handmatig)
    ]

    for i, dm in enumerate(date_iter):
        date_str = dm.group(1)
        d = _parse_flexible_date(date_str)
        if d is None:
            continue

        start_idx = dm.end()
        end_idx = date_iter[i + 1].start() if i + 1 < len(date_iter) else len(text)
        chunk = text[start_idx:end_idx]

        # Voor activiteit-detectie werken we ook met regels
        lines = [ln.strip() for ln in chunk.split("\n")]

        def find_activity_near(pos_start: int, pos_end: int) -> str:
            """
            Zoek 'Activiteit' dicht in de buurt van de match.
            1) Directe regel met 'Activiteit: ...'
            2) Een regel 'Activiteit' en de eerstvolgende niet-lege regel als waarde
            3) Als fallback: neem het dichtstbijzijnde 'Activiteit: ...' binnen ±400 tekens
            """
            ctx_before = chunk[max(0, pos_start - 400):pos_start]
            ctx_after  = chunk[pos_end: min(len(chunk), pos_end + 400)]

            # 1) Directe 'Activiteit: ...' in na-context (meest gebruikelijk)
            for rx in activity_line_res:
                m = rx.search(ctx_after)
                if m and m.lastindex == 1:
                    return _clean_activity(m.group(1))

            # 2) Losse 'Activiteit' regel gevolgd door een waarde
            #    We doorzoeken regels in de buurt (±8 regels vanaf match)
            #    Heuristiek: vind regelindex van begin van match
            start_line_idx = chunk[:pos_start].count("\n")
            window = lines[start_line_idx: start_line_idx + 12]
            for idx, ln in enumerate(window):
                if re.fullmatch(r"(?i)activiteit", ln):
                    # pak eerstvolgende niet-lege regel als waarde
                    for j in range(idx + 1, min(idx + 5, len(window))):
                        if window[j]:
                            return _clean_activity(window[j])

            # 3) Fallback: 'Activiteit: ...' in voor-context
            for rx in activity_line_res:
                m = rx.search(ctx_before)
                if m and m.lastindex == 1:
                    return _clean_activity(m.group(1))

            return ""

        # Verzamel ALLE matches binnen dit datumblok
        seen = set()
        for m in service_re.finditer(chunk):
            raw_label = m.group(1)
            start_s, end_s = _fix_2400(m.group(2)), _fix_2400(m.group(3))

            try:
                sdt = datetime.combine(d, datetime.strptime(start_s, "%H:%M").time())
                edt = datetime.combine(d, datetime.strptime(end_s,   "%H:%M").time())
            except ValueError:
                continue

            if edt <= sdt:
                edt += timedelta(days=1)

            key = (sdt.isoformat(), edt.isoformat())
            if key in seen:
                continue
            seen.add(key)

            label = _clean_label(raw_label)
            if not label or "Dienst" not in label.title():
                label = "Dienst"

            # Activiteit zoeken rond deze match
            act = find_activity_near(m.start(), m.end())

            # Bouw samenvatting & beschrijving
            if act:
                summary = f"{act} – {label}"
                description = f"Activiteit: {act}\nSoort: {label}\nDatum: {d.strftime('%d-%m-%Y')}\nTijd: {start_s} - {end_s}"
            else:
                summary = label
                description = f"Soort: {label}\nDatum: {d.strftime('%d-%m-%Y')}\nTijd: {start_s} - {end_s}"

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
            print("[DEBUG] Voorbeeld:", events[0]["summary"])

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
