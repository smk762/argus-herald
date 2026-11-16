#!/bin/sh
# Install user services so the watcher and review UI start at login, and at
# boot when lingering is enabled.
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
bin="$root/.venv/bin/argus-herald"
if [ ! -x "$bin" ]; then
  echo "Missing $bin. From $root: python -m venv .venv && .venv/bin/pip install -e ." >&2
  exit 1
fi

dest="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$dest"

# Units from before the rename (pip-release-*) are stopped and removed; the new ones replace them.
for name in pip-release-watch pip-release-ui; do
  if [ -f "$dest/$name.service" ]; then
    systemctl --user disable --now "$name.service" || true
    rm -f "$dest/$name.service"
    echo "Replaced $name.service with argus-herald-${name#pip-release-}.service"
  fi
done
for name in argus-herald-watch argus-herald-ui; do
  sed -e "s|@ROOT@|$root|g" -e "s|@BIN@|$bin|g" \
    "$root/contrib/systemd/$name.service" > "$dest/$name.service"
done

systemctl --user daemon-reload
systemctl --user enable --now argus-herald-watch.service argus-herald-ui.service

if loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -qx yes; then
  echo "Lingering is already enabled, so these start at boot."
else
  if loginctl enable-linger "$USER"; then
    echo "Lingering enabled. The watcher and review UI start at boot."
  else
    echo "Could not enable lingering. The services start when you log in." >&2
  fi
fi

systemctl --user --no-pager --full status argus-herald-watch.service argus-herald-ui.service
