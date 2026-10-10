"""Download the public release using the real shipped updater, never install into a user folder."""
import json
import argparse
from pathlib import Path
import tempfile
import time
from app.update_service import fetch_manifest,DEFAULT_MANIFEST_URL
from tests.verify_release_package import verify
import updater_app

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--manifest-url',default=DEFAULT_MANIFEST_URL)
    args=parser.parse_args()
    local=json.loads(Path('D:/Codes/gupiao/release/v1.6.4-latest.json').read_text(encoding='utf-8'))
    manifest=fetch_manifest(args.manifest_url)
    assert manifest.version=='1.6.4',manifest.version
    assert manifest.sha256==local['sha256'],'Remote manifest checksum differs'
    print(json.dumps({'remote_manifest':{'version':manifest.version,'checksum_matches':True,'download_sources':1+len(manifest.urls)}}),flush=True)
    with tempfile.TemporaryDirectory(prefix='gupiao-public-download-') as folder:
        path=Path(folder)/'update.zip'
        attempts=[]
        last=[None]
        def progress(count,total,attempt,sources):
            if last[0]!=attempt:
                last[0]=attempt;attempts.append(attempt)
                print(json.dumps({'download_attempt':attempt,'resume_bytes':count,'total':total,'sources':sources}),flush=True)
        started=time.monotonic()
        updater_app._download(manifest.url,manifest.sha256,path,urls=manifest.urls,progress=progress)
        print(json.dumps({'public_download':{'bytes':path.stat().st_size,'sha256_verified':True,'seconds':round(time.monotonic()-started,2),'attempts':attempts}}),flush=True)
        verify(path)

if __name__=='__main__':main()
