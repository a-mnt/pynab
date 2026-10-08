#!/usr/bin/env bash
#
# Update Pynab (button "Upgrade now" of the web site, or by hand).
#
#   upgrade.sh [--drivers=LIST]
#
# --drivers: drivers to update, as checked beforehand by the web site
#   (comma separated: sound_driver,ears_driver,rfid_driver,nfc_driver,
#   nabblockly; empty: none). Without it, every driver is checked.
#
# Only what the new version changes is redone (see install.sh --upgrade).

set -uo pipefail
trap 's=$?; echo "$0: Error on line "$LINENO": $BASH_COMMAND"; exit $s' ERR
IFS=$'\n\t'

root_dir=`sed -nE -e 's|WorkingDirectory=(.+)|\1|p' < /lib/systemd/system/nabd.service`
owner=`stat -c '%U' ${root_dir}`
uid=`stat -c '%u' ${root_dir}`

step="init"
drivers_arg=""
from_arg=""
for arg in "$@"; do
  case "${arg}" in
    install) step="install" ;;
    --drivers=*) drivers_arg="${arg}" ;;
    --from=*) from_arg="${arg}" ;;
  esac
done

# The whole "case" is read before it runs: git pull may replace this file.
case $step in
  "init")
    cd ${root_dir}
    sudo -u ${owner} touch /tmp/pynab.upgrade
    sudo chown ${owner} /tmp/pynab.upgrade
    sudo rm -f /tmp/pynab.upgrade.skipped
    echo "Updating Pynab - 0/14" > /tmp/pynab.upgrade
    echo "Updating Pynab"
    # Version before the update: install.sh compares it with the new one
    # to redo only what changed. Services keep running meanwhile.
    from=`sudo -u ${owner} git rev-parse HEAD 2>/dev/null || true`
    if [[ $EUID -ne ${uid} ]]; then
      sudo -u ${owner} git pull
    else
      git pull
    fi
    bash upgrade.sh install "--from=${from}" ${drivers_arg}
    exit $?
    ;;
  "install")
    cd ${root_dir}
    sudo -u ${owner} bash install.sh --upgrade ${from_arg} ${drivers_arg}
    status=$?
    sudo rm -f /tmp/pynab.upgrade
    exit ${status}
    ;;
esac
