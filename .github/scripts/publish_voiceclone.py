"""Mirror explicitly selected APKs; no private source checkout or long-lived credentials."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = os.environ['GITHUB_REPOSITORY']
ALLOWED = {'voiceclone.apk', 'voiceclone-mf.apk', 'voiceclone-rf.apk', 'voiceclone-mf-rf.apk'}


def gh(*args, check=True):
    return subprocess.run(['gh', *args, '--repo', REPOSITORY], text=True, capture_output=True, check=check)


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def download(url, path, size, digest):
    request = urllib.request.Request(url, headers={'Authorization': '', 'Cookie': ''})
    actual = hashlib.sha256(); count = 0
    with urllib.request.urlopen(request, timeout=120) as response, path.open('wb') as output:
        while block := response.read(1024 * 1024):
            count += len(block)
            if count > size:
                raise ValueError('Download exceeds the declared size')
            actual.update(block); output.write(block)
    if count != size or actual.hexdigest() != digest:
        raise ValueError('Downloaded file checksum/size mismatch')


def publish(tag, title, notes, files, root):
    view = gh('release', 'view', tag, '--json', 'isDraft', check=False)
    exists = view.returncode == 0
    draft = exists and json.loads(view.stdout)['isDraft']
    if exists and not draft:
        print(f'Checking existing immutable release {tag}', flush=True)
    else:
        notes_file = root/'notes.md'; notes_file.write_text(notes)
        if not exists:
            gh('release', 'create', tag, '--draft', '--prerelease', '--title', title, '--notes-file', str(notes_file))
        # Only incomplete drafts may be resumed. Published releases are never overwritten.
        gh('release', 'upload', tag, *(str(path) for path in files), '--clobber')
        for path in files:
            directory = root/'verified'; directory.mkdir(exist_ok=True)
            gh('release', 'download', tag, '--pattern', path.name, '--dir', str(directory), '--clobber')
            if checksum(directory/path.name) != checksum(path):
                raise ValueError('Authenticated upload verification failed')
            (directory/path.name).unlink()
        gh('release', 'edit', tag, '--draft=false', '--prerelease')
    # End users never authenticate; require a full anonymous download, not just a HEAD request.
    for path in files:
        target = root/'anonymous-check'
        download(f'https://github.com/{REPOSITORY}/releases/download/{tag}/{path.name}', target, path.stat().st_size, checksum(path))
        target.unlink()
    print(f'Anonymous downloads verified: {tag}', flush=True)


def main():
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text())
    manifest = event.get('client_payload', {}).get('transfer_manifest') or event.get('inputs', {}).get('transfer_manifest')
    if isinstance(manifest, str): manifest = json.loads(manifest)
    if manifest.get('format_version') != 1 or not re.fullmatch(r'voiceclone-android-\d+-\d+', manifest['tag']):
        raise ValueError('Unexpected release identity')
    if not re.fullmatch('[0-9a-f]{40}', manifest['source_commit']):
        raise ValueError('Unexpected source commit')
    assets = manifest['assets']
    names = [asset['name'] for asset in assets]
    if not assets or len(names) != len(set(names)) or not set(names) <= ALLOWED:
        raise ValueError('Unexpected APK inventory')
    for asset in assets:
        url = urllib.parse.urlparse(asset['url'])
        if url.scheme != 'https' or url.hostname != 'release-assets.githubusercontent.com' or url.username or url.password:
            raise ValueError('Unexpected transfer host')
        if not 0 < asset['size'] < 2**31 or not re.fullmatch('[0-9a-f]{64}', asset['sha256']):
            raise ValueError('Invalid file checksum/size')
        # Read event inputs from disk so temporary transfer URLs never appear in step env logs.
        print('::add-mask::'+asset['url'], flush=True)
    with tempfile.TemporaryDirectory(prefix='voiceclone-') as temporary:
        root = Path(temporary)
        for asset in assets:
            download(asset['url'], root/asset['name'], asset['size'], asset['sha256'])
            print(f'APK checksum verified: {asset["name"]}', flush=True)
        for profile in ('mf-mixed8', 'rf-mixed8'):
            family = profile.split('-')[0]
            apk = next((root/name for name in (f'voiceclone-{family}.apk', 'voiceclone-mf-rf.apk') if (root/name).exists()), None)
            if apk is None: continue
            directory = root/profile; directory.mkdir()
            name = f'irodori-{profile}.zip'
            with zipfile.ZipFile(apk) as archive:
                catalog = json.loads(archive.read(f'assets/model-packs/{profile}.json'))
                if catalog['profile'] != profile or catalog['asset'] != name: raise ValueError('Wrong model catalog')
                with archive.open(f'assets/model-packs/{name}') as source, (directory/name).open('wb') as output:
                    shutil.copyfileobj(source, output, 1024*1024)
            if (directory/name).stat().st_size != catalog['size'] or checksum(directory/name) != catalog['sha256']:
                raise ValueError('Bundled model checksum failed')
            with zipfile.ZipFile(directory/name) as pack:
                model = json.loads(pack.read('manifest.json'))
                if model['profile'] != profile or not model.get('attribution'): raise ValueError('Missing model attribution')
                for entry in model['files']:
                    with pack.open(entry['name']) as stream:
                        if hashlib.file_digest(stream, 'sha256').hexdigest() != entry['sha256']:
                            raise ValueError('Model weights failed their checksum')
            (directory/'download.json').write_text(json.dumps(catalog, indent=2)+'\n')
            publish(f'models-{profile}-v1', f'VoiceClone {profile} model',
                    'VoiceClone用モデル。アプリ内からアカウント登録なしで取得できます。ライセンス・出典はZIP内manifest.jsonに含まれます。',
                    [directory/name, directory/'download.json'], root)
            shutil.rmtree(directory)
        files = [root/name for name in names]
        sums = root/'SHA256SUMS'
        sums.write_text(''.join(f'{checksum(path)}  {path.name}\n' for path in files))
        notes = ('VoiceClone Android（arm64）\n\n'
                 '- voiceclone.apk：約35MBのアプリ本体。モデルはアプリ内で別途準備します。\n'
                 '- voiceclone-mf.apk：約1.08GB。普段使いのMFモデルを同梱。\n'
                 '- voiceclone-rf.apk：約1.08GB。比較用のRFモデルを同梱。\n'
                 '- voiceclone-mf-rf.apk：約2.12GB。両モデルを同梱。\n'
                 '同梱版では「モデル」→「アプリ内のモデルを準備」。上書き更新で声と準備済みモデルを保持します。\n'
                 'モデルの重みは同じです。APKの分割で音質・生成速度は変わりません。\n'
                 'SHA256SUMSにチェックサムを記載しています。実機インストールは未確認です。\n\n'
                 'Source commit: '+manifest['source_commit']+'\n')
        publish(manifest['tag'], 'VoiceClone Android '+manifest['tag'].removeprefix('voiceclone-android-'), notes, [*files, sums], root)
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write(f'VoiceClone APK downloads: https://github.com/{REPOSITORY}/releases/tag/{manifest["tag"]}\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Exceptions can include signed URLs. Keep temporary access parameters out of logs.
        print(f'Publication failed ({type(error).__name__}); unpublished drafts are retained.', flush=True)
        raise SystemExit(1)
