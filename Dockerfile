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

# Streamlit configuratie
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_HEADLESS=true

# Vercel geeft een dynamische $PORT variabele
EXPOSE 8501

# Start Streamlit op de juiste poort
CMD ["sh", "-c", "streamlit run rooster_webtool_pwa.py --server.port=$PORT --server.address=0.0.0.0"]
