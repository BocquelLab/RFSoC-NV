# USBADC
Il s'agit d'un ADC externe qui mesure en continue la puissance perçue par le powermeter, la température du powermeter et une tension supplémentaire sur la pin PA3 (par défaut c'est le tachomètre intégré du ventilateur.)

Il y a 4 LEDs qui indictent l'état des 4 tensions présentent sur le board (28 volts, 12 volts, 5 volts, 3.3 volts).

Le board génère aussi 28 volts pour alimenter un l'amplificateur RF et supporte jusqu'à 1 ampère en sortie. Il est surement possible de modifier les valeurs de certaines composantes pour créer une tension différente au besoin en se fiant à la fiche technique du convertisseur de tension.

Il possède aussi 10 sorties digitales programmables. Les pins PA5, PA6, PA7 et PA8 sont des sorties 3.3 volts. Les pins PB7, PB6, PB5, PB4, PB3, PB1 sont des pins "open-drain", ce qui veut dire que lorsqu'elles sont à l'état logique bas, c'est comme si elle n'était branchées à rien, quand elles sont à l'état logique haut, elles agissent comme un ground et peuvent ingérer [jusqu'à 320 milli-ampères](https://www.diodes.com/assets/Datasheets/DMN62D0UT.pdf). Cela peut-être utile pour controller un relai par exemple.

Le port USB permet de communiquer avec le micro-contrôlleur STM32 sur le PCB. Un protocole permet d'envoyer des commandes et de lire des tensions.

Pour plus d'informations, voir [le répertoire USBADC sur github](https://github.com/BocquelLab/USBADC).
