# Observer, de l'intérieur du client, ce qu'il fait d'un effet de statut

Les trois points du chemin qui décident, avec leurs offsets relatifs au module
(base d'image 0x140000000 soustraite, parce que l'ASLR déplace la base à chaque
lancement) :

| offset | ce que c'est | ce que ça dit |
|---|---|---|
| `0x34cca5` | `ClientStatusEffectManager::HandleStatusEffectCommand` | le message est arrivé jusqu'au handler |
| `0x34ee58` | `StatusEffectManager::StatusEffectTableRowToId(int)` | `edx` = l'index de ligne, donc **quel effet le client comprend** |
| `0x98404c` | la création de l'instance | la valeur de retour : nulle = l'élément est jeté en silence |

La commande, à lancer dans la VM :

```
python3 hookcap.py capture --target dro_client64.exe ^
  --hook "dro_client64.exe+0x34cca5:handler:args=2" ^
  --hook "dro_client64.exe+0x34ee58:rowToId:args=2:ret" ^
  --hook "dro_client64.exe+0x98404c:createInstance:args=4:ret" ^
  -o effets.jsonl
```

Puis un seul Dragon Hide, et `effets.jsonl` contient :

* si `handler` n'apparaît pas — le message n'atteint pas le handler ;
* si `rowToId` apparaît avec `args[1]` = 5168 et 5169 — le client comprend bien
  « armure » et « résistance » de Dragon Hide, donc l'index est juste ;
* si `rowToId` montre d'autres nombres — l'index est décalé, et l'écart se lit
  directement ;
* si `createInstance` retourne 0 — l'élément est jeté à la création, et les
  conditions à examiner sont celles de `statuseffectmanager.cc` :
  `causer.isvalid()`, `effectTemplate->IsValid()`, `0 < causerLevel`,
  `MaxActorLevel >= causerLevel`.

Chacune de ces quatre sorties désigne une cause différente. C'est le seul point
du circuit qui n'est pas observable depuis le serveur, et il est le dernier.
