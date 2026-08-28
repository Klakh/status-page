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

### Cron

```cron
*/5 * * * * /usr/bin/python3 /home/dietpi/status-page/monitor.py >> /var/log/status-page.log 2>&1
```

Si l'intervalle change, ajuster `CHECK_INTERVAL` dans `monitor.py` : c'est ce
qui permet à la page de signaler des données périmées.

## Configuration

```json
[
  {
    "id": "ktv",
    "name": "KTV",
    "check_url": "http://127.0.0.1:8080/health",
    "public_url": "https://example.com",
    "icon": "https://example.com/favicon.ico",
    "timeout": 5,
    "expect_status": 200
  }
]
```

| Clé             | Requis | Défaut                     | Rôle                                            |
|-----------------|--------|----------------------------|-------------------------------------------------|
| `id`            | oui    | —                          | Identifiant stable, clé de l'historique          |
| `name`          | oui    | —                          | Nom affiché                                      |
| `check_url`     | oui    | `public_url`               | URL sondée                                       |
| `public_url`    | non    | —                          | Lien cliquable sur la carte                      |
| `icon`          | non    | —                          | Icône affichée à côté du nom                     |
| `timeout`       | non    | `5`                        | Secondes avant abandon                           |
| `expect_status` | non    | tout code 2xx/3xx          | Impose un code HTTP exact                        |

Changer un `id` réinitialise l'historique du service concerné.

## Publication Git

- Changement d'état d'un service → commit dédié (`Alerte : changement d'état`).
- Sinon → le commit automatique précédent est **amendé** puis repoussé avec
  `--force-with-lease`, ce qui garde le dépôt à taille constante malgré 288
  exécutions par jour. Un vrai commit est ouvert au moins une fois par jour.
- Si quelqu'un a poussé entre-temps, le `--force-with-lease` échoue, le script
  rebase et repart sur un commit normal : aucun travail distant n'est écrasé.

Pour désactiver ce comportement et garder un commit par exécution, mettre
`SQUASH_AUTO_COMMITS = False` dans `monitor.py`.
