FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY agent ./agent
COPY server ./server
COPY sim ./sim
EXPOSE 8000 5600/udp 5601
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "8000"]
