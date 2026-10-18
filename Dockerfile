FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home decisionos
COPY --chown=decisionos:decisionos src ./src
COPY --chown=decisionos:decisionos web ./web
ENV PYTHONUNBUFFERED=1 PORT=8080 COS_WORKER_INLINE=1
USER decisionos
EXPOSE 8080
# One process keeps the in-container worker and its Firestore lease simple; threads serve concurrent requests.
CMD exec gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300 web.app:app
