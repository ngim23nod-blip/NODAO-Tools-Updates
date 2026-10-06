#!/usr/bin/env python3
"""Verified RBZ build and two-phase GitHub publication (standard library only)."""
import argparse
import base64
import datetime
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

UPDATES = 'ngim23nod-blip/NODAO-Tools-Updates'
REPO_ID = 1384193965
SEMVER = re.compile(r'\d+\.\d+\.\d+')
SHA = re.compile(r'[0-9a-f]{40}')
HASH = re.compile(r'[0-9a-f]{64}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def version_in(text):
    match = re.search(r"^\s*VERSION = '([0-9]+\.[0-9]+\.[0-9]+)'", text, re.M)
    require(match is not None, 'VERSION missing')
    return match[1]


def artifact_path(version):
    require(isinstance(version, str) and SEMVER.fullmatch(version), 'Invalid version')
    return f'dist/v{version}/nodao_tools_v{version}.rbz'


def validate_archive(data, version):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'Duplicate ZIP entry')
        for name in names:
            p = PurePosixPath(name)
            require(not p.is_absolute() and '..' not in p.parts and '\\' not in name,
                    'Unsafe ZIP path')
            require(name == 'nodao_tools.rb' or name.startswith('nodao_tools/'),
                    'Unexpected RBZ root')
        require(archive.testzip() is None, 'ZIP CRC failure')
        for name in ('nodao_tools.rb', 'nodao_tools/version.rb'):
            require(version_in(archive.read(name).decode('utf-8')) == version,
                    'Archive/source version mismatch')


def validate_request(request):
    version = request['version']
    require(request['artifact']['path'] == artifact_path(version), 'Artifact path mismatch')
    require(HASH.fullmatch(request['artifact']['sha256']) is not None, 'Invalid SHA256')
    require(type(request['artifact']['size']) is int and request['artifact']['size'] > 0,
            'Invalid size')
    require(SHA.fullmatch(request['source_commit']) is not None, 'Invalid source commit')
    datetime.date.fromisoformat(request['released'])
    return request


def verify(data, request):
    validate_request(request)
    require(len(data) == request['artifact']['size'], 'Published size mismatch')
    require(hashlib.sha256(data).hexdigest() == request['artifact']['sha256'],
            'Published SHA256 mismatch')
    validate_archive(data, request['version'])


def encode_json(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode('utf-8')


def checksum(request):
    a = request['artifact']
    return f"{a['sha256']}  {PurePosixPath(a['path']).name}\n".encode()


def build(root, source_commit, released):
    require(SHA.fullmatch(source_commit) is not None, 'Invalid source commit')
    root = Path(root)
    version = version_in((root / 'nodao_tools.rb').read_text())
    require(version_in((root / 'nodao_tools/version.rb').read_text()) == version,
            'Source versions disagree')
    files = [root / 'nodao_tools.rb'] + sorted((root / 'nodao_tools').rglob('*'))
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in files:
            require(not path.is_symlink(), 'Symlinks not allowed in RBZ')
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            z.writestr(info, path.read_bytes(), compresslevel=9)
    data = stream.getvalue()
    request = {'version': version, 'released': released, 'source_commit': source_commit,
               'artifact': {'path': artifact_path(version), 'size': len(data),
                            'sha256': hashlib.sha256(data).hexdigest()}}
    verify(data, request)
    target = root / request['artifact']['path']
    if target.exists():
        require(target.read_bytes() == data, 'Version already built with different bytes; bump VERSION')
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.with_name('SHA256SUMS.txt').write_bytes(checksum(request))
    receipt = target.with_name('release.json')
    # Keep the original provenance/date on a byte-identical rerun.
    if receipt.exists():
        old = json.loads(receipt.read_text())
        verify(data, old)
        request = old
    receipt.write_bytes(encode_json(request))
    return request


class GitHub:
    def __init__(self, repo, token=None):
        require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo), 'Invalid repository')
        self.repo, self.token = repo, token
        self.base = f'https://api.github.com/repos/{repo}/'

    def call(self, path, method='GET', payload=None, raw=False, public=False, missing=False):
        headers = {'Accept': 'application/vnd.github.raw+json' if raw else 'application/vnd.github+json',
                   'User-Agent': 'NODAO-Release', 'X-GitHub-Api-Version': '2022-11-28'}
        if self.token and not public:
            headers['Authorization'] = 'Bearer ' + self.token
        data = None if payload is None else encode_json(payload)
        for attempt in range(4):
            try:
                req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=60) as response:
                    body = response.read()
                return body if raw or not body else json.loads(body)
            except urllib.error.HTTPError as exc:
                if missing and exc.code == 404:
                    return None
                if method != 'GET' or exc.code not in (429, 500, 502, 503, 504) or attempt == 3:
                    raise RuntimeError(f'GitHub {method} {path}: HTTP {exc.code}') from None
                time.sleep(2 ** attempt)

    def head(self):
        return self.call('git/ref/heads/main')['object']['sha']

    def raw(self, path, ref, public=False, missing=False):
        return self.call('contents/' + urllib.parse.quote(path, safe='/') + '?ref=' + ref,
                         raw=True, public=public, missing=missing)

    def commit(self, base, files, message):
        entries = []
        for path, data in files.items():
            sha = None
            if data is not None:
                sha = self.call('git/blobs', 'POST', {
                    'encoding': 'base64', 'content': base64.b64encode(data).decode('ascii')})['sha']
            entries.append({'path': path, 'mode': '100644', 'type': 'blob', 'sha': sha})
        tree = self.call('git/commits/' + base)['tree']['sha']
        tree = self.call('git/trees', 'POST', {'base_tree': tree, 'tree': entries})['sha']
        commit = self.call('git/commits', 'POST', {
            'message': message, 'tree': tree, 'parents': [base]})['sha']
        require(self.head() == base, 'Concurrent main change; rerun safely')
        self.call('git/refs/heads/main', 'PATCH', {'sha': commit, 'force': False})
        return commit


def stage(api, request, data):
    verify(data, request)
    base = api.head()
    path = request['artifact']['path']
    existing = api.raw(path, base, missing=True)
    request_path = f"releases/v{request['version']}.json"
    files = {}
    if existing is not None:
        verify(existing, request)
        require(existing == data, 'Existing version differs')
    else:
        files[path] = data
    sums_path = str(PurePosixPath(path).with_name('SHA256SUMS.txt'))
    sums = api.raw(sums_path, base, missing=True)
    require(sums is None or sums == checksum(request), 'Existing checksum differs')
    if sums is None:
        files[sums_path] = checksum(request)
    old = api.raw(request_path, base, missing=True)
    if old is not None:
        require(json.loads(old) == request, 'Existing release request differs')
    else:
        files[request_path] = encode_json(request)
    return api.commit(base, files, f"Stage verified V{request['version']} release") if files else base


def validate_manifest(manifest, request):
    require(manifest['product'] == 'NODAO Tools' and manifest['channel'] == 'stable', 'Invalid channel')
    require(manifest['repo_id'] == REPO_ID and manifest['updater_schema'] == 1, 'Invalid updater identity')
    require(manifest['version'] == request['version'], 'Manifest version mismatch')
    require(manifest['artifact'] == request['artifact'], 'Manifest artifact mismatch')
    require(SHA.fullmatch(manifest['ref']) is not None, 'Manifest ref must be a full commit SHA')


def publish(api, request):
    validate_request(request)
    base = api.head()
    current = json.loads(api.raw('latest.json', base))
    version = request['version']
    require(tuple(map(int, version.split('.'))) >= tuple(map(int, current['version'].split('.'))),
            'Refusing stable downgrade')
    tag = 'v' + version
    require(api.call('git/ref/heads/' + tag, missing=True) is None, 'Release branch conflicts with tag')
    existing = api.call('git/ref/tags/' + tag, missing=True)
    # Resume after a tag was created but before the manifest was committed.
    ref = current['ref'] if current['version'] == version else (existing['object']['sha'] if existing else base)
    require(SHA.fullmatch(ref) is not None, 'Expected immutable artifact commit')
    data = api.raw(request['artifact']['path'], ref, public=True)
    verify(data, request)
    sums_path = str(PurePosixPath(request['artifact']['path']).with_name('SHA256SUMS.txt'))
    require(api.raw(sums_path, ref, public=True) == checksum(request), 'Published SHA256SUMS differs')
    if existing is None:
        api.call('git/refs', 'POST', {'ref': 'refs/tags/' + tag, 'sha': ref})
    else:
        require(existing['object']['sha'] == ref, 'Immutable tag points elsewhere')
    manifest = {'product': 'NODAO Tools', 'channel': 'stable', 'version': version,
                'released': request['released'], 'repo_id': REPO_ID, 'updater_schema': 1,
                'ref': ref, 'tag': tag, 'artifact': request['artifact']}
    validate_manifest(manifest, request)
    if current['version'] == version:
        validate_manifest(current, request)
        return current
    api.commit(base, {'latest.json': encode_json(manifest)}, f'Publish verified V{version} stable manifest')
    final = json.loads(api.raw('latest.json', 'main', public=True))
    validate_manifest(final, request)
    verify(api.raw(final['artifact']['path'], final['ref'], public=True), request)
    return final


def retire_synced_legacy_branch(api):
    # One-time migration of the documented historical collision; never force a ref.
    branch = api.call('git/ref/heads/v1.4.4', missing=True)
    tag = api.call('git/ref/tags/v1.4.4', missing=True)
    if branch and tag:
        require(branch['object'] == tag['object'], 'Legacy refs diverge; manual audit required')
        latest = json.loads(api.raw('latest.json', api.head()))
        require(SHA.fullmatch(latest['ref']) is not None, 'Migrate stable manifest before retiring branch')
        api.call('git/refs/heads/v1.4.4', 'DELETE')


def main():
    parser = argparse.ArgumentParser(__doc__)
    parser.add_argument('command', choices=['build', 'stage', 'publish', 'verify'])
    parser.add_argument('--root', default='.')
    parser.add_argument('--source-commit', default=os.environ.get('GITHUB_SHA', ''))
    parser.add_argument('--released', default=datetime.date.today().isoformat())
    parser.add_argument('--request')
    args = parser.parse_args()
    if args.command == 'build':
        result = build(args.root, args.source_commit, args.released)
    else:
        require(args.request, '--request required')
        request = json.loads(Path(args.request).read_text())
        validate_request(request)
        api = GitHub(UPDATES, os.environ.get('NODAO_RELEASE_TOKEN'))
        if args.command == 'stage':
            require(api.token, 'NODAO_RELEASE_TOKEN required')
            data = (Path(args.root) / request['artifact']['path']).read_bytes()
            result = {'staged_commit': stage(api, request, data)}
        elif args.command == 'publish':
            require(api.token, 'NODAO_RELEASE_TOKEN required')
            retire_synced_legacy_branch(api)
            result = publish(api, request)
        else:
            result = json.loads(api.raw('latest.json', 'main', public=True))
            validate_manifest(result, request)
            verify(api.raw(result['artifact']['path'], result['ref'], public=True), request)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
