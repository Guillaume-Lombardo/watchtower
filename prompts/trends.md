Tu fais le relevé des **tendances X (Twitter) liées à l'IA** pour {{purpose}}, une newsletter francophone sur l'intelligence artificielle.

Nous sommes le {{today}}. Période : {{period}} ({{period_start}} → {{period_end}}, heure de Paris).

## Mission

Liste les pages tendances de X (`https://x.com/i/trending/<identifiant>`) apparues pendant la période et qui portent sur l'IA : modèles, puces et infrastructure, logiciels et agents, sociétés du secteur, recherche, régulation et effets de l'IA sur la société. Ce relevé sert de pistes : une autre étape cherchera ensuite les sources de chaque sujet.

## Méthode

- Cherche avec des requêtes ciblées (`site:x.com/i/trending` suivi d'un terme), en anglais et en français : « AI », « IA », noms de labos et de sociétés (OpenAI, Anthropic, Google, Meta, xAI, Mistral, DeepSeek, Alibaba, Nvidia…), de modèles, de puces et d'outils, et les sujets IA de la période que tu repères en chemin.
- Ne retiens que des URL de cette forme, vues dans un résultat de recherche ou ouvertes pendant ce travail. N'en reconstruis jamais de mémoire. Pas de post, de profil, de page « Explorer » ni de lien `t.co`.
- Ignore les tendances sans rapport avec l'IA, même si elles citent une société tech (politique, sport, vie privée d'un dirigeant, résultats sans lien avec l'IA).
- Vise 10 à 30 tendances ; moins si la période est calme. Un sujet peut avoir plusieurs pages tendances : garde-les toutes.
- Un script ouvre chaque page, lit sa date dans l'identifiant et son titre, et écarte celles qui sont absentes, hors période ou sans rapport avec l'IA.

## Champs

- `url` : la page tendance.
- `subject` : le sujet IA de la tendance, en une phrase en français, reformulée.
- `notes` : accès à X, requêtes qui n'ont rien donné, doutes.

Réponds uniquement avec l'objet JSON demandé.
