# Egress proxy for the JobFinderAgent reference deployment (see squid.conf).
FROM alpine:3.22

RUN apk add --no-cache "squid=~6.12" \
    && mkdir -p /run/squid \
    && chown squid:squid /run/squid

COPY squid.conf /etc/squid/squid.conf
COPY entrypoint.sh /usr/local/bin/egress-proxy-entrypoint
RUN chmod 0644 /etc/squid/squid.conf && chmod 0755 /usr/local/bin/egress-proxy-entrypoint

USER squid
EXPOSE 3128
ENTRYPOINT ["/usr/local/bin/egress-proxy-entrypoint"]
