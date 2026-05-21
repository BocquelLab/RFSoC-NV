# Semaine 1 - 4 au 8 Mai
Le RFSoC 4x2 fonctionne parfaitement, tous les exemples fournis ont le comportement expectés

Je crois que le montage physique serait prêt à être testé pour le 25 mai. QICK-DAWG fournit déjà un Jupyter notebook qui fait exactement 
la même expérience que nous.
J'ai contacté Jacob Feder (celui qui a fait le PCB avec 8 entrées SMA) pour obtenir les schematics et le pinout du PCB
puisqu'ils ne sont plus trouvables en ligne. On en a besoin pour contrôler l'intensité du laser.

Faire attention, QICK dénote le générateur 0 et 1 comme DAC_B et DAC_A respectivement.
Les readouts 0 et 1 sont ADC_D et ADC_C respectivements.

Il est simple d'utiliser QICK en dehors du RFSoC 4x2. QICK fournit des notebooks pour démarrer un serveur Pyro4 qui permet d'utiliser sur un ordinateur des objets Python qui sont sur un autre ordinateurs.
Même sans ça, il est possible d'exporter une configuration et de l'utiliser hors ligne pour vérifier que certains parties d'un programme
compilent.

Il y a des bitstreams dans QICK-DAWG pour configurer le FPGA avec un mode qui compte les photons (pulses de l'APD)

Le papier qui parle de contrôle sub-nanoseconde (arXiv:2604.11743) l'a fait en utilisant une fonctionnalité de QICK.
Puisque le FPGA a une période d'horloge de 3.25ns et que le DAC supporte une période de 200ps, QICK permet d'envoyer 16 données en
un seul coup d'horloge du FPGA. Ils ont alors utilisé ça pour faire commencer leurs signaux en retard d'une fraction de la période d'horloge du FPGA.

Il me semble que la méthode décrites dans le papier va introduire des périodes d'au plus 3ns dans lesquels le signal est nul si aucune mitigation n'est prise.
À voir si c'est viable, mais on pourrait décrire l'entiereté de la séquence dans un seul pulse. Il serait ainsi beaucoup plus facile de ne pas avoir ce problème.

Je ne comprends pas pourquoi dans l'exemple RabiSweep de QICK-DAWG, ils mesurent deux fois ce que l'APD retourne dans une séquence.
Est-ce qu'ils re-mesurent toujours pour trouver le delta entre les deux mesures? Je vais explorer.

Le RFSoC 4x2, l'APD et le routeur fonctionnent tous avec 12V, on pourrait splicer la sortie de l'adapteur AC/DC fournit avec le RFSoC 4x2

L'APD est dans la bonne plage pour détecter les photons émits par la relaxation dans le centre NV. Par contre, le laser est aussi dans cette plage.

Le code généré par la librairie QICK est parfois sous-optimal, à voir si c'est dans les objectifs de stage d'implémenter des passes d'optimisation et de les pousser upstream

Je pense avoir bien compris comment le "time processor" (tProc) de QICK fonctionne. Par contre, certains aspects de la librairie Python sont encore flous.
J'ai fait quelques tests et je suis confiant pour écrire des programmes qui envoient des séquences de pulses. Je ne suis pas autant confiant pour faire plusieurs lectures.
Par contre, il est facile d'utiliser la DDR4 et de lire constamment une entrée si c'est un enjeu. Je vais regarder plus précisément comment QICK-DAWG fait.

Le mainteneur de la librairie QICK (Sho Uemura) est contactable sur le serveur Discord de la "Unitary Fondation". J'ai réussi à avoir de l'aide provenant de lui.


# Semaine 2 - 11 au 15 Mai
Les bitstreams qui sont capable de compter les photons sont sur le repo de qick-dawg, [mais les sources HDL ne sont pas encore uploadés](https://github.com/sandialabs/qick-dawg/issues/56)

Quelqu'un a fait une [pull-request](https://github.com/sandialabs/qick-dawg/pull/63) dans qick-dawg pour rajouter le timing fin avec une précision de 200ps comme dans ce [papier](https://arxiv.org/abs/2604.11743). Le décodage en timing fin et coards est fait directement dans le programme sur le tProc et référence les waveforms chargées en mémoire de manière décalée afin d'avoir une résolution de 200ps. (16 valeurs de DAC par tick du FPGA au ~3.2ns)

Le language du tProc est assez expressif pour être capable de faire des sweeps tout en ayant un petit programme.

Les readouts se font avec `set` et `seti`, même si c'est une entrée et non un canal de sorti. On contrôle avec un `QickProgram.trigger()` les ADCs, le buffer DDR4 et les sorties digitales sur le PMOD en même temps.

Quand le readout acquisitionne, il ne fait qu'écrire en mémoire et ensuite `QickProgram.acquire()` va lire à la bonne place pour remonter les données lues.

Je ne vois pas comment lire ce que l'ADC voit et faire une décision avec la valeur dans le tProc. La [documentation](https://github.com/openquantumhardware/qick/blob/main/firmware/tProcessor_64_and_Signal_Generator_V4.pdf) parle d'un "input port", mais il n'est mentionné que trois fois, mais je ne vois pas d'autre information à son sujet. Je vois que c'est mentionné quelques fois dans les fichiers system verilog, dans `qick_lib/qick/drivers/readout.py` et `qick_lib/qick/ip.py`. Ils ne sont même pas dessinés sur le diagramme du circuit du tProc.
La doc parle aussi d'un "Input channel" que le tProc pourrait lire et prendre des décisions dessus, mais il n'est mentionné qu'a `qick_lib/qick/rfboard.py` et semble référrer à un autre board.

J'ai des problèmes avec les Jupyter notebooks, je ne suis pas capable de restarter le kernel ipython, ça loop à l'inifini. Je crois que c'est VSCode le problème, puisque la version web fonctionne bien.

J'ai commencé à écrire un script de calibration en utilisant qick-dawg. C'est dur de savoir si j'ai des bugs sans avoir le hardware pour testkernel ipython, ça loop à l'inifini.

Idéalement, on va utiliser le deuxième DAC pour être capable d'échanger l'état de charge et l'état de spin nucléaire. QICK permet de faire du multiplexage en fréquence à la sortie du DAC, à voir si c'est possible et si le firmware et l'antenne vont supporter des fréquences différentes à 3 ordres de magnitudes.

Aussi, si on explore l'idée d'ioniser le centre NV lorsqu'il est dans l'état singlet, alors on va avoir besoin d'un autre laser et donc d'une autre entrée sur le PMOD. Si on fait ça, il va falloire aussi être capable de varier l'intensité du laser, ce que les PMODs ne supportent pas, il va falloire trouver un moyen d'avoir une intensité variable sans modification hardware.

Pour optimiser le pulse pi, on pourrait faire une calibration qui essaye de faire 30 pulses pi d'affilés et fait une genre de descente de gradient. Une autre idée serait d'utiliser un algorithme génétique sur toutes les `idata` du pulse. Par contre, pour que ce genre d'algorithme trouve des solutions en dehors des maximums locaux, ils doivent converger lentement. Cette calibration pourrait durer quelques minutes/heures (?). Peut-être qu'il y a une forme de pulse exotique qui est supérieure à toutes les autres.

L'algorithme [GRAPE](https://arxiv.org/abs/2307.08479) a aussi été utilisé dans certains papiers (Je ne sais plus lesquels) pour optimiser leurs pulses. Cette algorithme utilise le gradient pour faire certains calculs. Selon [certains](https://michaelgoerz.net/research/grape_june_2010_slides.pdf), la convergence n'est pas très bonne.

À chaque expériences, on peut recalibrer le pulse pi pour ne pas être sujet au bruit. Le changement de température ambiante fait bouger la table optique légèrement et affecte aussi légèrement les propriétés du centre NV.
