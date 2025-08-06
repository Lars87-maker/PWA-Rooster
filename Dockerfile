# Gebruik een lichte Python base image
FROM python:3.10-slim

# Werkdirectory in container
WORKDIR /app

# Dependencies installeren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopieer alle bestanden in de container
COPY . .

# Expose de Render poort
ENV PORT=8080
EXPOSE 8080

# Start Flask app
CMD ["python", "app.py"]
