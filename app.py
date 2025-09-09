from flask import Flask, render_template, send_from_directory, request, send_file, jsonify
from datetime import datetime, timedelta, date
from icalendar import Calendar, Event
import fitz  # PyMuPDF
import re
import io
import os
from collections import defaultdict

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
    data = file_storage.read()
    with fitz.open(stream=data, filetype="pdf") as doc:
        parts = [page.get_text() for page in doc]
    return "\n".join(parts)


# =========================
# PARSER HELPERS
# =========================

DATE_RE = r"(\d{2}[/-]\d{2}[/-](?:\d{2}|\d{4}))"
DASH = r"[-–—]"

def _normalize_text(s: str) -> str:
    s = (
        s.replace("\u00A0", " ")
         .replace("\u2013", "-")
         .replace("\u2014", "-")
    )
    return s.replace("\r\n", "\n").replace("\r", "\n")

def _parse_flexible_date(date_str: str) -> date | None:
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except ValueError:
            continue
    return None

def _fix_2400(t: str) -> str:
    return "23:59" if t == "24:00" else t

def _clean_text_short(s: str, limit: int = 80) -> str:
    s = s.strip(" :/.-–—\t\n\r")
    s = re.sub(r"\s+", " ", s)
    return s[:limit]

def _service_title(service_raw: str) -> str:
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
    m = re.search(r"(?i)Memo:\s*Activiteit\s*:\s*(.+)", text)
    if m:
        val = _clean_text_short(m.group(1).lower())
        known = [
            "wijkzorg", "achterwacht", "surveilleren", "operationeel coördineren",
            "operationeel coordinator", "toezicht houden", "trainen",
            "werkverdelen", "monitoren", "evenementen", "afhandelen meldingen"
        ]
        for k in known:
            if k in val:
                return k.title()
        return " ".join(val.split()[:3]).title()

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
        r"(?i)\bafhandelen\s+meldingen\b",
        r"(?i)\bwijkzorg\b",
    ]
    for rx in verbs:
        m2 = re.search(rx, text)
        if m2:
            ph = m2.group(0)
            ph = _clean_text_short(ph.title())
            ph = re.sub(r"(?i)^Uitvoeren\s+", "", ph).strip()
            return ph
    return ""

# =========================
# PARSER (alle diensten + type + activiteit)
# =========================

def extract_events_from_text(raw_text: str):
    """
    - Neemt ALLE diensten per dag mee
    - Vindt servicetype (Consig/Dienst/…)
    - Vindt activiteit rondom de match
    - Retourneert ruwe events; wordt daarna opgeschoond (merge CONSIG, verwijder all-day artefacts)
    """
    text = _normalize_text(raw_text)
    events = []

    date_iter = list(re.finditer(DATE_RE, text))
    if not date_iter:
        return events

    # Service match:
    SERVICE = r"(CONSIG|\[?\s*Rust\s*\]?|[A-Za-zÀ-ÖØ-öø-ÿ\s]{0,20}DIENST[A-Za-zÀ-ÖØ-öø-ÿ\s]{0,10})"
    service_re = re.compile(
        rf"(?i)\b{SERVICE}\b[^0-9]{{0,80}}(\d{{2}}:\d{{2}})\s*{DASH}\s*(\d{{2}}:\d{{2}})",
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
        lines = [ln.strip() for ln in chunk.split("\n")]

        def find_activity_near(pos_start: int, pos_end: int) -> str:
            ctx_before = chunk[max(0, pos_start - 500):pos_start]
            ctx_after  = chunk[pos_end: min(len(chunk), pos_end + 500)]
            tag = _activity_tag_from_text(ctx_after)
            if tag:
                return tag
            tag = _activity_tag_from_text(ctx_before)
            if tag:
                return tag
            line_start = chunk[:pos_start].count("\n")
            for j in range(line_start, min(line_start + 8, len(lines))):
                t = _activity_tag_from_text(lines[j])
                if t:
                    return t
            return ""

        seen = set()
        for m in service_re.finditer(chunk):
            service_raw = m.group(1)
            if re.search(r"(?i)rust", service_raw):
                continue  # sla 'Rust' over

            start_s, end_s = _fix_2400(m.group(2)), _fix_2400(m.group(3))
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

            service_kind = _service_title(service_raw)   # 'Consig' / 'Dienst' / …
            activity_tag = find_activity_near(m.start(), m.end())

            # SUMMARY-regel: CONSIG domineert; anders activiteit; anders Dienst
            if service_kind.lower() == "consig":
                summary = "Consig"
            elif activity_tag:
                summary = activity_tag
            else:
                summary = "Dienst"

            desc_parts = [f"Type: {service_kind}"]
            if activity_tag:
                desc_parts.append(f"Activiteit: {activity_tag}")
            desc_parts.append(f"Datum: {d.strftime('%d-%m-%Y')}")
            desc_parts.append(f"Tijd: {start_s} - {end_s}")
            description = "\n".join(desc_parts)

            events.append({
                "summary": summary,
                "type": service_kind,       # bewaar origin type voor post-processing
                "activity": activity_tag,
                "start": sdt,
                "end": edt,
                "description": description,
            })

    return events


# =========================
# OPSCHONEN: merge CONSIG + verwijder all-day artefacts
# =========================

def _same_day(dt1: datetime, dt2: datetime) -> bool:
    return dt1.date() == dt2.date()

def post_process_events(events: list) -> list:
    if not events:
        return events

    # 1) Sorteer
    events.sort(key=lambda e: (e["start"], e["end"], e["summary"]))

    # 2) Merge aaneengesloten CONSIG-blokken (end == next.start)
    merged = []
    for ev in events:
        if ev["type"].lower() != "consig":
            merged.append(ev)
            continue

        if merged and merged[-1]["type"].lower() == "consig" and merged[-1]["end"] == ev["start"]:
            # Plak aan vorige CONSIG vast
            merged[-1]["end"] = ev["end"]
            # Update beschrijving (alleen tijden en datumrange aanpassen)
            start_dt = merged[-1]["start"]
            end_dt = merged[-1]["end"]
            merged[-1]["description"] = (
                f"Type: Consig\n"
                f"Periode: {start_dt.strftime('%d-%m-%Y %H:%M')} → {end_dt.strftime('%d-%m-%Y %H:%M')}"
            )
        else:
            # Maak CONSIG-beschrijving als periode (helder bij dagoverschrijding)
            if ev["type"].lower() == "consig":
                ev["description"] = (
                    f"Type: Consig\n"
                    f"Periode: {ev['start'].strftime('%d-%m-%Y %H:%M')} → {ev['end'].strftime('%d-%m-%Y %H:%M')}"
                )
            merged.append(ev)

    # 3) Verwijder ‘volledige dag’-artefacten (00:00–23:59) als er die dag andere events zijn
    cleaned = []
    by_day = defaultdict(list)
    for ev in merged:
        by_day[ev["start"].date()].append(ev)

    for day, day_events in by_day.items():
        # detect all-day artefacten
        all_day = [e for e in day_events if e["start"].time() == datetime.min.time()
                   and e["end"].time() in (datetime.strptime("23:59", "%H:%M").time(),
                                           datetime.strptime("00:00", "%H:%M").time())
                   and e["type"].lower() == "dienst"]
        if all_day and len(day_events) > len(all_day):
            # er zijn andere events deze dag → drop all-day diensten
            keep = [e for e in day_events if e not in all_day]
        else:
            keep = day_events
        cleaned.extend(keep)

    # 4) Final sort
    cleaned.sort(key=lambda e: (e["start"], e["end"], e["summary"]))
    return cleaned


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
        raw_events = extract_events_from_text(raw_text)
        events = post_process_events(raw_events)

        print(f"[DEBUG] Tekst: {len(raw_text)} chars | ruwe: {len(raw_events)} | na opschonen: {len(events)}")
        if events:
            print("[DEBUG] Voorbeeld:", events[0]["summary"], events[0]["start"], "→", events[0]["end"])

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
