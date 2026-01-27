# Pelican-Kuma-GameMonitor
A docker container that automatically creates/removes Kuma Uptime entries for Pelican servers

To Run:
docker run -d --name Pelican-Kuma-GameMonitor \
  --restart unless-stopped \
  --env-file .env \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v pelican-kuma-cache:/data \
  ghcr.io/sjdodge123/Pelican-Kuma-GameMonitor:latest