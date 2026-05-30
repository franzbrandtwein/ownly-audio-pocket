"""p4a build hook: ensure de.ownly.ownlyaudiopocket.ForegroundAudioService
is declared in the AndroidManifest of the built APK, with its own
`android:process=":audio"` so that ExoPlayer survives even when the
activity process gets frozen by aggressive OEMs.

Why both hook phases?
    p4a v2024.01.21's `after_apk_build` runs AFTER gradle has sealed the
    APK — at that point patching AndroidManifest.xml on disk does NOT
    change what's inside the APK. We therefore also patch in
    `before_apk_build` (which runs BEFORE gradle assemble) and again in
    `after_apk_build` as a safety net. The function is idempotent.

Why recursive search?
    Depending on bootstrap (sdl2 / webview) and p4a version, the
    generated manifest can live at any of:
        ./AndroidManifest.xml
        ./src/main/AndroidManifest.xml
        ./templates/AndroidManifest.tmpl.xml
    To be robust against future restructures we just `os.walk` the cwd
    and patch every AndroidManifest*.xml we encounter.
"""

import os
import re
import glob


DESIRED_SERVICE_XML = (
    '    <service\n'
    '        android:name="de.ownly.ownlyaudiopocket.ForegroundAudioService"\n'
    '        android:foregroundServiceType="mediaPlayback"\n'
    '        android:process=":audio"\n'
    '        android:exported="false" />\n'
)

# Matches any existing ForegroundAudioService block in any whitespace/attr layout.
_STALE_RE = re.compile(
    r'\s*<service\b[^>]*?android:name="de\.ownly\.ownlyaudiopocket\.ForegroundAudioService"[^/]*?/>\s*',
    re.DOTALL,
)


def _find_manifests():
    cwd = os.getcwd()
    candidates = set()

    # Direct lookups (fast paths)
    for rel in (
        'AndroidManifest.xml',
        'src/main/AndroidManifest.xml',
        'templates/AndroidManifest.tmpl.xml',
    ):
        p = os.path.abspath(rel)
        if os.path.isfile(p):
            candidates.add(p)

    # Recursive walk as a safety net — capped to ~depth 6 to avoid pulling
    # in node_modules / .gradle caches.
    for root, dirs, files in os.walk(cwd):
        depth = root[len(cwd):].count(os.sep)
        if depth > 6:
            dirs[:] = []
            continue
        # Skip common heavyweight caches/output dirs
        dirs[:] = [d for d in dirs if d not in (
            '.gradle', '.git', 'build', 'intermediates', 'node_modules')]
        for fn in files:
            if fn.startswith('AndroidManifest') and fn.endswith('.xml'):
                candidates.add(os.path.join(root, fn))

    return sorted(candidates)


def _patch_one(path):
    try:
        with open(path, 'r') as f:
            content = f.read()
    except Exception as e:
        print(f'[hook] read fail {path}: {e}')
        return False

    # Only patch files that actually look like an Android app manifest.
    if '<application' not in content:
        return False

    original = content

    # If already fully up-to-date, nothing to do.
    if DESIRED_SERVICE_XML in content:
        print(f'[hook] up-to-date: {path}')
        return False

    # Strip any stale ForegroundAudioService declaration (e.g. without :audio).
    new_content, removed = _STALE_RE.subn('\n', content)
    if removed:
        print(f'[hook] stripped {removed} stale service block(s) in {path}')
        content = new_content

    if '</application>' not in content:
        print(f'[hook] no </application> in {path}; skipping')
        return False

    content = content.replace(
        '</application>',
        DESIRED_SERVICE_XML + '    </application>',
        1,
    )

    if content == original:
        return False

    try:
        with open(path, 'w') as f:
            f.write(content)
        print(f'[hook] patched: {path}')
        return True
    except Exception as e:
        print(f'[hook] write fail {path}: {e}')
        return False


def _run_patch(phase):
    print(f'[hook] ===== ForegroundAudioService manifest patch ({phase}) =====')
    print(f'[hook] cwd = {os.getcwd()}')
    manifests = _find_manifests()
    if not manifests:
        print('[hook] no AndroidManifest*.xml files found')
        return
    patched = 0
    for m in manifests:
        if _patch_one(m):
            patched += 1
    print(f'[hook] {phase}: patched {patched}/{len(manifests)} manifest(s)')


# p4a hook entry points — registered automatically when this file is
# referenced as `p4a.hook = p4a_hook.py` in buildozer.spec.

def before_apk_build(ctx):
    _run_patch('before_apk_build')


def after_apk_build(ctx):
    _run_patch('after_apk_build')


# Some p4a versions/branches call differently named hooks — wire them up
# defensively so the patch happens no matter what.

def before_apk_assemble(ctx):
    _run_patch('before_apk_assemble')


def after_apk_assemble(ctx):
    _run_patch('after_apk_assemble')


def before_build(ctx):
    _run_patch('before_build')
