FROM python:3.12-slim

# Force UTF-8 everywhere. python:3.12-slim defaults to the C/POSIX (ASCII)
# locale, so any non-ASCII character in a log line, card name, or response
# (e.g. "…", accented card names) raises 'ascii' codec can't encode and
# 502s the request. UTF-8 mode makes stdio and the default text encoding utf-8.
ENV PYTHONUTF8=1 \
    PYTHONIOENCODING=utf-8 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

EXPOSE 8443

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8443"]
