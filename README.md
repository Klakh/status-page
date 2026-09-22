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
python3 monitor.py --once   # premier passage, quitte après
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

### Service systemd

`monitor.py` tourne en processus persistant (plus en cron) : il sonde toutes
les `POLL_INTERVAL` secondes sans jamais recharger l'état depuis le disque
entre deux sondes, ce qui rend le downtime affiché précis à quelques secondes
au lieu d'à la minute.

```bash
sudo cp monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now monitor.service
```

Adapter `User=` et les chemins dans `monitor.service` s'ils diffèrent de
`/home/dietpi/status-page`. `Restart=always` relance le service s'il plante ;
`journalctl -u monitor -f` montre les tracebacks éventuels, `status.log` le
reste (échecs de check, publications).

`python3 monitor.py --once` reste disponible pour un passage unique manuel
(test, dépannage) — c'est aussi ce que fait un premier lancement.

Trois cadences distinctes, de la plus chère à la moins chère, chacune
indépendante des deux autres :

- **Sonder** (`POLL_INTERVAL`, 5 s par défaut) : une requête HTTP sur le LAN,
  quasi gratuite. C'est elle qui borne la précision du downtime — une panne
  est confirmée au plus tard `FAILURES_BEFORE_DOWN * POLL_INTERVAL` secondes
  après son début.
- **Écrire sur la carte SD** (`STATE_FLUSH_EVERY`, 60 s par défaut) :
  `data.json`/`state.json` ne sont réécrits (avec `fsync`) qu'à ce rythme, sauf
  changement d'état où c'est immédiat — c'est justement l'instant qui doit
  être précis, pas l'attente entre deux. Sonder plus vite n'use donc pas la
  carte SD plus vite.
- **Publier** (`PUBLISH_EVERY`, 300 s par défaut) : un commit + push coûte bien
  plus cher qu'une écriture locale. Sonder plus souvent ne pousse donc pas
  plus souvent.

Changer `POLL_INTERVAL` ne réinterprète pas l'historique déjà enregistré à une
autre cadence ; l'écart se résorbe au fil de la rétention (jusqu'à 48 h pour le
graphe le plus fin).

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

## Alertes Discord

Chaque changement d'état est envoyé dans un salon Discord via un webhook
(Paramètres du salon → Intégrations → Webhooks → copier l'URL) :

```bash
cp notify.json.example notify.json
$EDITOR notify.json
```

Sans `notify.json`, aucune alerte n'est envoyée. Le fichier est ignoré par Git :
quiconque connaît l'URL peut écrire dans le salon.

- L'alerte part **avant** le commit + push, pour ne pas attendre Git.
- Si Discord est injoignable, l'alerte est gardée dans `state.json` et
  retentée à chaque passage, puis abandonnée au bout de 6 h.
- Le retour en ligne indique la durée de l'interruption.

Le Pi ne peut pas annoncer sa propre panne. Pour ça, un service externe du type
[healthchecks.io](https://healthchecks.io) (gratuit) fait « homme mort » : il
prévient sur Discord ou par mail quand ses pings cessent. Avec `monitor.py` en
service persistant, ce ping doit venir d'un minuteur systemd indépendant — pas
d'un `&&` après le script, qui ne s'applique plus — pour continuer à détecter
un processus figé, pas seulement arrêté :

```cron
# crontab séparée, uniquement pour le ping "homme mort"
* * * * * systemctl is-active --quiet monitor.service && curl -fsS -m 10 --retry 3 -o /dev/null https://hc-ping.com/<uuid>
```

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
