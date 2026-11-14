"""Build a source-only release from tracked files; never copy workspace state."""
import argparse
import hashlib
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath


PRIVATE_ROOTS = {'.git', '.local', '.venv', '.maintenance', 'data', 'backups', 'secrets', 'models', 'releases'}
PRIVATE_SUFFIXES = {'.pem', '.key', '.dump', '.pgdump', '.backup', '.zip', '.gz'}
PRIVATE_NAMES = {'console.psd1', 'server_known_hosts', 'known_hosts', 'id_rsa', 'id_ed25519', 'id_ecdsa'}
KEY_MARKER = re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----')


def source_files(root):
    result = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True)
    files = []
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        name = raw.decode('utf-8')
        relative = PurePosixPath(name)
        if (relative.is_absolute() or '..' in relative.parts or
                relative.parts[0] in PRIVATE_ROOTS or '__pycache__' in relative.parts or
                relative.suffix.lower() in PRIVATE_SUFFIXES or relative.name in PRIVATE_NAMES or
                (relative.name.startswith('.env') and relative.name != '.env.example')):
            raise ValueError(f'Private or generated path is tracked: {name}')
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Source must be a regular existing file: {name}')
        content = path.read_bytes()
        if KEY_MARKER.search(content):
            raise ValueError(f'Private key material in source: {name}')
        files.append((name, content))
    return sorted(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='validate tracked files without writing a package')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    files = source_files(root)
    if args.check:
        print(f'Source boundary check passed: {len(files)} tracked files; no private state included.')
        return
    version = (root / 'VERSION').read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'[0-9]+\.[0-9]+(?:\.[0-9]+)?', version):
        raise ValueError('Invalid release version')
    output = args.output or root / 'releases' / version / f'wechat-vault-{version}-source.zip'
    output = output.resolve()
    release_root = (root / 'releases').resolve()
    if not output.is_relative_to(release_root):
        raise ValueError('Release outputs must be inside this checkout releases directory')
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = f'wechat-vault-{version}/'
    manifest = ''.join(f'{hashlib.sha256(content).hexdigest()}  {name}\n' for name, content in files)
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name, content):
            info = zipfile.ZipInfo(prefix + name)
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if name.endswith('.sh') else 0o100644) << 16
            archive.writestr(info, content)
        for name, content in files:
            add(name, content)
        add('MANIFEST.sha256', manifest.encode('utf-8'))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_name(output.name + '.sha256').write_text(f'{checksum}  {output.name}\n', encoding='utf-8')
    print(f'Created {output.name}: {len(files)} source files; SHA-256 {checksum}')


if __name__ == '__main__':
    main()
