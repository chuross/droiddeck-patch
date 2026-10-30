"""Import local ARM64 Proton archives through Android's document picker."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parent
PATCHES = (ROOT / "proton-import.patch",)
HELPERS = ("droiddeck-proton-import",)
ASSETS = tuple("usr/local/bin/" + name for name in HELPERS)
KOTLIN = "app/src/main/java/com/droiddeck/launcher/session/ProtonImport.kt"
TEST = "app/src/test/java/com/droiddeck/launcher/session/ProtonImportTest.kt"


def check(source):
    if (source / KOTLIN).exists() or (source / TEST).exists():
        raise RuntimeError("Proton import integration changed; generated files already exist")
    failures = []
    for patch in PATCHES:
        result = subprocess.run(["git", "-C", str(source), "apply", "--check", str(patch)],
                                capture_output=True, text=True)
        if result.returncode == 0:
            return patch
        failures.append(patch.name + ":\n" + result.stderr)
    raise RuntimeError("Proton import integration changed; review the file picker:\n" + "\n".join(failures))


def apply(source):
    patch = check(source)
    subprocess.run(["git", "-C", str(source), "apply", str(patch)], check=True)
    shutil.copy2(ROOT / "ProtonImport.kt", source / KOTLIN)
    (source / TEST).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "ProtonImportTest.kt", source / TEST)

