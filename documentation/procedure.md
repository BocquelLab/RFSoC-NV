# Procédure Étapes pour lancer un expérience:
1. Brancher le cable USB et Ethernet à l'arrière de la boîte dans l'ordinateur.
2. Alimenter le système en appuyant sur l'interrupteur à l'arrière droit du boîtier. Vous devriez entendre le ventilateur démarrer.
3. Démarrer la carte en appuyant sur l'interrupteur à l'avant droit de la carte. Elle prends quelques secondes à démarrer.
4. Ouvrez l'interface web de l'ADC externe. Pour ce faire, dans un terminal, lancer le script python.
```sh
cd //bob/Recherche/Bocquel/Appareils/ADC\ externe # Ou le re-télécharger avec un `git clone`
git pull # Aller chercher les dernières modifications
cd software

# Faire ceci sur un système Linux
. .venv/bin/activate
python3 app.py


# Faire cei sur un système Windows
. .venv/Scripts/activate
py app.py
```

5. Lancer un script Python qui utilise QICK. La carte devrait se trouver sur `pynq.local`, si elle n'est pas trouvable, essayez avec son ip directement.
