# Egress proxy for worker containers (PRD 10.3, ADR-0013).
# Workers sit on an internal Docker network with no route out; this proxy is their only
# way out, and it forwards only to allowlisted hosts. The config is mounted at run time.
FROM alpine:3.20
RUN apk add --no-cache tinyproxy
USER nobody
EXPOSE 8888
ENTRYPOINT ["tinyproxy", "-d", "-c", "/etc/codeit/tinyproxy.conf"]
