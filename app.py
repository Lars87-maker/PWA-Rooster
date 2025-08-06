from flask import Flask, render_template, send_from_directory, request, send_file
from datetime import datetime, timedelta
from icalendar import Calendar, Event
import fitz  # PyMuPDF
import re
import io

app = Flask(__name__, static_folder="static", template_folder="templates")

# Homepage
@app.route("/")
def index():
    return render_template("index.html")

# Manifest.json
@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json", mimetype="application/manifest+json")

# Service worker
@app.route("/service-worker.js")
def service_worker():
    return send_from_directory("static", "service-worker.js", mimetype="application/javascript")

# ---------------- PDF -> ICS functionaliteit ----------------

def correct_time(time_str):
    return "23:59" if time_str == "24:00" else time_str

def extract_events_from_text(text):
    events = []
    lines = text.splitlines()
    dienst_entries = []
    activiteiten_per_datum = {}
    current_date = None
    onder_soort_kop = False

    for line in lines:
        if 'Soort' in line and 'Start-Eind' in line:
            onder_soort_kop = True
            continue
        if not onder_soort_kop:
            continue
        date_match = re.match(r"(\d{2}/\d{2}/\d{4})", line)
        if date_match:
            current_date = datetime.strptime(date_match.group(1), "%d/%m/%Y")
            continue
        activity_match = re.search(r"(?:Memo:\s*)?Activiteit:\s*(.+)", line)
        if activity_match and current_date:
            activiteiten_per_datum[current_date.date()] = activity_match.group(1).strip()
            continue
        dienst_match = re.search(r"(DIENST|CONSIG).*?(\d{2}:\d{2})-(\d{2}:\d{2})", line, re.IGNORECASE)
        if dienst_match and current_date:
            dienst_type = dienst_match.group(1).upper()
            start_str = correct_time(dienst_match.group(2))
            end_str = correct_time(dienst_match.group(3))
            start_dt = datetime.strptime(f"{current_date.date()} {start_str}", "%Y-%m-%d %H:%M")
            end_dt = datetime.strptime(f"{current_date.date()} {end_str}", "%Y-%m-%d %H:%M")
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)
            dienst_entries.append({
                "date": current_date.date(),
                "type": dienst_type,
                "start": start_dt,
                "end": end_dt
            })

    for entry in dienst_entries:
        activiteit = activiteiten_per_datum.get(entry["date"], "")
        summary = f"{entry['type']} - {activiteit}" if activiteit else entry['type']
        events.append({
            "summary": summary,
            "start": entry['start'],
            "end": entry['end']
        })
    return events

def create_ics(events):
    cal = Calendar()
    for event in events:
        e = Event()
        e.add('summary', event['summary'])
        e.add('dtstart', event['start'])
        e.add('dtend', event['end'])
        e.add('dtstamp', datetime.now())
        cal.add_component(e)
    return cal.to_ical()

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return "Geen bestand geüpload", 400
    file = request.files["file"]
    if file.filename == "":
        return "Geen bestand geselecteerd", 400

    # PDF uitlezen
    doc = fitz.open(stream=file.read(), filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    events = extract_events_from_text(text)

    if not events:
        return "Geen diensten gevonden in dit PDF-bestand", 400

    # ICS bestand genereren
    ics_data = create_ics(events)
    return send_file(
        io.BytesIO(ics_data),
        as_attachment=True,
        download_name="rooster.ics",
        mimetype="text/calendar"
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
