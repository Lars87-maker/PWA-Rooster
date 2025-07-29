import streamlit as st
import fitz  # PyMuPDF
from icalendar import Calendar, Event
from datetime import datetime, timedelta
import re

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

# Streamlit interface
st.set_page_config(page_title="Rooster naar ICS", layout="centered")
st.title("📅 Rooster PDF naar ICS Converter")
uploaded_file = st.file_uploader("Upload je PDF-rooster", type=["pdf"])

if uploaded_file:
    doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
    text = "\n".join(page.get_text() for page in doc)
    events = extract_events_from_text(text)
    if events:
        ics_data = create_ics(events)
        st.success("ICS-bestand gegenereerd!")
        st.download_button("💾 Download ICS-bestand", data=ics_data, file_name="rooster.ics", mime="text/calendar")
    else:
        st.warning("Geen diensten gevonden in dit PDF-bestand.")



# --- PWA Injectie ---
st.markdown(
    """
    <link rel="manifest" href="/manifest.json">
    <script>
      if ("serviceWorker" in navigator) {
        navigator.serviceWorker.register("/service-worker.js")
          .then(() => console.log("Service Worker Registered"))
          .catch(err => console.error("Service Worker failed:", err));
      }
    </script>
    """ ,
    unsafe_allow_html=True

    # --- PWA Injectie ---

BASE_URL = "https://raw.githubusercontent.com/Lars87-maker/PWA-Rooster/main"

st.markdown(
    f"""
    <link rel="manifest" href="{BASE_URL}/manifest.json">
    <script>
      if ("serviceWorker" in navigator) {{
        navigator.serviceWorker.register("{BASE_URL}/service-worker.js")
          .then(() => console.log("Service Worker Registered"))
          .catch(err => console.error("Service Worker failed:", err));
      }}
    </script>
    """,
    unsafe_allow_html=True
)

)
