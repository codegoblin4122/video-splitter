FROM python:3.12-slim

# Video tools
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8080"]
