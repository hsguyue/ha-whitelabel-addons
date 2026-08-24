FROM nginx:1.27-alpine

# Overwrite the default server block with the white-label policy:
# only /api/* and /api/websocket are proxied to Home Assistant Core,
# everything else (the web UI, onboarding, auth pages) returns 404.
COPY rootfs/etc/nginx/conf.d/default.conf /etc/nginx/conf.d/default.conf
