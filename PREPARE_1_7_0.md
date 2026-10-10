# Préparation 1.7.0 — canal stable inchangé

Cette branche contient uniquement la préparation documentaire. Ne pas déposer de
request 1.7.0 dans releases/ sur main avant validation native utilisateur : le push
déclencherait le workflow de publication. Aucun RBZ candidat public ou tag 1.7.0
n'est publié pendant cette phase. latest.json reste 1.6.0, updater_schema 1.

Audit : 17 RBZ ZIP/CRC valides ; demandes 1.5.1, 1.5.2, 1.5.3 et 1.6.0 avec
taille/SHA exacts. Anciennes stables, URLs et branches de version conservées.
noop-temp (44bd3062b46e066edca1ca74790b34a08699922f) est un ancêtre de main, sans
travail unique et absent des références de latest.json historiques : suppression sûre.
v1.4.4-staging possède un commit divergent (checksum + ancien latest), à traiter
séparément sans jamais fusionner le downgrade du manifeste. Aucun artefact supprimé.
API GitHub Releases/Actions et HTTP public inaccessibles dans le cloud (403) ;
objets et références Git vérifiés. Ne pas confondre ce contrôle avec un téléchargement
HTTP natif consommé par l'updater.

Après accord utilisateur : intégrer le source validé à main, tag source immuable,
build déterministe, transfert RBZ entier + checksum + release request, vérification
publique à son SHA, tag Updates immuable, latest dans le second commit et vérification
publique finale. Conserver la stable 1.6.0 pour le test d'update réel vers 1.7.0.
Le ref du futur manifeste est le SHA public du commit contenant le RBZ, jamais le SHA
privé du source. Les templates candidats restent hors releases/ et latest.json.

Procédure native détaillée : docs/RELEASE_1_7_0.md dans le dépôt source.
Pas de modification des scripts/workflows de publication : leurs 15 tests sont verts.

## Candidat privé vérifié

Source : `06cb7e0468b760ab910826251644595aff818252` sur lot-data-v2.
RBZ : `nodao_tools_v1.7.0.rbz`, 114960 octets.
SHA256 : `2a989405d9b53e228fbc3781787f2e2bb594ac66c06e4bed3351cb147e65eefd`.
Deux builds identiques, ZIP/CRC, versions 1.7.0 et schéma 26 vérifiés.
Ce RBZ reste un candidat de test ; aucun artefact public 1.7.0, aucune request dans
releases/, aucun tag stable 1.7.0 et aucun changement de latest.json dans ce commit.
