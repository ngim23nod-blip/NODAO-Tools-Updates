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

class ChannelGitHub:
    def __init__(self, request, data):
        self.base = 'a' * 40
        self.files = {'latest.json': r.encode_json({'channel': 'stable', 'version': '1.7.0', 'ref': '0'*40}),
                      request['artifact']['path']: data,
                      str(Path(request['artifact']['path']).with_name('SHA256SUMS.txt')): r.checksum(request)}
        self.tags = {}
        self.commits = []
        self.downloads = []
        self.fail_commit = False
    def head(self):
        return self.base
    def raw(self, path, ref, public=False, missing=False):
        self.downloads.append((path, ref, public))
        if path not in self.files:
            if missing:
                return None
            raise KeyError(path)
        return self.files[path]
    def call(self, path, method='GET', payload=None, **kwargs):
        if path.startswith('git/ref/heads/'):
            return None
        if path.startswith('git/ref/tags/'):
            return self.tags.get(path.removeprefix('git/ref/tags/'))
        if path == 'git/refs' and method == 'POST':
            tag = payload['ref'].removeprefix('refs/tags/')
            self.tags[tag] = {'object': {'sha': payload['sha']}}
            return self.tags[tag]
        raise AssertionError(path)
    def commit(self, base, files, message):
        if self.fail_commit:
            raise RuntimeError('interrupted')
        self.commits.append(files)
        self.files.update(files)
        self.base = 'b'*40
        return self.base


class ChannelReleaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'nodao_tools').mkdir()
        (self.root/'test/fixtures').mkdir(parents=True)
        (self.root/'test/fixtures/NODAO_RELEASE_TEST.json').write_text('{"project_id":"NODAO_RELEASE_TEST"}\n')
        self.write_version('1.8.0-test.1')
        self.req = r.build(self.root,'c'*40,'2026-10-10',channel='test')
        self.data = (self.root/self.req['artifact']['path']).read_bytes()
        self.api = ChannelGitHub(self.req,self.data)
        self.fixture = (self.root/self.req['test_fixture']['path']).read_bytes()
        self.api.files[self.req['test_fixture']['path']] = self.fixture
    def write_version(self, version):
        for name in ['nodao_tools.rb','nodao_tools/version.rb']:
            (self.root/name).write_text(f"VERSION = '{version}'\n")
    def test_ordered_candidate_progression_numeric_and_stable(self):
        versions = ['1.7.1','1.8.0-test.1','1.8.0-test.2','1.8.0-test.10','1.8.0-rc.1','1.8.0-rc.10','1.8.0','1.8.1-test.1']
        self.assertEqual(versions,sorted(reversed(versions),key=r.version_key))
        for invalid in ['01.8.0','1.8.0-test.01','1.8.0-test','1.8.0-beta.1','1.8.0+machine']:
            with self.assertRaises(ValueError):
                r.version_key(invalid)
    def test_candidate_build_reproducible_and_immutable_with_fixture(self):
        self.assertEqual(self.req,r.build(self.root,'d'*40,'2026-10-11',channel='test'))
        self.assertEqual(self.data,(self.root/self.req['artifact']['path']).read_bytes())
        (self.root/'nodao_tools/extra.rb').write_text('# changed\n')
        with self.assertRaisesRegex(ValueError,'already built'):
            r.build(self.root,'d'*40,'2026-10-11',channel='test')
    def test_fixture_bytes_cannot_change_under_same_version(self):
        (self.root/'test/fixtures/NODAO_RELEASE_TEST.json').write_text('{"project_id":"CHANGED"}')
        with self.assertRaisesRegex(ValueError,'fixture already built'):
            r.build(self.root,'d'*40,'2026-10-11',channel='test')
    def test_prerelease_never_built_or_published_on_stable(self):
        with self.assertRaisesRegex(ValueError,'Prerelease'):
            r.build(self.root,'c'*40,'2026-10-10')
        invalid = dict(self.req,channel='stable')
        with self.assertRaisesRegex(ValueError,'Prerelease'):
            r.publish(self.api,invalid)
        self.assertEqual([],self.api.commits)
    def test_test_publication_only_changes_test_manifest_and_test_tag(self):
        stable = self.api.files['latest.json']
        manifest = r.publish(self.api,self.req)
        self.assertEqual('test',manifest['channel'])
        self.assertEqual(self.req['test_fixture'],manifest['test_fixture'])
        self.assertEqual(stable,self.api.files['latest.json'])
        self.assertEqual(['test-v1.8.0-test.1'],list(self.api.tags))
        self.assertEqual([{'channels/test/latest.json', 'fixtures/NODAO_RELEASE_TEST.json'}],[set(commit) for commit in self.api.commits])
        self.assertTrue(all(public for path,ref,public in self.api.downloads if path.endswith(('.rbz','NODAO_RELEASE_TEST.json'))))
        self.assertEqual(manifest,r.publish(self.api,self.req))
        self.assertEqual(1,len(self.api.commits))
    def test_resume_publication_after_tag_never_changes_stable(self):
        stable = self.api.files['latest.json']
        self.api.fail_commit = True
        with self.assertRaises(RuntimeError):
            r.publish(self.api,self.req)
        self.api.fail_commit = False
        self.api.base = 'e'*40
        self.assertEqual('a'*40,r.publish(self.api,self.req)['ref'])
        self.assertEqual(stable,self.api.files['latest.json'])
    def test_older_candidate_and_wrong_channel_manifest_blocked(self):
        for current in [{'channel':'test','version':'1.8.0-test.2'},{'channel':'stable','version':'1.7.0'}]:
            self.api.files['channels/test/latest.json'] = r.encode_json(current)
            with self.assertRaises(ValueError):
                r.publish(self.api,self.req)
        self.assertEqual([],self.api.commits)
    def test_corrupt_public_artifact_or_fixture_never_creates_tag_or_manifest(self):
        for field in ['artifact','test_fixture']:
            original = self.api.files[self.req[field]['path']]
            self.api.files[self.req[field]['path']] = b'bad'
            with self.assertRaises(ValueError):
                r.publish(self.api,self.req)
            self.assertEqual({},self.api.tags)
            self.assertEqual([],self.api.commits)
            self.api.files[self.req[field]['path']] = original
    def test_staging_test_receipt_does_not_touch_stable_requests_or_manifest(self):
        self.api.files.pop(self.req['artifact']['path'])
        self.api.files.pop(self.req['test_fixture']['path'])
        stable = self.api.files['latest.json']
        r.stage(self.api,self.req,self.data,self.fixture)
        self.assertIn('releases/test/v1.8.0-test.1.json',self.api.files)
        self.assertNotIn('releases/v1.8.0-test.1.json',self.api.files)
        self.assertEqual(stable,self.api.files['latest.json'])
    def test_stable_manifest_and_old_request_keep_original_paths(self):
        self.assertEqual('latest.json',r.manifest_path('stable'))
        self.assertEqual('releases/v1.7.1.json',r.request_path({'version':'1.7.1'}))
        with self.assertRaises(ValueError):
            r.request_channel({'channel':'unknown'})


if __name__ == '__main__':
    unittest.main()
