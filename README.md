# NODAO Tools — canal public d'auto-update

Le code source et les tests métier restent dans NODAO-Tools (privé). Ce dépôt distribue les RBZ validés.

Un commit de staging apporte le RBZ entier, son SHA256SUMS.txt et releases/vX.Y.Z.json. Le workflow publish-rbz.yml teste le pipeline, télécharge publiquement le binaire par SHA de commit, vérifie ZIP/CRC, versions, taille et SHA, puis avance latest.json dans un second commit. Une publication interrompue avant validation ne modifie pas le canal stable.

Relance : Actions → Publish NODAO RBZ → Run workflow → version (ou Re-run jobs). Un binaire déjà publié ne peut pas être remplacé sous la même version. Aucun force-push de tag ; aucune branche de version n'est créée. latest.json.ref est un SHA complet, jamais un nom ambigu.

Le champ tag est informatif. L'updater existant accepte les SHA et conserve sa vérification SHA256 avant installation. Les anciennes branches sans homonyme sont historiques ; le doublon branche/tag v1.4.4 est retiré uniquement après vérification de leur égalité et migration du manifest vers un SHA.

Le transport automatisé peut utiliser le secret NODAO_UPDATES_TOKEN configuré dans le dépôt source (Contents read/write sur ce dépôt). Sans ce secret, le connecteur GitHub de maintenance transfère le blob binaire complet et la demande de release. Aucun fragment Base64 ni token committé.

Versions majeures V2/V3/V4 : release explicite sur le canal principal après validation produit. Versions mineures/correctives, y compris 1.5.x : auto-update normal. La politique complète est dans docs/32_VERSIONING_MIGRATIONS.txt du dépôt source.
