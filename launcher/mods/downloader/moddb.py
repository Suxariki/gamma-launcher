import json
import re

from bs4 import BeautifulSoup
from pathlib import Path
from requests.exceptions import HTTPError, RequestException
from typing import Dict, Optional

from launcher.exceptions import HashError, ModDBDownloadError
from launcher.mods.downloader.base import DefaultDownloader, g_session

LOCAL_MODDB_CACHE_DIR = ".gamma-launcher-moddb"


class ModDBDownloader(DefaultDownloader):
    "Specialization of `launcher.mods.downloader.base.DefaultDownloader` to manage ModDB URLs"

    def __init__(self, url: str, iurl: str) -> None:
        super().__init__(url)
        self._iurl = iurl

    @staticmethod
    def _parse_moddb_metadata(url: str) -> Dict[str, str]:
        r = g_session.get(url)

        if r.status_code != 200:
            r.raise_for_status()

        soup = BeautifulSoup(r.text, features="html.parser")
        result = {}

        for i in soup.body.find_all('div', attrs={'class': "row clear"}):
            try:
                name = i.h5.text
                value = i.span.text.strip()
            except AttributeError:
                # if div have no h5 or span child, just ignore it.
                continue

            # We can parse more, but we don't need it.
            if name in ('Filename', 'MD5 Hash'):
                result[name] = value
        try:
            result['Download'] = soup.find(id='downloadmirrorstoggle')['href'].strip()
        except TypeError:
            pass

        return result

    @staticmethod
    def _get_download_url(url: str) -> str:
        id = url.split('/')[-1]
        s = re.search(f'/downloads/mirror/{id}/[^"]*', g_session.get(url).text)
        if not s:
            raise ModDBDownloadError(f"Download link not found when requesting {url}")

        mirror = f"https://www.moddb.com{s[0]}"
        # Same-origin navigation: mirror expects a normal browser session (see g_session).
        r = g_session.get(
            mirror,
            allow_redirects=False,
            headers={"Referer": url},
        )
        loc = r.headers.get("Location") or r.headers.get("location")
        if r.status_code not in (301, 302, 303, 307, 308) or not loc:
            raise ModDBDownloadError(
                f"ModDB mirror request failed ({r.status_code}) for {mirror}; "
                "no redirect to file host — site layout or anti-bot rules may have changed."
            )
        return loc

    def _try_resolve_mirror_when_still_start_url(self) -> None:
        """Sidecar cache restores filename/hash but leaves `_url` as .../start/<id>. Network
        downloads must use the mirror redirect (CDN); otherwise GET returns HTML (~8 KiB)."""
        u = self._url
        if (
            'moddb.com/addons/start/' not in u
            and 'moddb.com/downloads/start/' not in u
        ):
            return
        try:
            self._url = self._get_download_url(u)
        except (HTTPError, ModDBDownloadError, RequestException):
            pass

    @staticmethod
    def _download_id_from_url(url: str) -> Optional[str]:
        m = re.search(r'/(?:downloads|addons)/start/(\d+)', url)
        return m.group(1) if m else None

    def _local_moddb_meta_file(self, to: Path, download_id: str) -> Path:
        return to / LOCAL_MODDB_CACHE_DIR / f'{download_id}.json'

    def _persist_local_moddb_cache(self, to: Path, download_id: str) -> None:
        if not self._user_wanted_name:
            return
        meta_dir = to / LOCAL_MODDB_CACHE_DIR
        meta_dir.mkdir(parents=True, exist_ok=True)
        data = {'filename': self._user_wanted_name}
        if self._archivehash:
            data['md5'] = self._archivehash
        self._local_moddb_meta_file(to, download_id).write_text(
            json.dumps(data, indent=2), encoding='utf-8'
        )

    def _apply_local_moddb_cache(self, to: Path, download_id: str) -> bool:
        path = self._local_moddb_meta_file(to, download_id)
        if not path.is_file():
            return False
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return False
        fn = data.get('filename')
        if not fn:
            return False
        archive_path = to / fn
        if not archive_path.is_file():
            return False
        self._user_wanted_name = fn
        md5 = data.get('md5')
        self._archivehash = md5 if md5 else None
        return True

    def _set_vars_from_metadata(self):
        metadata: Dict[str, str] = {}
        if not self._iurl:
            return metadata

        try:
            metadata = self._parse_moddb_metadata(self._iurl)
            self._archivehash = metadata.get('MD5 Hash', None)
            self._user_wanted_name = metadata.get('Filename', None)
        except HTTPError:
            metadata = {}

        return metadata

    def check(self, to: Path, update_cache: bool = False) -> None:
        if not self._iurl:
            raise HashError('No Info URL provided for this mod')

        metadata = self._set_vars_from_metadata()

        if not self._user_wanted_name:
            raise ModDBDownloadError(f'Could not find Filename in {self._iurl}')

        if not self._archivehash:
            raise ModDBDownloadError(f'Could not find archive hash in {self._iurl}')

        if metadata.get('Download', '') not in self._url:
            raise ModDBDownloadError(f'Skipping {self._user_wanted_name} since ModDB info do not match download url')

        self._url = self._get_download_url(self._url)

        super().check(to, update_cache)

    def download(self, to: Path, use_cached: bool = False, *args, **kwargs) -> Path:
        download_id = self._download_id_from_url(self._url)

        # Prefer disk when use_cached: avoids relying on ModDB HTML when the archive + sidecar exist.
        if use_cached and download_id and self._apply_local_moddb_cache(to, download_id):
            self._try_resolve_mirror_when_still_start_url()
            return super().download(to, use_cached=True)

        mirror_ok = False
        try:
            self._set_vars_from_metadata()
            if self._user_wanted_name:
                self._url = self._get_download_url(self._url)
                mirror_ok = True
        except (HTTPError, ModDBDownloadError, RequestException):
            mirror_ok = False

        def _maybe_persist_sidecar() -> None:
            if download_id and self._user_wanted_name:
                meta_path = self._local_moddb_meta_file(to, download_id)
                if not meta_path.is_file():
                    self._persist_local_moddb_cache(to, download_id)

        if mirror_ok:
            try:
                result = super().download(to, use_cached)
            except RequestException:
                if use_cached and download_id and self._apply_local_moddb_cache(to, download_id):
                    return super().download(to, use_cached=True)
                raise
            except Exception:
                if use_cached and download_id and self._apply_local_moddb_cache(to, download_id):
                    return super().download(to, use_cached=True)
                raise
            else:
                _maybe_persist_sidecar()
                return result

        if use_cached and download_id and self._apply_local_moddb_cache(to, download_id):
            return super().download(to, use_cached=True)

        raise ModDBDownloadError(
            'ModDB is unreachable and there is no local cache entry for this download. '
            f'Complete one successful download while ModDB is online (metadata is saved under '
            f'{to / LOCAL_MODDB_CACHE_DIR}/), or copy the archive and matching JSON sidecar into '
            'that folder.'
        )
