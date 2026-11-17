Tâche planifiée quotidienne de watchtower (newsletters ai-weekly, ai-monthly, ai-yearly).

Tu es un exécutant : ne modifie ni le code, ni les prompts, ni les éditions publiées.

1. Dans ce dépôt, lance `git pull --ff-only origin main`.
2. Lance `python3 scripts/watchtower.py due` en arrière-plan (Bash avec `run_in_background`, timeout de 3 heures), puis attends sa fin avec `t3_background_wait`. Un passage complet prend de 20 à 90 minutes ; ne le lance pas deux fois en parallèle.
3. Si la sortie contient `RIEN À FAIRE`, réponds « Rien à faire aujourd'hui. » et arrête-toi.
4. Si la sortie contient `PUBLIÉ`, réponds en deux ou trois lignes : le lien GitHub de chaque fichier publié (`https://github.com/Guillaume-Lombardo/watchtower/blob/main/<chemin>`), et les remarques du journal (fournisseur absent, recherche en échec, nombre de sujets retenus).
5. Si le script échoue (`ÉCHEC` ou code de sortie non nul), relance-le une seule fois de la même façon. S'il échoue encore :
   - ouvre une issue avec `gh issue create -R Guillaume-Lombardo/watchtower --title "Échec du passage du <JJ/MM/AAAA>" --body "<message d'erreur et dernières lignes utiles du journal>"`. Le dépôt est public : retire du texte tout chemin local, nom de machine, nom d'utilisateur ou secret ;
   - réponds avec le lien de l'issue et la cause probable.
