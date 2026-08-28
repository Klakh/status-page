# status-page

Page de statut auto-hébergée pour [status.keeklah.fr](https://status.keeklah.fr),
alimentée par un Raspberry Pi 1 B+ sous DietPi et publiée via GitHub Pages.

## Principe

Les données et la présentation sont séparées :

| Fichier      | Rôle                                        | Change à chaque run |
|--------------|---------------------------------------------|---------------------|
| `monitor.py` | Sonde les services, écrit `data.json`       | non                 |
| `data.json`  | État courant + historique agrégé            | oui                 |
| `index.html` | Page statique, recharge `data.json` seule   | non                 |

Le navigateur repolle `data.json` toutes les 60 s (requête conditionnelle : un
`304 Not Modified` la plupart du temps) et redessine les barres sans
rechargement. Une seule mise à jour de `index.html` est nécessaire quand on
touche à l'interface, plus jamais pour les données.

Le polling s'arrête quand l'onglet passe en arrière-plan et reprend au retour.

## Historique multi-résolution

Chaque vérification incrémente simultanément trois compteurs, ce qui évite
toute ré-agrégation ultérieure :

| Pas    | Rétention | Points max |
|--------|-----------|------------|
| 5 min  | 48 h      | 576        |
| 1 h    | 30 j      | 720        |
| 1 j    | 180 j     | 180        |

Le client choisit la série adaptée à la période affichée : la couverture prime
sur la finesse, pour ne jamais laisser de trou dans le graphe. `data.json`
plafonne ainsi à ~30 Ko par service (~4 Ko une fois gzippé par GitHub Pages),
quelle que soit l'ancienneté du dépôt.

## Installation

```bash
git clone git@github.com:Klakh/status-page.git
cd status-page
cp config.json.example config.json
$EDITOR config.json
python3 monitor.py          # premier passage
```

`config.json` et `state.json` sont ignorés par Git : la configuration reste
locale au Pi, et l'état complet n'est jamais publié.

Sans `config.json`, `monitor.py` s'arrête en erreur sans rien sonder ni
publier. C'est délibéré : sur un clone neuf, où le fichier manque par
construction, un repli sur la configuration d'exemple reviendrait à publier un
historique vide par-dessus le vrai.

`state.json` étant ignoré, un clone neuf n'en a pas — mais `data.json`, lui,
est versionné et contient tout ce que l'état doit retenir. `monitor.py` le
reconstruit donc automatiquement à partir de la dernière publication.

## Réparer un historique

`restore_state.py` rebâtit `state.json` depuis une ou plusieurs publications
`data.json`, et sait déclarer un service en ligne sur une période non mesurée
pour effacer les zones grises d'une interruption connue de la sonde :

```bash
git show <commit-sain>:data.json > /tmp/bon.json
python3 restore_state.py --data /tmp/bon.json --data data.json \
                         --up ktv --since-epoch <timestamp>
python3 monitor.py
```

Ajouter `--dry-run` pour vérifier avant d'écrire.

Deux façons de déclarer une période en ligne :

- par défaut, les créneaux manquants sont comblés comme s'ils avaient été
  relevés : le graphe est vert plein et la période compte dans l'uptime ;
- avec `--no-backfill`, seule la date de mise en ligne est posée. Le journal
  des transitions suffit alors à colorer la période en hachuré « présumé en
  ligne », sans prétendre l'avoir mesurée ni la faire entrer dans l'uptime.

Dans les deux cas le remplissage respecte la rétention de chaque palier : une
date vieille de plusieurs mois ne crée pas de créneaux de 5 min qui seraient
élagués au passage suivant.

### Cron

```cron
* * * * * /usr/bin/python3 /home/dietpi/status-page/monitor.py
```

Aucune redirection : `monitor.py` tient lui-même son journal dans
`status.log`, avec rotation par taille — trois fichiers de 256 Ko, soit 768 Ko
au plus, une dizaine de jours à raison d'une exécution par minute. Le fichier
est ignoré par Git.

Rediriger depuis le cron serait une mauvaise idée à deux titres : vers
`/var/log`, la redirection échoue avant que la commande démarre et cron
n'exécute alors rien du tout, sans que rien n'apparaisse nulle part ; ailleurs,
elle double les lignes déjà écrites par le journal et grossit sans limite.

Sonder et publier sont découplés : la sonde tourne à chaque passage, mais un
commit + push n'a lieu qu'au bout de `PUBLISH_EVERY` secondes — sauf changement
d'état, poussé immédiatement. Une minute de précision ne coûte donc pas
1440 push par jour au Pi.

`CHECK_INTERVAL` **doit** correspondre au cron : c'est la durée qu'un check en
échec représente dans le downtime, et le seuil d'obsolescence affiché par la
page. Le changer ne réinterprète pas l'historique déjà enregistré à une autre
cadence ; l'écart se résorbe au fil de la rétention.

## Configuration

```json
[
  {
    "id": "ktv",
    "name": "K.tv",
    "check_url": "http://127.0.0.1:8080/health",
    "timeout": 5,
    "expect_status": 200
  }
]
```

| Clé             | Requis | Défaut            | Rôle                                    |
|-----------------|--------|-------------------|-----------------------------------------|
| `id`            | oui    | —                 | Identifiant stable, clé de l'historique  |
| `name`          | oui    | —                 | Nom **codé**, affiché sur la page        |
| `check_url`     | oui    | —                 | URL sondée, jamais publiée               |
| `timeout`       | non    | `5`               | Secondes avant abandon                   |
| `expect_status` | non    | tout code 2xx/3xx | Impose un code HTTP exact                |

La page publie exactement quatre champs par service : `id`, `name`, `status` et
l'instant du dernier changement d'état. Rien d'autre ne sort de la machine —
ni URL sondée, ni lien, ni icône, ni domaine. `config.json` est ignoré par Git
et ne quitte jamais le Pi ; c'est là qu'on garde les adresses réelles.

Changer un `id` réinitialise l'historique du service concerné.

## Publication Git

- Changement d'état d'un service → commit dédié (`Alerte : changement d'état`).
- Sinon → le commit automatique précédent est **amendé** puis repoussé avec
  `--force-with-lease`, ce qui garde le dépôt à taille constante malgré 288
  exécutions par jour. Un vrai commit est ouvert au moins une fois par jour.
- Si quelqu'un a poussé entre-temps, le push est rejeté et le script se
  réaligne : il repart de la tête distante et repose `data.json` par-dessus.
  Le Pi n'est propriétaire que des données, jamais du code — un changement de
  code poussé depuis un poste est donc intégré, pas écrasé. Si le HEAD local
  portait quelque chose d'inédit, il est étiqueté `avant-realignement-<ts>`
  avant d'être défait, pour rester récupérable.
- Une amende laisse l'ancien commit dans le reflog, donc joignable, donc jamais
  élagué par `gc` : environ 3,5 Mo par jour de carte SD. Le dépôt local est
  compacté à chaque commit non amendé, donc au moins une fois par jour.

Le Pi n'est pas un poste de travail : n'y modifiez pas le code. Pour le mettre
à jour, `git fetch origin && git reset --hard origin/main`.

Pour désactiver ce comportement et garder un commit par exécution, mettre
`SQUASH_AUTO_COMMITS = False` dans `monitor.py`.
