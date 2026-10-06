"""Reuse the pinned Maven archive, verifying its upstream checksum on every run."""
import hashlib
import os
import tarfile
import urllib.request
from pathlib import Path

VERSION = '3.9.11'
URL = f'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/{VERSION}/apache-maven-{VERSION}-bin.tar.gz'


def install(cache: Path, directory: Path) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f'apache-maven-{VERSION}-bin.tar.gz'
    with urllib.request.urlopen(URL + '.sha512', timeout=60) as response:
        checksum = response.read().decode().strip().split()[0]
    if not archive.is_file() or hashlib.sha512(archive.read_bytes()).hexdigest() != checksum:
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read()
        if hashlib.sha512(data).hexdigest() != checksum:
            raise RuntimeError('Maven distribution checksum mismatch')
        archive.write_bytes(data)
    directory.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as handle:
        handle.extractall(directory, filter='data')
    return directory / f'apache-maven-{VERSION}/bin'


if __name__ == '__main__':
    executable_directory = install(Path.home() / '.cache/java-update/maven',
                                   Path(os.environ['RUNNER_TEMP']) / 'java-update-maven')
    with Path(os.environ['GITHUB_PATH']).open('a') as handle:
        handle.write(str(executable_directory) + '\n')
