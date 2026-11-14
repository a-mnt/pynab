#!/bin/bash
# nab-monitor.sh — mode analyse du lapin.
#
# Relève l'état du système et des processus pynab pour repérer ce qui
# s'accumule (mémoire, fichiers ouverts, threads...) avant un freeze.
# Lancé toutes les 10 minutes par /etc/cron.d/nab-monitor, que la page
# Système de nabweb crée (switch « Mode analyse ») ou supprime.
#
# Format : une ligne par mesure, champs séparés par ';'. Le dernier champ
# liste les processus « nom=rss_ko,fds,threads » séparés par des espaces.

# Hors de /var/log, qui est en RAM sur DietPi : les mesures doivent
# survivre au redémarrage qui suit un freeze.
LOG=${NAB_MONITOR_LOG:-/var/lib/nab-monitor/nab-monitor.csv}
mkdir -p "$(dirname "$LOG")"
MAX_LINES=6000   # ~40 jours à 10 min
KEEP_LINES=4500

HEADER="date;uptime_s;mem_avail_kb;swap_used_kb;load1;temp_mc;throttled;root_pct;tmp_kb;nb_proc;wifi;processes"

if [ ! -s "$LOG" ]; then
  echo "$HEADER" > "$LOG"
elif [ "$(wc -l < "$LOG")" -gt "$MAX_LINES" ]; then
  { echo "$HEADER"; tail -n "$KEEP_LINES" "$LOG"; } > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

now=$(date '+%Y-%m-%dT%H:%M:%S')
uptime_s=$(cut -d. -f1 /proc/uptime)
mem_avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
swap_used=$(awk '/^SwapTotal:/ {t=$2} /^SwapFree:/ {f=$2} END {print t-f}' /proc/meminfo)
load1=$(cut -d' ' -f1 /proc/loadavg)
temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)
throttled=$(vcgencmd get_throttled 2>/dev/null | cut -d= -f2)
root_pct=$(df --output=pcent / | tail -1 | tr -dc '0-9')
tmp_kb=$(du -sk /tmp 2>/dev/null | cut -f1)
nb_proc=$(ls -d /proc/[0-9]* 2>/dev/null | wc -l)
wifi=$(cat /sys/class/net/wlan0/operstate 2>/dev/null)

procs=""
# Sélection sur le nom de l'exécutable (et non la ligne de commande
# complète, qui peut contenir n'importe quoi).
for pid in $(pgrep 'python|gunicorn|postgres'); do
  [ -r "/proc/$pid/status" ] || continue
  comm=$(cat "/proc/$pid/comm" 2>/dev/null)
  case "$comm" in
    gunicorn*) name=nabweb ;;
    postgres*) name=postgres ;;
    python*)
      # Démons pynab : "python -m nabclockd.nabclockd" -> nabclockd
      name=$(tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null \
        | awk 'prev == "-m" {print; exit} {prev = $0}' | cut -d. -f1)
      [ -n "$name" ] || name=python
      ;;
    *) name=$comm ;;
  esac
  name=$(printf '%s' "$name" | tr -cd 'A-Za-z0-9_-' | cut -c1-32)
  [ -n "$name" ] || continue
  rss=$(awk '/^VmRSS:/ {print $2}' "/proc/$pid/status")
  thr=$(awk '/^Threads:/ {print $2}' "/proc/$pid/status")
  fds=$(ls "/proc/$pid/fd" 2>/dev/null | wc -l)
  [ -n "$rss" ] && procs="$procs $name=$rss,${fds:-0},${thr:-0}"
done

echo "$now;$uptime_s;$mem_avail;$swap_used;$load1;$temp;$throttled;$root_pct;$tmp_kb;$nb_proc;$wifi;${procs# }" >> "$LOG"
