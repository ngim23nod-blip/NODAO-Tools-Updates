import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release', ROOT / 'scripts/release.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


class FakeGitHub:
    def __init__(self, request, data):
        self.request, self.data = request, data
        self.current = {'version': '1.5.0', 'ref': '0' * 40}
        self.tag = None
        self.branch = None
        self.commits = []
        self.fail_commit = False
        self.downloads = []
        self.base = 'a' * 40

    def head(self):
        return self.base

    def raw(self, path, ref, public=False, missing=False):
        self.downloads.append((path, ref, public))
        if path == 'latest.json':
            return r.encode_json(self.current)
        if path.endswith('SHA256SUMS.txt'):
            return r.checksum(self.request)
        return self.data

    def call(self, path, method='GET', payload=None, **kwargs):
        if path.startswith('git/ref/heads/'):
            return self.branch
        if path.startswith('git/ref/tags/'):
            return self.tag
        if path == 'git/refs' and method == 'POST':
            self.tag = {'object': {'sha': payload['sha']}}
            return self.tag
        raise AssertionError(path)

    def commit(self, base, files, message):
        if self.fail_commit:
            raise RuntimeError('Concurrent main change')
        self.commits.append(files)
        self.current = json.loads(files['latest.json'])
        self.base = 'b' * 40
        return self.base


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'nodao_tools').mkdir()
        for name in ('nodao_tools.rb', 'nodao_tools/version.rb'):
            (self.root / name).write_text("VERSION = '1.5.1'\n")
        (self.root / 'nodao_tools/loader.rb').write_text('# loader\n')
        self.req = r.build(self.root, 'c' * 40, '2026-10-06')
        self.data = (self.root / self.req['artifact']['path']).read_bytes()
        self.api = FakeGitHub(self.req, self.data)

    def test_build_reproducible_and_checksums(self):
        second = r.build(self.root, 'd' * 40, '2026-10-07')
        self.assertEqual(self.req, second)
        self.assertEqual(self.data, (self.root / second['artifact']['path']).read_bytes())
        self.assertEqual(r.checksum(second), (self.root / 'dist/v1.5.1/SHA256SUMS.txt').read_bytes())

    def test_disagreeing_source_versions_fail(self):
        (self.root / 'nodao_tools.rb').write_text("VERSION = '1.5.2'\n")
        with self.assertRaisesRegex(ValueError, 'versions disagree'):
            r.build(self.root, 'c' * 40, '2026-10-06')

    def test_same_version_changed_bytes_fail(self):
        (self.root / 'nodao_tools/loader.rb').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'already built'):
            r.build(self.root, 'c' * 40, '2026-10-06')

    def test_archive_version_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, 'version mismatch'):
            r.validate_archive(self.data, '1.5.2')

    def test_missing_version_rb_fails(self):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr('nodao_tools.rb', "VERSION = '1.5.1'\n")
        with self.assertRaises(KeyError):
            r.validate_archive(out.getvalue(), '1.5.1')

    def test_invalid_zip_fails(self):
        with self.assertRaises(zipfile.BadZipFile):
            r.validate_archive(b'not zip', '1.5.1')

    def test_public_corruption_cannot_advance_manifest(self):
        self.api.data = self.data[:-1] + bytes([self.data[-1] ^ 1])
        with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
            r.publish(self.api, self.req)
        self.assertEqual([], self.api.commits)
        self.assertIsNone(self.api.tag)

    def test_public_truncation_cannot_advance_manifest(self):
        self.api.data = self.data[:-1]
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            r.publish(self.api, self.req)
        self.assertEqual([], self.api.commits)

    def test_publish_verifies_public_bytes_then_manifest_and_is_idempotent(self):
        manifest = r.publish(self.api, self.req)
        self.assertEqual('a' * 40, manifest['ref'])
        self.assertEqual(self.req['artifact'], manifest['artifact'])
        self.assertTrue(all(public for path, ref, public in self.api.downloads if path.endswith('.rbz')))
        self.assertEqual(manifest, r.publish(self.api, self.req))
        self.assertEqual(1, len(self.api.commits))

    def test_resume_after_tag_before_manifest_with_new_main(self):
        self.api.fail_commit = True
        with self.assertRaises(RuntimeError):
            r.publish(self.api, self.req)
        self.assertEqual('1.5.0', self.api.current['version'])
        self.api.fail_commit = False
        self.api.base = 'e' * 40
        self.assertEqual('a' * 40, r.publish(self.api, self.req)['ref'])

    def test_branch_collision_blocks_publication(self):
        self.api.branch = {'object': {'sha': 'a' * 40}}
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            r.publish(self.api, self.req)
        self.assertEqual([], self.api.commits)

    def test_downgrade_rejected(self):
        self.api.current['version'] = '1.6.0'
        with self.assertRaisesRegex(ValueError, 'downgrade'):
            r.publish(self.api, self.req)

    def test_manifest_ref_must_be_commit(self):
        manifest = r.publish(self.api, self.req)
        manifest['ref'] = 'v1.5.1'
        with self.assertRaisesRegex(ValueError, 'full commit'):
            r.validate_manifest(manifest, self.req)

    def test_bad_path_and_hash_rejected(self):
        for field, bad in [('path', '../evil.rbz'), ('sha256', 'bad'), ('size', 0)]:
            request = json.loads(json.dumps(self.req))
            request['artifact'][field] = bad
            with self.assertRaises(ValueError):
                r.validate_request(request)

    def test_stage_rejects_existing_different_binary(self):
        self.api.data = b'wrong'
        with self.assertRaisesRegex(ValueError, 'size mismatch'):
            r.stage(self.api, self.req, self.data)
        self.assertEqual([], self.api.commits)


if __name__ == '__main__':
    unittest.main()
