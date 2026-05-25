"""
p4a build hook: injects ForegroundAudioService into AndroidManifest.xml.

android.extra_manifest_xml writes into <manifest> (top level), but
<service> declarations must be inside <application>. This hook runs
after p4a generates the manifest but before Gradle compiles it.
"""


def after_apk_build(ctx):
    import os

    manifest_path = 'AndroidManifest.xml'
    if not os.path.exists(manifest_path):
        print('[hook] AndroidManifest.xml not found — skipping service patch')
        return

    with open(manifest_path, 'r') as f:
        content = f.read()

    if 'ForegroundAudioService' in content:
        print('[hook] ForegroundAudioService already in manifest')
        return

    service_xml = (
        '    <service\n'
        '        android:name="de.ownly.ownlyaudiopocket.ForegroundAudioService"\n'
        '        android:foregroundServiceType="mediaPlayback"\n'
        '        android:exported="false" />\n'
    )

    if '</application>' not in content:
        print('[hook] WARNING: </application> not found in AndroidManifest.xml')
        return

    content = content.replace('</application>', service_xml + '    </application>')
    with open(manifest_path, 'w') as f:
        f.write(content)
    print('[hook] Patched AndroidManifest.xml: added ForegroundAudioService')
