Télécharger toutes les memories Snap (photos et vidéos) à partir du fichier `memories_history.html` du fichier zip `mydata~...zip` fourni par Snapchat après leur avoir demandé vos photos et vidéos. Il ne faut pas que votre demande à Snapchat excède 7 jours (date d'expiration des données).

1. Cliquer droit sur le fichier "memories_history.html" et copier le chemin vers le fichier, et mettre ce chemin dans le fichier `params.json` au niveau de la variable `memories_history_path`.
2. Indiquer le chemin du dossier dans lesquels les memories doivent arriver et l'écrire au niveau de la variable `output_directory`.
3. Exécuter le fichier exe.

La variable `starting_index` dans le fichier `params.json`. Il est utile quand vous avez déjà téléchargé une partie des memories dans l'ordre, et que ne vous ne voulez pas tout re-télécharger depuis le début. Si `starting_index` = 1, le téléchargement commencera au 1er fichier.

Certains fichiers peuvent ne pas avoir été téléchargés à cause d'une erreur côté serveur Snap (fichier indisponible), les fichiers correspondant (indice du fichier, type et date) seront dans le fichier `not_saved.txt`