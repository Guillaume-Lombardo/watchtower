# Instructions pour les agents

Dépôt public : chaque commit est visible de tous.

## Publication

- **Exception documentée à la règle « pas de push sur `main` »** : le passage planifié
  (`python3 scripts/watchtower.py due`, voir `prompts/scheduled-task.md`) commite et pousse
  directement sur `main` les éditions qu'il génère (`weekly/`, `monthly/`, `yearly/`) et leurs
  données (`raw/`). Message de commit : `docs(<type>): <newsletter> <AAAA-MM-JJ>`.
- Toute autre modification (code, prompts, schémas, README) passe par une branche
  (`feat/`, `fix/`, `chore/`, `docs/`) et une PR.
- Une édition publiée ne se réécrit pas. Pour corriger une erreur, ajouter à la fin une section
  `## Correction du JJ/MM/AAAA` qui dit ce qui était faux et ce qui est juste, avec source.
- Ne jamais commiter de secret, de chemin local, de nom de machine ou de journal (`runs/` est ignoré).

## Contenu

- Les éditions sont en français et annoncent qu'elles sont générées par IA (en-tête ajouté par le
  script) : ne pas retirer cette mention.
- Chaque information cite une source issue des recherches vérifiées ; le script refuse les autres
  liens. Résumés reformulés, pas de reprise de texte, pas de citation longue.
- Les changements de prompts sont des changements éditoriaux : décrire dans la PR l'effet attendu.

## Vérifications

- `python3 -m unittest discover -s tests` avant toute PR.
- `python3 scripts/watchtower.py due --dry-run --today AAAA-MM-JJ` pour voir ce qui serait produit
  un jour donné.
- Tester un changement de prompt avec `--no-push` : les fichiers sont écrits sans commit ni push ;
  les supprimer ensuite (`git clean`/`git checkout`) avant de pousser la branche.
