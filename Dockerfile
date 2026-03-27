FROM python:3.11-slim

WORKDIR /app

COPY proxy_upnp.py /app/proxy_upnp.py
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh \
    && useradd --create-home --shell /usr/sbin/nologin app

USER app

EXPOSE 18080/tcp
EXPOSE 1900/udp

ENTRYPOINT ["/app/docker-entrypoint.sh"]
