#!/bin/env bash
# Charger les variables d'environnement nécessaires
. /etc/profile.d/xrt_setup.sh # Source the XRT env
. /root/.bashrc
export PATH="$PATH:/usr/local/bin"
export BOARD="RFSoC4x2"
export PYRO_SERIALIZERS_ACCEPTED="pickle"
export PYRO_PICKLE_PROTOCOL_VERSION="4"

while true; do
	IP=$(hostname --all-ip-addresses | tr ' ' '\n' | sort -n | head -n 2 | tail -n 1)
	if [ "$IP" ]; then
		/usr/local/share/pynq-venv/bin/pyro4-ns  -n $IP -p 8888 &
		# Éxécuter avec l'environnement virtuel de PYNQ. Il qick et pyro4 doivent être installés sur cet environnement
		/usr/local/share/pynq-venv/bin/python3 /home/xilinx/startup.py $IP;
	
		# Tuer le processus pyro4 encore en vie
		kill -9 $(pgrep "pyro4-ns")
	fi
	sleep 5;
done
