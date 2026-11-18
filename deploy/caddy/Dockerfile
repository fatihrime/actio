# The proxy as an image: the official Caddy image, pinned, with Actio's config in it.
# compose.yaml builds it from this folder:
#   docker build deploy/caddy
#
# The config is part of the image, as the API's and the web app's code are part of
# theirs. A change to the Caddyfile is a new image, and `make up` or `make dev` starts
# the proxy anew from it: nothing is mounted, and nothing watches the file. See
# docs/adr/0023-answers-that-wait-their-turn.md.

FROM caddy:2.11.9-alpine@sha256:0bbb055bda6793c999bde78b1b5ce3e89f829fb031af2cf50056ea1246559fee

COPY Caddyfile /etc/caddy/Caddyfile

# A mistake in the config stops the build, and the proxy that is running keeps serving
# with the config it has. The upstreams come from compose.yaml when the proxy runs;
# here they only have to be addresses.
RUN ACTIO_API_UPSTREAM=api:8000 ACTIO_WEB_UPSTREAM=web:3000 \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
