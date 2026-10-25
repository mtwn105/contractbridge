"""Install the pinned official release, verifying its published SHA-256 manifest."""
import argparse
import hashlib
import io
import platform
import ssl
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = '1.34.1'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='.local/bin')
    parser.add_argument('--ca-file')
    args = parser.parse_args()
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if platform.system() != 'Linux' or arch is None:
        raise SystemExit('This installer targets Linux/WSL2 amd64 or arm64; use the official release for your platform.')
    context = ssl.create_default_context()
    if args.ca_file and Path(args.ca_file).exists():
        context.load_verify_locations(args.ca_file)
    name = f'oasdiff_{VERSION}_linux_{arch}.tar.gz'
    base = f'https://github.com/oasdiff/oasdiff/releases/download/v{VERSION}/'
    with urlopen(base + 'checksums.txt', context=context, timeout=60) as response:
        manifest = response.read(100000).decode()
    checksum = next((line.split()[0] for line in manifest.splitlines() if line.split()[-1].lstrip('*') == name), None)
    if checksum is None:
        raise SystemExit('Release checksum entry is missing')
    with urlopen(base + name, context=context, timeout=60) as response:
        data = response.read(25_000_001)
    if len(data) > 25_000_000 or hashlib.sha256(data).hexdigest() != checksum:
        raise SystemExit('Release checksum verification failed')
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        member = next(m for m in archive.getmembers() if m.name == 'oasdiff' and m.isfile())
        binary = archive.extractfile(member).read()
    target = Path(args.directory) / 'oasdiff'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(binary)
    target.chmod(0o755)
    print(f'Installed verified oasdiff {VERSION} at {target}')


if __name__ == '__main__':
    main()
