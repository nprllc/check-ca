# Check ça

Plateforme open source d'aide à la vérification des contenus en ligne.
On colle le lien d'un post, Check ça :

- vérifie ses **affirmations** sur le web, avec sources citées ;
- cherche les **fact-checks déjà publiés** (Google Fact Check Tools) ;
- analyse ses **images et vidéos** : métadonnées, Content Credentials (C2PA), marqueurs IPTC d'IA, probabilité IA, recherche inversée ;
- reconstitue la **chronologie** de diffusion (Wayback Machine) ;
- calcule **deux indices séparés** (solidité de l'affirmation, authenticité des images) et la confiance de l'analyse.

> Check ça est une aide à la vérification, pas un jugement définitif.

## Démarrer

```bash
pip install -r requirements.txt
cp .env.example .env        # renseigner les clés voulues, toutes optionnelles
set -a; . ./.env; set +a
uvicorn app.main:app --reload
# http://localhost:8000
```

Ou avec Docker :

```bash
docker build -t check-ca .
docker run -p 8000:8000 --env-file .env -v checkca-data:/data check-ca
```

## Briques et clés

| Brique | Service | Clé | Sans clé |
| --- | --- | --- | --- |
| Récupération des posts X | FxTwitter (public, non officiel) | — | toujours actif |
| Pages web | balises Open Graph | — | toujours actif |
| Affirmations | Mistral Conversations API + outil `web_search` (repli : Claude/Anthropic) | `MISTRAL_API_KEY` | désactivé |
| Fact-checks existants | Google Fact Check Tools | `GOOGLE_FACTCHECK_API_KEY` | désactivé |
| Recherche inversée | SerpApi (Google Lens) | `SERPAPI_API_KEY` | désactivé |
| Probabilité IA | endpoint configurable | `AI_DETECTOR_URL` | désactivé |
| EXIF, C2PA, IPTC, empreinte perceptuelle | local | — | toujours actif |
| Archives | Wayback Machine (CDX) | — | toujours actif |

**Détecteur IA** : aucun modèle n'est imposé. Les détecteurs open source sont
instables d'un jeu de données à l'autre (voir arXiv 2602.07814). Choisissez un
modèle, mesurez-le sur vos propres cas, et gardez en tête que son poids dans
l'indice est volontairement limité (−20 à +5 points).

## Méthode de scoring (v0.1.0)

Code : `app/scoring.py`. Pondérations **non calibrées** : à valider sur un jeu
de cas déjà tranchés par des fact-checkeurs avant tout lancement public.

| Indice | Règle |
| --- | --- |
| Authenticité des images | base 60 · C2PA « prise de vue » valide +25 · détecteur IA −20 à +5 · image en ligne > 7 jours avant le post −30 · marqueur de génération IA → max 10 · image déjà démentie → max 15 · le média le plus suspect détermine l'indice |
| Solidité de l'affirmation | moyenne pondérée (principale ×2) : confirmée 100, partielle 50, non vérifiable 50, contredite 0 · affirmation principale démentie par un fact-check → max 15 |
| Confiance | faible si moins de la moitié des briques ont fonctionné ou si rien n'a été réellement vérifié : les indices s'affichent alors « Indéterminé » |

Garde-fous : une affirmation n'est « confirmée » ou « contredite » que si une
source **réellement consultée** pendant la recherche est citée ; toute URL
inventée par le modèle est retirée.

## Sécurité

- Protection SSRF (`app/netguard.py`) : refus des adresses non publiques.
- Limitation de débit par IP et cache par URL normalisée (24 h).
- Aucun média stocké : seuls l'URL d'origine et les résultats sont conservés.
- Tout texte issu des posts est inséré dans la page via `textContent` (pas de HTML).

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

Toutes les API externes sont simulées : les tests tournent sans clé ni réseau.

## Ajouter une plateforme

Créer une classe `Connector` dans `app/connectors/` (TikTok, Instagram,
YouTube…) et l'enregistrer dans `app/connectors/__init__.py`, avant le
connecteur générique.

## Limites connues (v0.1)

- FxTwitter est un service tiers non officiel : prévoir la bascule vers l'API X payante.
- Vidéo : seule la vignette est analysée en profondeur ; la vidéo source est seulement inspectée pour un marqueur IA.
- SynthID : pas d'intégration automatique (pas d'API publique gratuite identifiée).
- Community Notes : non intégrées (données publiques disponibles, à brancher).
- Date de première apparition par Wayback : approximation (date d'archivage de la page, pas de l'image).

## Licence

AGPL-3.0-or-later.
