# Procédure
Étapes pour lancer un expérience:

1. Brancher le cable USB et Ethernet à l'arrière de la boîte dans l'ordinateur.
2. Alimenter le système en appuyant sur l'interrupteur à l'arrière droit du boîtier. Vous devriez entendre le ventilateur démarrer.
3. Démarrer la carte en appuyant sur l'interrupteur à l'avant droit de la carte. Elle prends quelques secondes à démarrer.
4. Ouvrez l'interface web de l'ADC externe. Pour ce faire, dans un terminal, lancer le script python.
```sh
cd C:/Codes/USBADC/software
. .venv/bin/activate # Linux
. .venv/Scripts/activate # Windows avec git bash
.venv/Scripts/activate # Windows avec cmd ou powersheel
python app.py
```

5. Lancer un script Python qui utilise QICK. La carte devrait se trouver sur `pynq.local`, si elle n'est pas trouvable, essayez avec son ip directement.
