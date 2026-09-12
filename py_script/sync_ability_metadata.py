"""Refresh tree metadata from cached Wynncraft v3 tree/map responses.

Usage: python3 py_script/sync_ability_metadata.py data/temp

Reads api_ability_{tree,map}_<class>.json. Writes candidate files to the
same directory; never invents calculation effects for newly added abilities.
The API map is needed because tree.links can retain deleted node references.
"""

import argparse
import copy
import json
import re
from pathlib import Path

from get_atree import sub


CLASSES = ["Archer", "Warrior", "Mage", "Assassin", "Shaman"]
ROOT = Path(__file__).resolve().parents[1]
ICONS = {"White": "node_0", "Yellow": "node_1", "Purple": "node_2",
         "Red": "node_3", "Blue": "node_4"}


def plain(text):
    return re.sub(r"§.", "", re.sub(r"<[^>]*>", "", text))


def description(lines):
    result = []
    for line in lines:
        if line == '</br>':
            result.append('</br>')
            continue
        text = plain(line)
        if any(marker in text for marker in
               ["Archetype", "Ability Points:", "Unlocking will block:"]):
            break
        text = sub(text)
        # The API mixes the resource pack glyphs with Unicode symbols.
        for symbol, label in [("✣", "Damage"), ("✦", "Thunder"),
                              ("✹", "Fire"), ("❉", "Water")]:
            text = text.replace(symbol + " " + label, label)
        text = re.sub(r"[\ue000-\uf8ff⚔✧➼☀⌛⌚♚]", "", text)
        result.append(text.strip())
    return " ".join(result).strip().removeprefix("</br>").removesuffix("</br>").strip()


def canonical(name):
    if name.startswith("Cheaper ") or name in ("More Focus I", "More Puppets I"):
        name = name.removesuffix(" I")
    return {"Spear Proficiency 1": "Spear Proficiency I",
            "Relik Proficiency 1": "Relik Proficiency I",
            "Nightcloak Knife": "Nightcloak Knives",
            "Hummingbirds' Song": "Hummingbird's Song"}.get(name, name)


def rename_refs(value, names):
    if isinstance(value, dict):
        return {k: rename_refs(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [rename_refs(v, names) for v in value]
    if isinstance(value, str):
        if value in names:
            return names[value]
        head, dot, tail = value.partition(".")
        if dot and head in names:
            return names[head] + dot + tail
    return value


def sync_tree(previous, tree, layout):
    live = {key: node for page in tree["pages"].values() for key, node in page.items()}
    names = {key: plain(node["name"]) for key, node in live.items()}
    renamed = {node["display_name"]: canonical(node["display_name"]) for node in previous}
    known = {renamed[node["display_name"]]: rename_refs(node, renamed) for node in previous}
    positions = {entry["meta"]["id"]: entry["coordinates"]
                 for page in layout.values() for entry in page if entry["type"] == "ability"}
    assert set(positions) == set(live), "Tree and connection map have different abilities"
    result = {}
    for key, node in live.items():
        name = names[key]
        out = copy.deepcopy(known.get(name, {"display_name": name, "properties": {}, "effects": []}))
        out["desc"] = description(node["description"])
        out["api_id"] = key
        out["parents"] = []
        requirements = node["requirements"]
        out["dependencies"] = [names[target] for target in requirements.get("NODE", [])]
        out["blockers"] = [names[target] for target in node["locks"] or [] if target in names]
        out["cost"] = requirements["ABILITY_POINTS"]
        for field in ("archetype", "archetype_req", "req_archetype", "level_req"):
            out.pop(field, None)
        for line in node["description"]:
            text = plain(line).strip()
            if text.endswith(" Archetype"):
                out["archetype"] = text.removesuffix(" Archetype")
        if "ARCHETYPE" in requirements:
            arch = requirements["ARCHETYPE"]
            out["archetype_req"] = arch["amount"]
            req_name = plain(tree["archetypes"][arch["name"]]["name"])
            if out.get("archetype") != req_name:
                out["req_archetype"] = req_name
        if "COMBAT_LEVEL" in requirements:
            out["level_req"] = requirements["COMBAT_LEVEL"]
        api_icon = node["icon"]["value"]["name"]
        icon = api_icon.removeprefix("abilityTree.node")
        if api_icon.startswith("abilityTree.ultimate"):
            # The local atlas represents ultimates with the red ability icon.
            icon_name = "node_3"
        else:
            icon_name = ICONS.get(icon, "node_" + icon.lower())
        out["display"] = {"row": positions[key]["y"] - 1,
                          "col": positions[key]["x"] - 1,
                          "icon": icon_name}
        result[name] = out

    edges = set()
    for page in layout.values():
        for entry in page:
            for a, b in entry["meta"].get("paths", []):
                if a in names and b in names:
                    edges.add(tuple(sorted((a, b))))
    # Adjacent abilities have no intervening connector tile.
    for key, node in live.items():
        for target in node["links"] or []:
            if target in names:
                edges.add(tuple(sorted((key, target))))
    for a, b in sorted(edges):
        first, second = result[names[a]], result[names[b]]
        if first["display"]["row"] <= second["display"]["row"]:
            second["parents"].append(names[a])
        if second["display"]["row"] <= first["display"]["row"]:
            first["parents"].append(names[b])
    # A lock is mutual even when the API lists it on only one of the nodes.
    for name, node in result.items():
        for blocker in node["blockers"]:
            if name not in result[blocker]["blockers"]:
                result[blocker]["blockers"].append(name)
    # Some API links jump over another ability on their drawn path. Such a
    # connector must terminate at that ability before continuing to the child.
    for node in result.values():
        parents = []
        for parent_name in node['parents']:
            parent = result[parent_name]
            pr, pc = parent['display']['row'], parent['display']['col']
            cr, cc = node['display']['row'], node['display']['col']
            between = [other for other in result.values() if other not in (node, parent) and (
                (other['display']['row'] == pr and min(pc,cc) < other['display']['col'] < max(pc,cc)) or
                (other['display']['col'] == cc and pr < other['display']['row'] < cr))]
            if between:
                parent_name = min(between, key=lambda other:
                    abs(other['display']['row']-cr)+abs(other['display']['col']-cc))['display_name']
            if parent_name not in parents:
                parents.append(parent_name)
        node['parents'] = parents
    ordered = [result.pop(name) for name in known if name in result]
    added = list(result)
    ordered.extend(result.values())
    return ordered, renamed, added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    args = parser.parse_args()
    baseline = ROOT / "data/baseline"
    trees = json.loads((baseline / "atree_constants.json").read_text())
    majors = json.loads((args.cache / "major_ids_clean.json").read_text())
    aspects = json.loads((baseline / "aspects.json").read_text())
    for cls in CLASSES:
        tree = json.loads((args.cache / f"api_ability_tree_{cls.lower()}.json").read_text())
        layout = json.loads((args.cache / f"api_ability_map_{cls.lower()}.json").read_text())
        trees[cls], renamed, added = sync_tree(trees[cls], tree, layout)
        aspects[cls] = rename_refs(aspects[cls], renamed)
        for major in majors.values():
            major["abilities"] = [rename_refs(abil, renamed) if abil["class"] == cls else abil
                                  for abil in major["abilities"]]
        print(f"{cls}: {len(trees[cls])} nodes; implement new effects: {', '.join(added) or 'none'}")
    for filename, data in [("atree_constants", trees), ("aspects", aspects), ("major_ids_clean", majors)]:
        (args.cache / f"{filename}.json").write_text(json.dumps(data, ensure_ascii=False, indent=4) + "\n")


if __name__ == "__main__":
    main()
