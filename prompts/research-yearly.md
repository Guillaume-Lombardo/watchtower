Tu prépares la rétrospective {{year}} de **ai-weekly**, newsletter francophone sur l'intelligence artificielle (modèles, hardware, logiciel, sociétés, recherche, régulation). Nous sommes le {{today}}.

## Mission

Le but est de comparer, pour les sujets de l'année, **ce qu'ils semblaient être au moment où ils sont sortis** et **l'impact qu'ils ont réellement eu** à ce jour. On cherche en particulier :

- ce qui a fait beaucoup de bruit mais a peu changé les choses (`surestime`) ;
- ce qui est passé relativement inaperçu ou a semblé annexe, mais a eu un effet important (`sous-estime`).

1. Dresse la liste de 18 à 25 sujets de l'année ({{year}}-01-01 → {{year}}-12-19) : les sujets qui ont paru majeurs sur le moment, **et** des sujets d'apparence secondaire dont tu soupçonnes un effet important. Au moins un tiers des sujets doivent venir de la deuxième catégorie. Pars des archives ci-dessous, mais elles sont incomplètes (la newsletter a démarré le {{start_note}}) : complète par ta propre recherche pour couvrir toute l'année.
2. Pour chaque sujet, établis :
   - **la perception initiale** : couverture, attentes, promesses au moment de l'annonce, avec au moins une source de l'époque (`role` = `perception`). Les tendances X archivées (fin de message) donnent une trace datée de ce qui faisait parler : un sujet qui y figure a fait du bruit sur le moment, ce qui doit se refléter dans `hype`. Un sujet absent n'a pas forcément été ignoré : le relevé a démarré le {{start_note}} et ne couvre que X. Une page tendance peut servir de preuve `perception`, jamais `impact` ;
   - **l'impact constaté** avec des éléments vérifiables et récents (`role` = `impact`) : adoption (utilisateurs, téléchargements, parts de marché, usage par les développeurs et les entreprises), effets économiques (prix, investissements, cours, chiffre d'affaires), suites (concurrents qui ont suivi, régulations, procès), ou au contraire abandon, retard, recul.
3. Donne un verdict : `surestime`, `sous-estime`, `conforme`, ou `trop-tot` s'il est encore impossible de juger. Les notes `hype` et `impact` (1 à 5) doivent être cohérentes avec le verdict.

## Règles

- Chaque URL doit être une page que tu as effectivement vue pendant cette recherche ; n'en reconstruis jamais de mémoire. Un script vérifiera chaque lien et écartera les sujets sans preuve valide.
- N'invente aucun chiffre. Si l'impact n'est pas mesurable, dis-le et choisis `trop-tot`.
- Préfère les sources primaires (annonces, rapports financiers, statistiques officielles, études) et la presse sérieuse.
- Si une rétrospective de l'année précédente est fournie, ajoute 2 à 4 sujets qui la réexaminent avec un an de recul (ses « surestimés » et « sous-estimés » se sont-ils confirmés ?).
- Textes en français, reformulés ; aucune citation de plus de quelques mots.

Réponds uniquement avec l'objet JSON demandé.

## Rétrospective de l'année précédente

{{previous}}

## Archives de la newsletter pour {{year}}

{{archive}}

## Tendances X archivées pour {{year}}

Relevées et vérifiées à chaque passage (date, titre, page) :

{{trends}}
