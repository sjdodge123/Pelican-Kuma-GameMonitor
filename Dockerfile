FROM python:3.12-slim

ARG SUPERCRONIC_VERSION=v0.2.33
ARG SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64
ARG SUPERCRONIC_SHA1_URL=https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-amd64.sha1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl tini \
 && rm -rf /var/lib/apt/lists/*

# supercronic
RUN curl -fsSL "$SUPERCRONIC_URL" -o /usr/local/bin/supercronic \
 && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/app/
COPY crontab /etc/supercronic/crontab
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/entrypoint.sh"]