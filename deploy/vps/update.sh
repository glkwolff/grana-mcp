#!/bin/sh
# Atualiza o grana na VPS: wheel novo -> imagem nova -> reinicia o web.
# Uso: deploy/vps/update.sh <host-ssh>   (o host precisa de sudo sem senha para o usuário grana)
set -eu
host=${1:?uso: deploy/vps/update.sh <host-ssh>}
cd "$(dirname "$0")/../.."
rm -rf dist && uv build -q
tar -cf - dist deploy/Containerfile | ssh -o BatchMode=yes "$host" '
  set -e; cd /
  sudo -n -u grana sh -c "rm -rf /home/grana/build && mkdir -p /home/grana/build && tar -C /home/grana/build -xf -"
  sudo -n -u grana -H env XDG_RUNTIME_DIR=/run/user/$(id -u grana) sh -c "
    cd /home/grana/build && podman build -q -f deploy/Containerfile -t grana-web . >/dev/null &&
    podman image prune -f >/dev/null &&
    systemctl --user restart grana-web && sleep 5 && systemctl --user is-active grana-web"'
