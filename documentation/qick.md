# QICK et QICK-DAWG
[Quantum Instrumentation Control Kit](https://github.com/openquantumhardware/qick) est une librairie Python qui permet de préparer des séquences de pulses, les envoyer et en recevoir sur plusieurs cartes, dont celle que nous utilisons: la [RFSoC4x2](https://www.realdigital.org/hardware/rfsoc-4x2).

[QICK-DAWG](https://github.com/sandialabs/qick-dawg) est une librairie qui utilise QICK et fournit des programmes utiles pour faire des expériences avec des défauts comme le centre NV dans un diamant.

J'ai cloné ces répertoires dans l'organisation BocquelLab pour ne pas perdre ces projets si ils venaient à être supprimés pour une quelconque raison.

La carte que nous utilisons possède 4 entrées appellées ADC (Analog-Digital converter) et 2 sorties appellées DAC (Digital-Analog converter). Sur la carte, elles sont appellées ADC_A, ADC_B, ADC_C, ADC_D, mais dans la librairie QICK, elles sont numérotées dans l'ordre inverse. Donc Channel 0 = ADC_D, Channel 1 = ADC_C, .... Idem Pour les sorties, Channel 0 = DAC_B, Channel 1 = DAC_A.


# Firmware FPGA
Un FPGA est une composante électronique qui peut être reconfiguré pour devenir n'importe quel [circuit logique](https://en.wikipedia.org/wiki/Logic_gate). 

Le projet incorpore des bitstreams précompilés qui seront envoyés sur la carte lorsqu'un objet `QickSoc` est créé. Les fichiers sources VHDL, Verilog et SystemVerilog se trouvent dans le dossier `firmware` du projet. Ces languages permettre de décrire un circuit logique et ensuite les compiler avec des programmes intégrés dans des outils tel que [Vivado](https://www.amd.com/en/products/software/adaptive-socs-and-fpgas/vivado.html). Les programmes compilés sont couramment appellés "bitstreams".

Le bitstream utilisé pour notre application se trouve dans un autre projet, [qick-dawg](https://github.com/sandialabs/qick-dawg) qui ont modifiées le firmware pour ajouter un compteur de pulses sur les entrées. Ainsi, on peut compter les pulses venant des APDs et savoir le nombre de photons reçus dans une certaine période de temps. Le firmware exact que nous utilisons est [celui-ci](https://github.com/BocquelLab/qick-dawg/raw/refs/heads/main/firmware/photon_counting/qick_4x2.bit).

Le mainteneur de la librairie (Sho Uemura) est contactable sur le canal `#qick` sur [le serveur discord](http://discord.unitary.foundation/) de la [Unitary Foundation](https://unitary.foundation/).


# tProc
Le projet QICK utilise le FPGA pour y construire [un processeur fait sur mesure](https://github.com/BocquelLab/qick/blob/main/firmware/tProcessor_64_and_Signal_Generator_V4.pdf) appellé tProc pour l'envoie et la réception de pulses.

Le jeu d'instruction de ce processeur est différent de celui trouvé dans les architectures d'ordinateurs modernes. Il contient les instructions usuelles pour faire de l'arithmétique, des branchements et manipuler la pile. Il contient aussi des instructions pour manipuler l'horloge interne, séquencer des pulses, jouer des pulses, stopper le processeur jusqu'à un certain temps et contient aussi plusieurs pages de registres. La documentation couvre une bonne partie du fonctionnement du jeu d'instruction, mais manque d'information sur comment les entrées fonctionnent.

De manière générale, il n'est pas nécessaire d'écrire directement en language d'assemblage car les abstractions écrites en Python permettent de construire un programme dans ce language.

Une deuxième version améliorée de ce processeur (tProcV2) est en cours de création depuis environ 2 ans.


# Utilisation de la librairie QICK
```python
from qick import *

```
Il existe une [documentation en ligne de la librairie QICK](https://docs.qick.dev/latest/).

Le répertoire [qick_demos](https://github.com/BocquelLab/qick/tree/main/qick_demos) contient des examples d'utilisation dans des notebooks Jupyter. Ces exemples montrent comment créer un programme, comment l'éxécuter et comment récupérer et afficher les résultats.

Il y a des exemples supplémentaires [ici](https://github.com/meeg/qick_demos_sho).


# Utilisation de la librairie QICK-DAWG
```python
import qickdawg as qd

# Connection avec la carte, on peut aussi entrer son IP directment
qd.start_client("pynq.local")

# On crée une configuration par défaut à partir de laquelle on va
# créer les configurations pour toutes les expériences.
default_config = qd.NVConfiguration()

default_config.adc_channel = 0 # Channel 0 / ADC_D
default_config.edge_counting = True # On veut compter le nombre de photons qui arrivent dans une certaine fenêtre

# https://en.wikipedia.org/wiki/Hysteresis pour le comptage de photon selon l'intensité du signal reçu.
default_config.high_threshold = 2000 # Intensité minimale requise sur l'entrée de l'ADC pour considérer qu'un photon est arrivé
default_config.low_threshold = 500 # Intensité maximale requise pour considérer que le pulse créé par le photon est terminé

default_config.mw_channel = 0 # Channel 0 / DAC_B
default_config.mw_nqz = 1 # Première zone de Nyquist https://en.wikipedia.org/wiki/Nyquist_frequency
default_config.mw_gain = 5000 # Gain appliqué au générateur d'onde

default_config.laser_gate_pmod = 0 # PMOD 0 sur la carte

default_config.relax_delay_tns = 50 # Temps entre les expériences en nano-secondes
# On aurait pu écrire ceci à la place de la ligne au dessus car qick-dawg permet de définir les unités
# avec le nom du champs qui est accédé en redéfinissant la fonction magique `__setattr__`:
# https://github.com/BocquelLab/qick-dawg/blob/8b8cbe77ce6aca13c1169f00eb48509972613379/src/qickdawg/nvpulsing/nvconfiguration.py#L69
# On peut utiliser `tns` pour des nano-secondes, `tus` pour des micro-secondes, `treg` pour un temps en nombre de cycles sur le tProc.
default_config.relax_delay_tus = 0.05 # Temps entre les expériences en micro-secondes
```
On peut ensuite charger une des expériences fournies par `qick-dawg` ou en créer une.
Il existe une [documentation en ligne de la librairie QICK-DAWG](https://qick-dawg.readthedocs.io/en/latest/index.html).


# Contrôle sub-nanoseconds
Le papier [Sub-nanosecond control for spin-defect quantum memories with a low-cost, compact FPGA platform](https://arxiv.org/pdf/2604.11743) indique qu'il est possible de faire jouer des pulses avec une résolution plus petite que celle de l'horloge sur le FPGA.

Cela est fait en exploitait le fait que lorsque le tProc cédule des pulses, il le fait par groupes de 16 et les envoies tous sur un canal pendant un cycle d'horloge. En construisant des enveloppes d'ondes dont le début n'est pas aligné avec ceux des cycles d'horloges, ils peuvent contrôller où un pulse commence avec une résolution 16 fois meilleures.

[Quelqu'un a déjà implémenté ceci et essaie d'ajouter cette fonctionnalité à qick-dawg](https://github.com/sandialabs/qick-dawg/pull/67).


# Visualisation des séquences de pulses
Il n'y a présentemment pas de moyen de visualiser les séquences de pulses créées, autre qu'en les regardant avec un oscilloscope.

Il a quelqu'un qui a fait une librairie qui prends en entrée un programme QICK et une configuration et sort la séquence de pulse et [qui essaie d'ajouter cette foncionnalité à QICK](https://github.com/openquantumhardware/qick/pull/426). Cette librairie ne fonctionne pas pour tous les programmes qui peuvent être roulé sur le tProc, mais fonctionne pour les programmes générées avec l'abstraction Python de QICK.
