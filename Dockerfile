FROM python:3.14-alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY proxy.py .

# Probes the listener only. A deeper check would need credentials and would
# spend an aKeyless call every interval.
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=3 \
  CMD python3 -c "import socket; socket.create_connection(('localhost', 8080), timeout=2)" || exit 1

USER nobody

ENTRYPOINT ["python3", "proxy.py"]
