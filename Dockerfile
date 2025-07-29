# Basis image: slanke Python 3.10
FROM python:3.10-slim

# Zorg dat pip up-to-date is
RUN pip install --upgrade pip

# Werkdirectory in de container
WORKDIR /app

# Vereisten kopiëren en installeren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Hele project kopiëren
COPY . .

# Streamlit configuratie (disable telemetry, standaard port 8501)
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Poort die Streamlit gebruikt
EXPOSE 8501

# Start commando voor Streamlit
CMD ["streamlit", "run", "rooster_webtool_pwa.py", "--server.port=8501", "--server.address=0.0.0.0"]
