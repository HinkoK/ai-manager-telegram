FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Контейнер не под root: у бота нет причин иметь права на запись куда-либо.
RUN useradd --create-home --uid 10001 app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# Миграции и загрузка базы знаний запускаются командой внутри контейнера.
COPY migrations ./migrations
COPY scripts ./scripts

# knowledge/ монтируется томом из compose: в образ базу знаний не запекаем,
# иначе каждая правка текста требовала бы пересборки.
ENV KNOWLEDGE_DIR=/knowledge

USER app
EXPOSE 8000

# Healthcheck дёргает тот же /healthz, что и человек: он краснеет, когда цикл
# поллинга завис, и Docker перезапускает контейнер.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# Exec-форма, чтобы SIGTERM пришёл прямо в uvicorn и остановка была аккуратной.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
