# watchtower

Veille sur l'intelligence artificielle, en français : IA générative surtout, mais aussi modèles,
hardware, logiciel, sociétés, recherche et régulation.

> **Les textes de ce dépôt sont générés automatiquement par IA**, sans relecture humaine
> systématique. Chaque information renvoie à sa source : vérifiez-la avant de la reprendre.
> Une erreur ? [Ouvrez une issue](https://github.com/Guillaume-Lombardo/watchtower/issues).

## Les éditions

| Newsletter | Dossier | Parution | Contenu |
|---|---|---|---|
| **ai-weekly** | [`weekly/`](weekly/) | chaque lundi vers 7 h (heure de Paris) | l'actualité de la semaine précédente, du lundi au dimanche |
| **ai-monthly** | [`monthly/`](monthly/) | le 1er de chaque mois | synthèse du mois précédent : tendances, chiffres, ce qui monte et ce qui retombe |
| **ai-yearly** | [`yearly/`](yearly/) | vers le 20 décembre | rétrospective de l'année et analyse d'impact : ce qui semblait important sans l'être, ce qui semblait annexe et a compté |

Les fichiers sont nommés `AAAA-MM-JJ_<newsletter>.md`, d'après leur date de parution. Première
édition : ai-weekly du 23 novembre 2026.

## Méthode

0. **Relevé des tendances X.** Les deux assistants listent les pages tendances X (Twitter) de la
   période liées à l'IA. Le script les relit (existence, date, titre) et ne garde que celles qui
   parlent d'IA. Elles servent de pistes communes aux deux recherches.
1. **Deux recherches indépendantes.** Deux assistants (Codex et Claude) reçoivent le même prompt et
   cherchent chacun de leur côté sur le web. Un sujet trouvé par les deux est confirmé ; un sujet
   trouvé par un seul est vérifié avec plus de prudence.
   Les recherches regardent aussi X (Twitter) pour repérer les sujets qui font parler : chaque
   sujet porte une estimation de sa discussion sur X (`x_buzz`), et l'hebdo a une section « Ce qui
   fait parler sur X ». C'est un avis des assistants sur ce qu'ils ont vu, pas une mesure : le
   pipeline n'a pas accès à l'API de X. Les tendances qu'aucun sujet sourcé ne couvre sont
   publiées dans `raw/` et comptées dans la section « Méthode ».
2. **Vérification des liens par script, sans IA.** Chaque lien est ouvert : les pages absentes et
   celles datées hors de la période sont écartées. Un lien X doit être un post ou une page
   tendance, dont la date est lue dans l'identifiant ; une page tendance n'est gardée que si son
   titre ou son résumé parle d'IA (liste de termes dans le script). Les sites qui bloquent les
   robots sont gardés mais signalés comme « non vérifiables ».
3. **Synthèse.** Un assistant rédige l'édition à partir des seuls sujets retenus. Le script refuse
   toute édition qui cite un lien absent des sujets vérifiés.
4. **Traçabilité.** Les réponses brutes des deux recherches, les sujets retenus et les sujets
   écartés (avec la raison) sont publiés dans [`raw/`](raw/). Chaque édition se termine par une
   section « Méthode » qui donne ces chiffres et la version des prompts utilisée.

L'édition mensuelle part des éditions hebdomadaires du mois, complétées par une recherche sur les
jours qu'aucune ne couvre. La rétrospective part des archives de l'année et d'une recherche
d'impact dédiée, qui confronte pour chaque sujet la perception de l'époque (source d'alors, et
tendances X archivées dans `raw/`) et
l'impact constaté (source récente).

Les limites sont celles de l'outil : une IA peut mal résumer une source, rater un sujet ou en
surestimer un autre. La vérification automatique garantit que les liens existent et sont datés de
la période, pas que chaque résumé est exact.

## Contenu du dépôt

| Chemin | Rôle |
|---|---|
| [`prompts/`](prompts/) | prompts de recherche et de synthèse, versionnés |
| [`schemas/`](schemas/) | format JSON imposé aux recherches |
| [`scripts/watchtower.py`](scripts/watchtower.py) | le pipeline : recherche, vérification, synthèse, publication |
| [`raw/`](raw/) | sorties brutes et vérifications de chaque passage |
| [`tests/`](tests/) | tests du pipeline (`python3 -m unittest discover -s tests`) |

Un passage quotidien à 4 h 30 (heure de Paris) lance `python3 scripts/watchtower.py due`, qui
produit ce qui est attendu ce jour-là et rattrape un passage manqué. On peut aussi lancer une
édition à la main :

```sh
python3 scripts/watchtower.py due --dry-run          # ce qui serait produit aujourd'hui
python3 scripts/watchtower.py weekly --no-push       # l'hebdo de la semaine, écrit sans commit ni push
python3 scripts/watchtower.py monthly --date 2026-12-01 --providers claude
```

Prérequis : Python 3.11 ou plus récent (bibliothèque standard seulement), les CLI `codex` et
`claude` authentifiées, `git` avec droit de push.

## Licence

- Les textes (éditions, prompts) sont sous licence
  [Creative Commons Attribution 4.0 International](LICENSE) (CC BY 4.0).
- Le code (`scripts/`, `tests/`, `schemas/`) est sous [licence MIT](LICENSE-CODE).
- Les articles et pages cités restent la propriété de leurs auteurs et éditeurs : les éditions n'en
  donnent que des résumés reformulés et des liens.
