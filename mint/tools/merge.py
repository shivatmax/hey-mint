"""Merging folders the careful way: "merge acme_old and 'Acme Corp (copy)' into Acme, keep the newest".

Every file under the source folders (subfolders too) moves into the target, flat. When a file with that name
is already there, the one modified most recently stays in the target and the other goes into target/older
(both keep their real names; a clash in older gets " 2"). Nothing is deleted; folders left empty are
removed; every move is one undo step. The model doing this move by move renamed files ("brief_old.txt") and
kept the wrong copy (bench hard-acme-merge).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _free(path: Path) -> Path:
    if not path.exists():
        return path
    for n in range(2, 100):
        other = path.with_name(f"{path.stem} {n}{path.suffix}")
        if not other.exists():
            return other
    return path


def merge_folders(args: dict) -> str:
    from mint.tools import harness as harness_tools
    from mint.tools import undo
    target, why = harness_tools._resolve(str(args.get("into") or ""), must_exist=False)
    if target is None:
        return f"FAILED: {why}"
    sources = []
    for raw in args.get("folders") or []:
        folder, why = harness_tools._resolve(str(raw), must_exist=True)
        if folder is None:
            return f"FAILED: {why}"
        if not folder.is_dir():
            return f"FAILED: {harness_tools._short(folder)} is not a folder."
        if folder.resolve() == target.resolve() or target.resolve() in folder.resolve().parents:
            continue
        sources.append(folder)
    if not sources:
        return "FAILED: name the folders to merge (folders) and the one to merge them into (into)."
    for path in [target, *sources]:
        blocked = harness_tools._blocked(path, write=True)
        if blocked:
            return f"FAILED: {blocked}"
    older_name = str(args.get("older_folder") or "older")
    target.mkdir(parents=True, exist_ok=True)
    moved, kept_older, replaced = [], [], []
    with undo.together("file", f"merging {len(sources)} folder(s) into {target.name}"):
        for source in sources:
            for root, dirs, files in os.walk(source):
                dirs[:] = [d for d in dirs if not d.startswith(".")]
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    path = Path(root) / name
                    there = target / name
                    if not there.exists():
                        shutil.move(str(path), str(there))
                        undo.record("file", f"moving {name}", {"kind": "file_move", "from": str(there), "to": str(path)})
                        moved.append(name)
                        continue
                    older_dir = target / older_name
                    older_dir.mkdir(exist_ok=True)
                    if path.stat().st_mtime > there.stat().st_mtime:
                        # The incoming copy is newer: the one in the target steps aside into older/.
                        aside = _free(older_dir / name)
                        shutil.move(str(there), str(aside))
                        undo.record("file", f"moving the older {name} aside",
                                    {"kind": "file_move", "from": str(aside), "to": str(there)})
                        shutil.move(str(path), str(there))
                        undo.record("file", f"moving the newer {name}",
                                    {"kind": "file_move", "from": str(there), "to": str(path)})
                        replaced.append(name)
                    else:
                        aside = _free(older_dir / name)
                        shutil.move(str(path), str(aside))
                        undo.record("file", f"moving the older {name} aside",
                                    {"kind": "file_move", "from": str(aside), "to": str(path)})
                        kept_older.append(name)
        removed = []
        if args.get("remove_empty", True) is not False:
            for source in sources:
                for root, dirs, files in sorted(os.walk(source, topdown=False), key=lambda t: -len(t[0])):
                    folder = Path(root)
                    try:
                        leftovers = [p for p in folder.iterdir() if p.name != ".DS_Store"]
                        if not leftovers:
                            (folder / ".DS_Store").unlink(missing_ok=True)
                            folder.rmdir()
                            if folder == source:
                                removed.append(folder.name)
                    except OSError:
                        pass
    short = harness_tools._short
    return (f"Merged {len(sources)} folder(s) into {short(target)}: {len(moved)} moved in"
            + (f"; {len(replaced)} newer copies replaced older ones there ({', '.join(replaced)}) - the older "
               f"ones are in {older_name}/" if replaced else "")
            + (f"; {len(kept_older)} older copies went to {older_name}/ ({', '.join(kept_older)})" if kept_older else "")
            + (f". Removed the emptied folder(s): {', '.join(removed)}" if removed else "")
            + ". Nothing was deleted; 'undo that' puts it all back.")


PROMPT = """Merging folders ("merge these folders into X, keep the newest", "combine my duplicate client folders") \
-> merge_folders: it moves everything in (flat), keeps the most recently modified copy of each name, puts the older \
ones in X/older with their real names, and removes the emptied folders. Only list the folders the user means."""


def declarations():
    from google.genai import types
    S = types.Type.STRING
    return [types.FunctionDeclaration(
        name="merge_folders",
        description=("Merge folders into one, flat (files in subfolders too): the newest copy of each file name "
                     "stays, older copies go to an 'older' subfolder with their real names, emptied folders are "
                     "removed. Never deletes files; one undo puts it back."),
        parameters=types.Schema(type=types.Type.OBJECT, properties={
            "folders": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=S),
                                    description="the folders to merge in (not the target)"),
            "into": types.Schema(type=S, description="the folder to merge into (made if missing)"),
            "older_folder": types.Schema(type=S, description="name of the subfolder for older copies (default older)"),
            "remove_empty": types.Schema(type=types.Type.BOOLEAN, description="remove emptied folders (default true)")},
            required=["folders", "into"]))]


HANDLERS = {"merge_folders": merge_folders}
