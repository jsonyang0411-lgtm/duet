import argparse
from pathlib import Path
from PyInstaller.archive.readers import CArchiveReader

REQUIRED_DLLS = [
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
    "WCDB.dll",
    "wcdb_api.dll",
    "wx_key.dll",
]

def find_entry_name(toc: dict, dll: str) -> str | None:
    dll_lower = dll.lower()
    preferred = []
    fallbacks = []
    for name in toc:
        normalized = str(name).replace("\\", "/").lower()
        if normalized == dll_lower:
            fallbacks.append(name)
        elif normalized.endswith("/" + dll_lower):
            if normalized.startswith("native/windows/") or normalized.startswith("lib/"):
                preferred.append(name)
            else:
                fallbacks.append(name)
    return (preferred or fallbacks or [None])[0]

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True)
    parser.add_argument("--dest", default="native/windows")
    args = parser.parse_args()

    archive = CArchiveReader(args.archive)
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    missing = []
    for dll in REQUIRED_DLLS:
        entry_name = find_entry_name(archive.toc, dll)
        if not entry_name:
            missing.append(dll)
            continue
        data = archive.extract(entry_name)
        (dest / dll).write_bytes(data)
        print(f"restored {dll} from {entry_name}")

    if missing:
        print(f"Warning: missing DLLs: {', '.join(missing)}")
    else:
        print("All required DLLs successfully extracted!")

if __name__ == "__main__":
    main()
