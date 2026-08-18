# RFSoC4x2
La carte RFSoC4x2 de chez [Real Digital](https://www.realdigital.org/) contient une puce [ZYNQ Ultrascale+ RFSoC ZU48DR](https://www.amd.com/en/products/adaptive-socs-and-fpgas/soc/zynq-ultrascale-plus-rfsoc.html) de chez AMD (Xilinx a été acquis par AMD en 2020 et a développé la technologie FPGA).


# Entrées / Sorties
Cette puce contient un processeur et un FPGA. Elle permet d'envoyer et recevoir des signaux qui vont jusqu'à 5GHz. Elle est souvent utilisée dans des applications nécessitant de la communication par ondes radio sur une large bande. Dans notre cas, on l'utilise pour faire évoluer l'état de spin d'un [centre NV dans un diamant](https://en.wikipedia.org/wiki/Nitrogen-vacancy_center).

Le processeur et le FPGA ont chacuns 4Gb de RAM. Pour se partager de l'information, ils utilisent un bus à l'intérieur du chip.

La carte contient deux sorties DAC (Digital-Analog converter) et quatre entrées ADC (Analog-Digital converter) hautes fréquences.

Dans notre montage, on utilise une sortie pour faire évoluer l'état de spin et une entrée pour collecter les pulses envoyés par les APDs lorsqu'un photon est détecté.

La carte est alimentée en 12 volts.

Elle possède aussi des sorties digitales "PMOD" qui sont controllées par le FPGA. On les utilise pour controller la gate du laser et ainsi l'activer/désactiver.

Un petit écran affiche la version du système d'opération Linux [PYNQ](https://github.com/Xilinx/Pynq) ainsi que l'adresse IP. L'écran affiche un addresse IP juste si la connection USB est utilisée et qu'elle était branchée au démarage.


# Communication
On peut communiquer avec elle par sériel ou par réseau. Le port Ethernet et le port USB permettent de communiquer par réseau. Lorsqu'elle est branchée au réseau, on peut lui envoyer des commandes pour qu'elle éxécute les programmes QICK qu'on lui envoie et qu'elle nous retourne les données mesurées.

Lorsque branchée en Ethernet, elle apparait sur le "Dual DNS Server" installé sur l'ordinateur du labo et qui contient une interface web accessible dans les favoris de Firefox.

On peut accéder à un shell sur la carte par SSH: `ssh xilinx@pynq.local # mot de passe: xilinx`.

Il y a aussin un daemon Jupyter qui tourne et qui rend un notebook Jupyter sur la carte accessible. Il suffit d'aller sur `http://pynq.local/` dans un navigateur.
