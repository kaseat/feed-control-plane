FROM python:3.11-slim

WORKDIR /opt/node-control

RUN mkdir -p /etc/node-control
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/*

COPY hub ./hub
COPY builder ./builder
COPY schemas ./schemas
COPY seeds ./seeds
COPY README.md ./

RUN chmod +x /opt/node-control/hub/scripts/*.sh

EXPOSE 18080

CMD ["/opt/node-control/hub/scripts/bootstrap.sh"]
