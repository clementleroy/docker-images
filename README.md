# Images Docker custom

Sources et constructions versionnées, indépendantes de la configuration privée de domotique.
Les exemples utilisent des identifiants fictifs ; aucun historique du dépôt privé n’est importé.

| Image | Version | Rôle |
| --- | --- | --- |
| `ghcr.io/clementleroy/docker-images-frigate-video-fixes` | `0.18.2-20261101` | Correctifs miniatures et nginx pour Frigate |
| `ghcr.io/clementleroy/docker-images-conseils-air` | `1.2.0` | Conseils d’aération, application Home Assistant conservée |

## Construire et publier

Le workflow GitHub Actions construit sur `main` ou par lancement manuel depuis `main`.
Il teste les sources Frigate natives et six scénarios nginx dans un conteneur sans
réseau, puis les 33 tests Conseils Air. Les images initiales ciblent `linux/amd64`.
La base Frigate est épinglée par digest ; les empreintes des fichiers sont vérifiées
avant modification. Les contextes Docker ne copient aucune donnée de fonctionnement.

Augmenter `IMAGE_VERSION` dans le dossier concerné après une modification, puis
committer et pousser. Un tag existant est conservé. Les nouveaux tags de version
sont accompagnés d’un tag `sha-<commit complet>` ; aucun tag `latest`.

GitHub crée les nouveaux packages GHCR en privé par défaut, même si les sources
sont publiques. Après le premier push, passer chaque package en Public dans
**Package settings → Change visibility**, puis vérifier le téléchargement anonyme.
Le workflow affiche la visibilité réellement retournée par GitHub.

## Utiliser dans Compose

Après publication et vérification du téléchargement, remplacer uniquement l’image
du service Frigate dans son Compose existant :

```yaml
services:
  frigate:
    image: ghcr.io/clementleroy/docker-images-frigate-video-fixes:0.18.2-20261101
```

Conserver les autres réglages, volumes, ports et services. Valider le Compose avant
une mise à jour ciblée. Une ancienne version reste disponible pour retour arrière.

Conseils Air dépend des API du Supervisor Home Assistant et du service MQTT ; son
image ne constitue pas une application Compose autonome. `config.json` contient
un exemple générique pour les tests et la configuration d’application HA. Elle
n’est pas activée par la publication de ce dépôt.

## Développer

Les tests Frigate sont exécutés au build contre les sources de la base exacte.
Sur une nouvelle base, revoir les empreintes et les tests avant de changer le digest.

```sh
docker build -t frigate-video-fixes services/frigate-video-fixes
docker run --rm -i --network none --entrypoint python3 frigate-video-fixes - \
  < services/frigate-video-fixes/check_nginx_runtime.py
docker build -t conseils-air apps/conseils_air
docker run --rm --network none --entrypoint python \
  --mount "type=bind,src=$PWD/apps/conseils_air,dst=/tests,readonly" \
  -w /tests conseils-air -m unittest discover -s /tests -p 'test_*.py'
```
