# Scripts à éxécuter automatiquement au démarrage de la carte

Le script `startup.sh` permet de démarrer le serveur pyro4 et les objets Qick nécessaires en arrière plan.

Pour installer, les scripts, il faut mettre `startup.sh` et `startup.py` dans `/home/xilinx` (Utilisateur par défaut). Cela peut être fait avec SFTP.
```bash
cd endroit_sur_lordinateur_où_sont_startup.sh_et_startup.py
sftp xilinx@pynq.local
put startup.sh
put startup.py
```

Ensuite, il faut préparer une cron job qui va être éxécuter par l'utilisateur `root` à chaque démarrages.
```bash
ssh xilinx@pynq.local
sudo su # Mot de passe "xilinx"
crontab -e # La première fois que cette commande est éxécuter sur une machine, il faut choisir un éditeur de texte. Prendre "nano".
# Écrire sur la dernière ligne: 
# @reboot sleep 20 && /home/xilinx/startup.sh > /home/xilinx/startup.log 2>&1
# Pour quitter et sauvegarder avec nano: ctrl + x, Y, enter
# Le script devrait maintenant être éxécuté automatiquement au démarrage.
```

La sortie du script va être écrite dans `/home/xilinx/startup.log` ce qui permet de débugguer si il y a un problème pour une quelconque raison.
Qick et Pyro4 doivent être installés sur la carte. L'installation doit être fait à partir d'un notebook pour qu'elle soit fait dans le bon environnement virtuel.
