#!/usr/bin/env bash

set -uo pipefail
trap 's=$?; echo "$0: Error on line "$LINENO": $BASH_COMMAND"; exit $s' ERR

# makerfaire2018: Paris Maker Faire 2018 card, only fits Nabaztag V1.
# (default): Ulule 2019 card, fits Nabaztag V1 and Nabaztag V2. Features a microphone. Button is on GPIO 17.
makerfaire2018=0

# ci-chroot : we're running in CI to build a release image or run tests
ci_chroot=0

# test : user wants to run tests (good idea, makes sure sounds and leds are functional)
test=0

# upgrade : this script is invoked from upgrade.sh, typically from the button in the web interface.
upgrade=0
upgrade_from=""
upgrade_drivers=""
drivers_given=0

if [ "${1:-}" == "--makerfaire2018" ]; then
  makerfaire2018=1
  shift
fi

if [ "${1:-}" == "ci-chroot" ]; then
  ci_chroot=1
elif [ "${1:-}" == "ci-chroot-test" ]; then
  ci_chroot=1
  test=1
elif [ "${1:-}" == "test" ]; then
  test=1
elif [ "${1:-}" == "--upgrade" ]; then
  upgrade=1
  # --from=COMMIT: version before the update; only what changed since is
  # redone. --drivers=LIST: drivers to update, as checked by the web site.
  for arg in "${@:2}"; do
    case "${arg}" in
      --from=*) upgrade_from="${arg#--from=}" ;;
      --drivers=*) upgrade_drivers="${arg#--drivers=}"; drivers_given=1 ;;
    esac
  done
  # auto-detect Maker Faire card here.
  if [ `sudo aplay -L | grep -c "hifiberry"` -gt 0 ]; then
    makerfaire2018=1
  fi
fi

model=$(grep "^Model" /proc/cpuinfo ; true)
if [[ ! "$model" == *"Raspberry Pi Zero"* ]]; then
  # not a Pi Zero or Zero 2
  echo "Installation only planned on Raspberry Pi Zero, will cowardly exit"
  exit 1
fi

if [ $USER == "root" ]; then
  echo "Please run this script as a regular user with sudo privileges"
  exit 1
fi

cd `dirname "$0"`
root_dir=`pwd`
owner=`stat -c '%U' ${root_dir}`
uid=`stat -c '%u' ${root_dir}`
gid=`stat -c '%g' ${root_dir}`
inst_dir=$(dirname ${root_dir})

# --- Light upgrade: redo only what the new version changes ---------------
# full=1: redo everything (installation, or previous version unknown).
full=1
changed_files=""
if [ $upgrade -eq 1 -a -n "${upgrade_from}" ]; then
  if git -C ${root_dir} cat-file -e "${upgrade_from}^{commit}" 2>/dev/null; then
    full=0
    changed_files=`git -C ${root_dir} diff --name-only "${upgrade_from}" HEAD`
  fi
fi

# changed REGEX: does the update change a file matching REGEX?
changed() {
  [ $full -eq 1 ] && return 0
  grep -qE "$1" <<< "${changed_files}"
}

# driver_wanted NAME: should this driver be updated?
driver_wanted() {
  [ ${drivers_given} -eq 0 ] && return 0
  [[ ",${upgrade_drivers}," == *",$1,"* ]]
}

# skip_step "Message" N: tell the web site that step N was not needed.
skip_step() {
  echo "$1 - $2/14 (skipped)" > /tmp/pynab.upgrade
  echo "$2" >> /tmp/pynab.upgrade.skipped
}

# Services of the update. All of them when shared code or libraries change,
# otherwise only those whose directory changed.
restart_all=1
restart_services=""
if [ $upgrade -eq 1 -a $full -eq 0 ]; then
  if ! changed '^(nabd|nabcommon)/|^requirements\.txt$|(^|/)nlu/' && [ "${drivers_given}" -eq 1 -a -z "${upgrade_drivers}" ]; then
    restart_all=0
    for service_file in ${root_dir}/*/*.service ; do
      name=`basename ${service_file}`
      dir=`basename $(dirname ${service_file})`
      # The web site is restarted at the end; nabweb-boot only runs at boot.
      case "${name}" in nabd.service|nabweb.service|nabweb-boot.service) continue ;; esac
      if changed "^${dir}/"; then
        restart_services="${restart_services} ${name}"
      fi
    done
  fi
fi

if [ $upgrade -eq 1 ]; then
  if [ $restart_all -eq 1 ]; then
    echo "Stopping services - 1/14" > /tmp/pynab.upgrade
    for service_file in ${root_dir}/*/*.service ; do
      name=`basename ${service_file}`
      if [ "${name}" != "nabd.service" -a "${name}" != "nabweb.service" ]; then
        sudo systemctl stop ${name} || true
      fi
    done
    sudo systemctl stop nabd.socket || true
    sudo systemctl stop nabd.service || true
  elif [ -n "${restart_services}" ]; then
    echo "Stopping services - 1/14" > /tmp/pynab.upgrade
    for name in ${restart_services}; do
      sudo systemctl stop ${name} || true
    done
  else
    skip_step "Stopping services" 1
  fi
fi

if [ $ci_chroot -eq 0 -a $makerfaire2018 -eq 0 -a `sudo aplay -L | grep -c "tagtagtagsound"` -eq 0 ]; then
  if [ `sudo aplay -L | grep -c "hifiberry"` -gt 0 ]; then
    echo "Judging from the sound card, this looks likes a Paris Maker Faire 2018 card."
    echo "Please double-check and restart this script with --makerfaire2018"
  else
    echo "Please install and configure sound card driver:"
    echo " https://github.com/pguyot/wm8960/tree/tagtagtag-sound"
  fi
  exit 1
fi

if [ $makerfaire2018 -eq 1 ]; then
  if [ `sudo aplay -L | grep -c "hifiberry"` -eq 0 ]; then
    echo "Please install and configure sound card driver:"
    echo " https://web.archive.org/web/20170914003528/support.hifiberry.com/hc/en-us/articles/205377651-Configuring-Linux-4-x-or-higher"
    exit 1
  fi
fi

build_and_install_driver() {
  driver=${1}
  for dir in /lib/modules/*/build
  do
    kernel=$(basename $(dirname ${dir}))
    echo "Building ${driver} driver for kernel ${kernel}"
    make KERNELRELEASE=${kernel} && sudo make install KERNELRELEASE=${kernel} && make clean KERNELRELEASE=${kernel}
  done
}

if [ $upgrade -eq 1 ] && ! driver_wanted sound_driver; then
  skip_step "Updating sound driver" 2
elif [ $upgrade -eq 1 -a $makerfaire2018 -eq 0 -a -d ${inst_dir}/wm8960 ]; then
  echo "Updating sound driver - 2/14" > /tmp/pynab.upgrade
  cd ${inst_dir}/wm8960
  sudo chown -R ${uid}:${gid} .
  pull=`git pull`
  if [ "$pull" != "Already up to date." ]; then
    build_and_install_driver wm8960
    sudo touch /tmp/pynab.upgrade.reboot
  fi
fi

if [ $upgrade -eq 1 ] && [ -d ${inst_dir}/tagtagtag-ears ] && ! driver_wanted ears_driver; then
  skip_step "Updating ears driver" 3
elif [ $upgrade -eq 1 ]; then
  echo "Updating ears driver - 3/14" > /tmp/pynab.upgrade
  if [ -d ${inst_dir}/tagtagtag-ears ]; then
    cd ${inst_dir}/tagtagtag-ears
    sudo chown -R ${uid}:${gid} .
    pull=`git pull`
    if [ "$pull" != "Already up to date." ]; then
      build_and_install_driver tagtagtag-ears
      sudo touch /tmp/pynab.upgrade.reboot
    fi
  else
    sudo mkdir -p ${inst_dir}/tagtagtag-ears
    sudo chown ${uid}:${gid} ${inst_dir}/tagtagtag-ears
    git clone https://github.com/pguyot/tagtagtag-ears ${inst_dir}/tagtagtag-ears
    cd ${inst_dir}/tagtagtag-ears
    build_and_install_driver tagtagtag-ears
    sudo touch /tmp/pynab.upgrade.reboot
  fi
else
  if [ $ci_chroot -eq 0 -a ! -e "/dev/ear0" ]; then
    echo "Please install ears driver https://github.com/pguyot/tagtagtag-ears"
    exit 1
  fi
fi

if [ $upgrade -eq 1 ] && [ -d ${inst_dir}/cr14 -a -d ${inst_dir}/st25r391x ] && ! driver_wanted rfid_driver && ! driver_wanted nfc_driver; then
  skip_step "Updating RFID drivers" 4
elif [ $upgrade -eq 1 ]; then
  echo "Updating RFID drivers - 4/14" > /tmp/pynab.upgrade
  if [ -d ${inst_dir}/cr14 ] && ! driver_wanted rfid_driver; then
    echo "RFID driver up to date"
  elif [ -d ${inst_dir}/cr14 ]; then
    cd ${inst_dir}/cr14
    sudo chown -R ${uid}:${gid} .
    pull=`git pull`
    if [ "$pull" != "Already up to date." ]; then
      build_and_install_driver cr14
      sudo touch /tmp/pynab.upgrade.reboot
    fi
  else
    sudo mkdir -p ${inst_dir}/cr14
    sudo chown ${uid}:${gid} ${inst_dir}/cr14
    git clone https://github.com/pguyot/cr14 ${inst_dir}/cr14
    cd ${inst_dir}/cr14
    build_and_install_driver cr14
    sudo touch /tmp/pynab.upgrade.reboot
  fi
  if [ -d ${inst_dir}/st25r391x ] && ! driver_wanted nfc_driver; then
    echo "NFC driver up to date"
  elif [ -d ${inst_dir}/st25r391x ]; then
    cd ${inst_dir}/st25r391x
    sudo chown -R ${uid}:${gid} .
    pull=`git pull`
    if [ "$pull" != "Already up to date." ]; then
      build_and_install_driver st25r391x
      sudo touch /tmp/pynab.upgrade.reboot
    fi
  else
    sudo mkdir -p ${inst_dir}/st25r391x
    sudo chown ${uid}:${gid} ${inst_dir}/st25r391x
    git clone https://github.com/pguyot/st25r391x ${inst_dir}/st25r391x
    cd ${inst_dir}/st25r391x
    build_and_install_driver st25r391x
    # Disable this driver as it conflicts with cr14 (nabboot will do the switch)
    sudo sed /boot/config.txt -i -e "s/^dtoverlay=st25r391x/#dtoverlay=st25r391x/"
    # Enable i2c-dev
    grep -q -E "^i2c-dev" /etc/modules || printf "i2c-dev\n" | sudo tee -a /etc/modules
    sudo touch /tmp/pynab.upgrade.reboot
  fi
else
  if [ $ci_chroot -eq 0 -a ! -e "/dev/rfid0" -a ! -e "/dev/nfc0" ]; then
    echo "If you have a TAGTAG with the original RFID card, you may want to install cr14 RFID driver https://github.com/pguyot/cr14"
    echo "If you have a 2022 NFC card, you need to install st25r391x RFID driver https://github.com/pguyot/st25r391x"
  fi
fi

if [ $upgrade -eq 1 ] && ! driver_wanted nabblockly; then
  skip_step "Updating NabBlockly" 5
elif [ $upgrade -eq 1 ]; then
  echo "Updating NabBlockly - 5/14" > /tmp/pynab.upgrade
  if [ -d ${root_dir}/nabblockly ]; then
    cd ${root_dir}/nabblockly
    sudo chown -R ${uid}:${gid} .
    pull=`git pull`
    if [ "$pull" != "Already up to date." ]; then
      ./rebar3 release
    fi
  else
    echo "You may want to install NabBlockly from https://github.com/pguyot/nabblockly"
  fi
else
  if [ $ci_chroot -eq 0 -a ! -d "${root_dir}/nabblockly" ]; then
    echo "You may want to install NabBlockly from https://github.com/pguyot/nabblockly"
  fi
fi

cd ${inst_dir}
if [ $makerfaire2018 -eq 0 ]; then
  if [ $upgrade -eq 1 ]; then
    echo "Updating ASR models - 6/14" > /tmp/pynab.upgrade
  fi

  # Maker Faire card has no mic, no need to install Kaldi
  kaldi_release="e4940d045"
  kaldi_dir="/opt/kaldi"; kaldi_pkgconfig="/usr/lib/pkgconfig/kaldi-asr.pc"
  if [[ -f "${kaldi_pkgconfig}" && "$(grep -c ${kaldi_release} ${kaldi_pkgconfig})" -eq 0 ]]; then
     # Installed Kaldi does not match needed version: remove it
     sudo rm -rf "${kaldi_dir}"
  fi
  if [ ! -d "${kaldi_dir}" ]; then
    kaldi_platform=$(. /etc/os-release && echo "$ID$VERSION_ID-`uname -m`")
    if [ "${kaldi_platform}" = "debian11-armv7l" ]; then
      # (nasty) DietPi patch: debian11 version not available for armv7l
      kaldi_platform="raspbian11-armv7l"
    fi
    # When running in 32 bits mode, maintain Pi Zero compatibility
    if [ "${kaldi_platform}" = "raspbian10-armv7l" ]; then
      kaldi_platform="raspbian10-armv6l"
    fi
    if [ "${kaldi_platform}" = "raspbian11-armv7l" ]; then
      kaldi_platform="raspbian11-armv6l"
    fi
    echo "Installing precompiled ${kaldi_platform} Kaldi into ${kaldi_dir}"
    kaldi_archive="${kaldi_release}/kaldi-${kaldi_release}-linux_${kaldi_platform}.tar.xz"
    wget -O - -q https://github.com/pguyot/kaldi/releases/download/${kaldi_archive} | sudo tar xJ -C /
    sudo ldconfig

    # Fix upgrade of py-kaldi-asr
    pushd ${root_dir}
    if [[ -f venv/lib/python3.7/site-packages/kaldiasr/nnet3.cpython-37m-arm-linux-gnueabihf.so && "$(grep -c ZN3fst8internal14DenseSymbolMapD1Ev venv/lib/python3.7/site-packages/kaldiasr/nnet3.cpython-37m-arm-linux-gnueabihf.so)" -ne 0 ]]; then
        echo "Removing incompatible py-kaldi-asr package"
        venv/bin/pip uninstall -y py-kaldi-asr
    fi
    popd
  fi

  sudo mkdir -p "${kaldi_dir}/model"

  if [ ! -d "${kaldi_dir}/model/kaldi-nabaztag-en-adapt-r20191222" ]; then
    echo "Installing Kaldi model for English"
    sudo tar xJf ${root_dir}/asr/kaldi-nabaztag-en-adapt-r20191222.tar.xz -C ${kaldi_dir}/model/
  fi

  if [ ! -d "${kaldi_dir}/model/kaldi-nabaztag-fr-adapt-r20200203" ]; then
    echo "Installing Kaldi model for French"
    sudo tar xJf ${root_dir}/asr/kaldi-nabaztag-fr-adapt-r20200203.tar.xz -C ${kaldi_dir}/model/
  fi
fi

cd ${root_dir}
if [ -x "$(command -v python3.9)" ] ; then
  py_ver=3.9
elif [ -x "$(command -v python3.7)" ] ; then
  py_ver=3.7
else
  echo "Please install Python 3.7 or 3.9 (you might need to upgrade your Linux distribution)"
  exit 1
fi
python=python${py_ver}
venv_cfg="venv/pyvenv.cfg"
if [[ -f "${venv_cfg}" && "$(grep -c version\ =\ ${py_ver} ${venv_cfg})" -eq 0 ]]; then
   # Installed virtual env does not match needed version: remove it
   sudo rm -rf "venv"
fi
venv_created=0
if [ ! -d "venv" ]; then
  echo "Creating Python ${py_ver} virtual environment"
  ${python} -m venv venv
  venv_created=1
fi

requirements_changed=0
if [ $venv_created -eq 1 ] || changed '^requirements\.txt$'; then
  requirements_changed=1
fi

if [ $requirements_changed -eq 1 ]; then
echo "Installing PyPi requirements"
if [ $upgrade -eq 1 ]; then
  echo "Updating Python requirements - 7/14" > /tmp/pynab.upgrade
fi
# Start with wheel which is required to compile some of the other requirements
venv/bin/pip install --no-cache-dir wheel
venv/bin/pip install --no-cache-dir -r requirements.txt
else
  skip_step "Updating Python requirements" 7
fi

nlu_needed=1
if [ $requirements_changed -eq 0 -a -d nabd/nlu/engine_en -a -d nabd/nlu/engine_fr ] && ! changed '(^|/)nlu/'; then
  nlu_needed=0
fi
if [ $upgrade -eq 1 -a $nlu_needed -eq 0 ]; then
  skip_step "Updating NLU models" 8
elif [ $makerfaire2018 -eq 0 ]; then
  if [ $upgrade -eq 1 ]; then
    echo "Updating NLU models - 8/14" > /tmp/pynab.upgrade
  fi

  # maker faire card has no mic, no need to install snips
  if [ ! -d "venv/lib/${python}/site-packages/snips_nlu_fr" ]; then
    echo "Downloading Snips NLU models for French"
    venv/bin/python -m snips_nlu download fr
  fi

  if [ ! -d "venv/lib/${python}/site-packages/snips_nlu_en" ]; then
    echo "Downloading Snips NLU models for English"
    venv/bin/python -m snips_nlu download en
  fi

  echo "Compiling Snips datasets"
  mkdir -p nabd/nlu
  venv/bin/python -m snips_nlu generate-dataset en */nlu/intent_en.yaml > nabd/nlu/nlu_dataset_en.json
  venv/bin/python -m snips_nlu generate-dataset fr */nlu/intent_fr.yaml > nabd/nlu/nlu_dataset_fr.json

  echo "Persisting Snips engines"
  if [ -d nabd/nlu/engine_en ]; then
    rm -rf nabd/nlu/engine_en
  fi
  venv/bin/snips-nlu train nabd/nlu/nlu_dataset_en.json nabd/nlu/engine_en
  if [ -d nabd/nlu/engine_fr ]; then
    rm -rf nabd/nlu/engine_fr
  fi
  venv/bin/snips-nlu train nabd/nlu/nlu_dataset_fr.json nabd/nlu/engine_fr
fi

trust=`sudo grep local /etc/postgresql/*/main/pg_hba.conf | grep -cE '^local +all +all +trust' || echo -n ''`
if [ $trust -ne 1 ]; then
  echo "Configuring PostgreSQL for trusted access"
  sudo sed -i.orig -E -e 's|^(local +all +all +)peer$|\1trust|' /etc/postgresql/*/main/pg_hba.conf
  trust=`sudo grep local /etc/postgresql/*/main/pg_hba.conf | grep -cE '^local +all +all +trust' || echo -n ''`
  if [ $trust -ne 1 ]; then
    echo "Failed to configure PostgreSQL"
    exit 1
  fi
  if [ $ci_chroot -eq 1 ]; then
    cluster_version=`echo /etc/postgresql/*/main/pg_hba.conf  | sed -E 's|/etc/postgresql/(.+)/(.+)/pg_hba.conf|\1|g'`
    cluster_name=`echo /etc/postgresql/*/main/pg_hba.conf  | sed -E 's|/etc/postgresql/(.+)/(.+)/pg_hba.conf|\2|g'`
    sudo -u postgres /usr/lib/postgresql/${cluster_version}/bin/pg_ctl start -D /etc/postgresql/${cluster_version}/${cluster_name}/
  else
    sudo systemctl restart postgresql
  fi
fi

sudo sed -e "s|/opt/pynab|${root_dir}|g" < nabweb/nginx-site.conf > /tmp/nginx-site.conf
if [ $upgrade -eq 0 ]; then
  if [ ! -e '/etc/nginx/sites-enabled/pynab' ]; then
    echo "Installing Nginx configuration file"
    if [ -h '/etc/nginx/sites-enabled/default' ]; then
      sudo rm /etc/nginx/sites-enabled/default
    fi
    sudo mv /tmp/nginx-site.conf /etc/nginx/sites-enabled/pynab
    if [ $ci_chroot -eq 0 ]; then
      sudo systemctl restart nginx
    fi
  else
    diff -q '/etc/nginx/sites-enabled/pynab' /tmp/nginx-site.conf >/dev/null || {
      echo "Updating Nginx configuration file"
      sudo mv /tmp/nginx-site.conf /etc/nginx/sites-enabled/pynab
      if [ $ci_chroot -eq 0 ]; then
        sudo systemctl restart nginx
      fi
    }
  fi
else
  if [ -e '/etc/nginx/sites-enabled/pynab' ] && ! diff -q '/etc/nginx/sites-enabled/pynab' /tmp/nginx-site.conf >/dev/null; then
    echo "Restarting Nginx"
    echo "Restarting Nginx - 9/14" > /tmp/pynab.upgrade
    sudo mv /tmp/nginx-site.conf /etc/nginx/sites-enabled/pynab
    sudo systemctl restart nginx
  else
    skip_step "Restarting Nginx" 9
  fi
fi
sudo rm -f /tmp/nginx-site.conf

psql -U pynab -c '' 2>/dev/null || {
  echo "Creating PostgreSQL database"
  sudo -u postgres psql -U postgres -c "CREATE USER pynab"
  sudo -u postgres psql -U postgres -c "CREATE DATABASE pynab OWNER=pynab LC_COLLATE='C' LC_CTYPE='C' ENCODING='UTF-8' TEMPLATE template0"
  sudo -u postgres psql -U postgres -c "ALTER ROLE pynab CREATEDB"
}

if [ $upgrade -eq 1 ] && ! changed '/migrations/'; then
  skip_step "Updating data models" 10
else
  echo "Updating data models"
  if [ $upgrade -eq 1 ]; then
    echo "Updating data models - 10/14" > /tmp/pynab.upgrade
  fi
  venv/bin/python manage.py migrate
fi

all_locales="-l fr_FR -l de_DE -l en_US -l en_GB -l it_IT -l es_ES -l ja_jp -l pt_BR -l de -l en -l es -l fr -l it -l ja -l pt"

echo "Updating localization messages"
if [ $upgrade -eq 0 ]; then
  venv/bin/django-admin compilemessages ${all_locales}
elif [ -x "$(command -v msgfmt)" ]; then
  compiled=0
  for po in nab*/locale/*/LC_MESSAGES/*.po; do
    [ -e "${po}" ] || continue
    mo="${po%.po}.mo"
    if [ ! -e "${mo}" -o "${po}" -nt "${mo}" ]; then
      if [ $compiled -eq 0 ]; then
        echo "Updating localization messages - 11/14" > /tmp/pynab.upgrade
      fi
      msgfmt -o "${mo}" "${po}" || echo "Could not compile ${po}"
      compiled=$((compiled + 1))
    fi
  done
  if [ $compiled -eq 0 ]; then
    skip_step "Updating localization messages" 11
  fi
else
  echo "Updating localization messages - 11/14" > /tmp/pynab.upgrade
  for module in nab*/locale; do
    (
      cd `dirname ${module}`
      ../venv/bin/django-admin compilemessages ${all_locales}
    )
  done
fi

if [ $test -eq 1 ]; then
  echo "Running tests"
  if [ $ci_chroot -eq 1 ]; then
      sudo CI=1 venv/bin/pytest
  else
      sudo venv/bin/pytest
  fi
fi

if [ $ci_chroot -eq 1 ]; then
  sudo -u postgres /usr/lib/postgresql/${cluster_version}/bin/pg_ctl stop -D /etc/postgresql/${cluster_version}/${cluster_name}/
fi

# copy service files
echo "Installing service files"
if [ $upgrade -eq 1 ]; then
  echo "Installing service files - 12/14" > /tmp/pynab.upgrade
fi
services_installed=0
for service_file in nabd/nabd.socket */*.service ; do
  name=`basename ${service_file}`
  sudo sed -e "s|/opt/pynab|${root_dir}|g" -e "s|/home/pi/pynab|${root_dir}|g" < ${service_file} > /tmp/${name}
  if cmp -s /tmp/${name} /lib/systemd/system/${name} && systemctl is-enabled --quiet ${name} 2>/dev/null; then
    rm -f /tmp/${name}
    continue
  fi
  sudo mv /tmp/${name} /lib/systemd/system/${name}
  sudo chown root /lib/systemd/system/${name}
  sudo systemctl enable ${name}
  services_installed=$((services_installed + 1))
  # A new or changed service is (re)started with the others.
  dir=`dirname ${service_file}`
  if [ "${name}" != "nabd.service" -a "${name}" != "nabweb.service" -a "${name}" != "nabd.socket" ]; then
    case " ${restart_services} " in *" ${name} "*) ;; *) restart_services="${restart_services} ${name}" ;; esac
  fi
done
if [ $services_installed -gt 0 ]; then
  sudo systemctl daemon-reload
elif [ $upgrade -eq 1 ]; then
  skip_step "Installing service files" 12
fi
sudo sed -e "s|/opt/pynab|${root_dir}|g" < nabboot/nabboot.py > /tmp/nabboot.py
sudo mv /tmp/nabboot.py /lib/systemd/system-shutdown/nabboot.py
sudo chown root /lib/systemd/system-shutdown/nabboot.py
sudo chmod +x /lib/systemd/system-shutdown/nabboot.py

# setup Pynab logs rotation
echo "Setting up Pynab logs rotation"
cat > '/tmp/pynab' <<- END
/var/log/nab*.log {
  weekly
  rotate 4
  missingok
  notifempty
  copytruncate
  delaycompress
  compress
}
END
sudo mv /tmp/pynab /etc/logrotate.d/pynab
sudo chown root:root /etc/logrotate.d/pynab

# advertise rabbit on local network
if [ ! -f "/etc/avahi/services/pynab.service" ]; then
  echo "Setting up Avahi service for Pynab"
  cat > '/tmp/pynab.service' <<- END
<?xml version="1.0" standalone='no'?><!--*-nxml-*-->
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- See avahi.service(5) for more information about this configuration file -->
<service-group>
  <name replace-wildcards="yes">Nabaztag rabbit (%h)</name>
  <service>
    <type>_http._tcp</type>
    <port>80</port>
    <txt-record>vendor=violet</txt-record>
    <txt-record>model=tag:tag:tag</txt-record>
  </service>
</service-group>
END
  sudo mv /tmp/pynab.service /etc/avahi/services/pynab.service
fi
if [ ! -f "/etc/avahi/services/nabblocky.service" ]; then
  echo "Setting up Avahi service for NabBlockly"
  cat > '/tmp/nabblocky.service' <<- END
<?xml version="1.0" standalone='no'?><!--*-nxml-*-->
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- See avahi.service(5) for more information about this configuration file -->
<service-group>
  <name replace-wildcards="yes">NabBlockly (%h)</name>
  <service>
    <type>_http._tcp</type>
    <port>8080</port>
    <txt-record>vendor=Paul Guyot</txt-record>
    <txt-record>model=tag:tag:tag</txt-record>
  </service>
</service-group>
END
  sudo mv /tmp/nabblocky.service /etc/avahi/services/nabblocky.service
fi

if [ -e /tmp/pynab.upgrade.reboot ]; then
  echo "Rebooting..."
  echo "Upgrade requires reboot, rebooting now - 14/14" > /tmp/pynab.upgrade
  sudo rm -f /tmp/pynab.upgrade
  sudo rm -f /tmp/pynab.upgrade.reboot
  sudo reboot
else
  if [ $ci_chroot -eq 0 ]; then
    echo "Starting services"
    if [ $upgrade -eq 1 ]; then
      echo "Restarting services - 13/14" > /tmp/pynab.upgrade
    fi
    sudo systemctl restart logrotate.service || true
    if [ $restart_all -eq 1 ]; then
      sudo systemctl start nabd.socket
      sudo systemctl start nabd.service

      # start services
      for service_file in */*.service ; do
        name=`basename ${service_file}`
        if [ "${name}" != "nabd.service" -a "${name}" != "nabweb.service" ]; then
          sudo systemctl start ${name}
        fi
      done
    elif [ -n "${restart_services}" ]; then
      # Only the services of the changed parts (the others kept running).
      for name in ${restart_services}; do
        sudo systemctl restart ${name}
      done
    else
      skip_step "Restarting services" 13
    fi

    if [ $upgrade -eq 1 ]; then
      if [ $restart_all -eq 1 -o -n "${changed_files}" ]; then
        echo "Restarting web site - 14/14" > /tmp/pynab.upgrade
        sudo systemctl restart nabweb.service
      else
        skip_step "Restarting web site" 14
      fi
    else
      sudo systemctl start nabweb.service
    fi
  fi
fi
