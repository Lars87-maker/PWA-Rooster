from flask import Flask, render_template, send_from_directory, request, send_file, jsonify
from datetime import datetime, timedelta, date, time as dtime
from icalendar import Calendar, Event
import fitz  # PyMuPDF
import re
import io
import os

app = Flask(__name__, static_folder="static", template_folder="templates")


# ---------------- PWA ROUTES ----------------

@app.route("/")
def index():
    return render_template("index.html")

# Serve manifest en iconen ook op root-paden (zodat PWA ze makkelijk vindt)
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
    # Belangrijk: correcte mimetype voor SW
    return send_from_directory("static", "service-worker.js", mimetype="application/javascript")


# ---------------- PDF -> TEXT -> EVENTS ----------------

DATE_RE = r"(\d{2}-\d{2}-(?:\d{2}|\d{4}))"  # 10-09-25 of 10-09-2025
TIME_RE = r"(\d{2}:\d{2})-(\d{2}:\d{2})"

# Match met tolerantie voor regeleinden/kolommen/whitespace
PATTERN = re.compile(
    rf"{DATE_RE}.{0,120}?DIENST\s+{TIME_RE}",
    re.DOTALL | re.IGNORECASE
)

def _parse_date(dstr: str) -> date:
    # Ondersteun zowel dd-mm-yy als dd-mm-yyyy
    for fmt in ("%d-%m-%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(dstr, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Ongeldige datum: {dstr}")

def _correct_time_2400(t: str) -> str:
    # Sommige roosters gebruiken 24:00
    return "23:59" if t == "24:00" else t

def extract_text_from_pdf(file_storage) -> str:
    # Lees stream éénmaal met PyMuPDF
    data = file_storage.read()
    with fitz.open(stream=data, filetype="pdf") as doc:
        parts = []
        for page in doc:
            parts.append(page.get_text())
    return "\n".join(parts)

def extract_events_from_text(text: str):
    events = []
    for date_s, start_s, end_s in PATTERN.findall(text):
        d = _parse_date(date_s)
        start_s = _correct_time_2400(start_s)
        end_s = _correct_time_2400(end_s)

        sdt = datetime.combine(d, datetime.strptime(start_s, "%H:%M").time())
        edt = datetime.combine(d, datetime.strptime(end_s, "%H:%M").time())
        # Over-middernacht
        if edt <= sdt:
            edt += timedelta(days=1)

        events.append({
            "summary": "DIENST",
            "start": sdt,
            "end": edt,
        })
    return events


# ---------------- ICS GENERATOR ----------------

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


# ---------------- UPLOAD ENDPOINT ----------------

@app.route("/upload", methods=["POST"])
def upload():
    try:
        f = request.files.get("file")
        if not f:
            return jsonify(error="Geen bestand ontvangen."), 400

        text = extract_text_from_pdf(f)
        events = extract_events_from_text(text)

        if not events:
            # Laat de client weten dat parsing is gelukt maar geen matches zijn gevonden
            return jsonify(error="Geen diensten gevonden in dit PDF-bestand."), 400

        ics_data = create_ics(events)
        return send_file(
            io.BytesIO(ics_data),
            as_attachment=True,
            download_name="rooster.ics",
            mimetype="text/calendar"
        )

    except Exception as e:
        # Log intern: print naar stdout (Render logs)
        print(f"[ERROR] {type(e).__name__}: {e}")
        return jsonify(error=f"Verwerken mislukt ({type(e).__name__})."), 500


# ---------------- MAIN ----------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
