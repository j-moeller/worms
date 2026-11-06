"""Install a pinned OpenTofu release into the disposable Linux test image."""
import hashlib
import io
import os
from pathlib import Path
import urllib.request
import zipfile

version = os.environ["OPENTOFU_VERSION"]
arch = os.environ["TARGETARCH"]
asset = f"tofu_{version}_linux_{arch}.zip"
base = f"https://github.com/opentofu/opentofu/releases/download/v{version}"
with urllib.request.urlopen(f"{base}/{asset}", timeout=120) as response:
    archive = response.read()
with urllib.request.urlopen(f"{base}/tofu_{version}_SHA256SUMS", timeout=30) as response:
    sums = response.read().decode()
expected = next(line.split()[0] for line in sums.splitlines() if line.split()[-1].lstrip("*") == asset)
assert hashlib.sha256(archive).hexdigest() == expected, "OpenTofu checksum mismatch"
with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
    binary = bundle.read("tofu")
path = Path("/usr/local/bin/tofu")
path.write_bytes(binary)
path.chmod(0o755)
