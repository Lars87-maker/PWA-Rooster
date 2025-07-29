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

# Streamlit configuratie (disable telemetry)
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

# Expose poort die door Vercel wordt toegewezen
EXPOSE 8501

# Start commando voor Streamlit (met dynamische poort van Vercel)
CMD streamlit run rooster_webtool_pwa.py --server.port=$PORT --server.address=0.0.0.0
