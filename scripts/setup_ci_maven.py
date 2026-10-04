"""Install the CI Maven distribution with its published SHA-512 checksum."""
import hashlib
import os
import tarfile
import urllib.request
from pathlib import Path

directory = Path(os.environ['RUNNER_TEMP']) / 'java-update-maven'
directory.mkdir()
url = 'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.11/apache-maven-3.9.11-bin.tar.gz'
with urllib.request.urlopen(url + '.sha512', timeout=60) as response:
    checksum = response.read().decode().strip().split()[0]
with urllib.request.urlopen(url, timeout=60) as response:
    data = response.read()
assert hashlib.sha512(data).hexdigest() == checksum, 'Maven distribution checksum mismatch'
archive = directory / 'maven.tar.gz'
archive.write_bytes(data)
with tarfile.open(archive) as handle:
    handle.extractall(directory, filter='data')
with Path(os.environ['GITHUB_PATH']).open('a') as handle:
    handle.write(str(directory / 'apache-maven-3.9.11/bin') + '\n')
