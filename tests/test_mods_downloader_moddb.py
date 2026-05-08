import json

from pathlib import Path
from shutil import copy
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from requests.exceptions import HTTPError

from launcher.exceptions import ModDBDownloadError
from launcher.mods.downloader.moddb import LOCAL_MODDB_CACHE_DIR, ModDBDownloader

from common import basic_url2, data_dir, mocked_get, moddb_start_url, moddb_page_info, moddb_mirror_url


class ModDBDownloaderTestCase(TestCase):

    @patch('launcher.mods.downloader.moddb.g_session.get', side_effect=mocked_get)
    def test_get_download_url(self, mock_request) -> None:
        self.assertEqual(ModDBDownloader._get_download_url(moddb_start_url,), basic_url2)

        self.assertEqual(len(mock_request.call_args_list), 2)
        self.assertTrue(mock_request.call_args_list[0].called_with(moddb_start_url))
        self.assertTrue(mock_request.call_args_list[1].called_with(moddb_mirror_url))

    @patch('launcher.mods.downloader.moddb.g_session.get', side_effect=mocked_get)
    def test_parse_moddb_metadata(self, mock_request) -> None:
        o = ModDBDownloader._parse_moddb_metadata(moddb_page_info)

        self.assertEqual(o['Filename'], 'Anomaly-1.5.3-Full.2.7z')
        self.assertEqual(o['MD5 Hash'], 'd6bce51a4e6d98f9610ef0aa967ba964')
        self.assertEqual(o['Download'], 'https://www.moddb.com/downloads/start/277404')

        mock_request.assert_called_once_with(moddb_page_info)

    @patch('launcher.mods.downloader.moddb.g_session.get', side_effect=mocked_get)
    def test_download(self, mock_request) -> None:
        o = ModDBDownloader(moddb_start_url, moddb_page_info)

        # TODO: Better test cases (check cache, requests call, ...)
        with TemporaryDirectory(prefix='gamma-launcher-moddb-downloader-test-') as dir:
            pdir = Path(dir)

            o.download(pdir)
            self.assertTrue((pdir / 'Anomaly-1.5.3-Full.2.7z').exists())
            meta = pdir / LOCAL_MODDB_CACHE_DIR / '277404.json'
            self.assertTrue(meta.is_file())
            self.assertEqual(
                json.loads(meta.read_text(encoding='utf-8'))['filename'],
                'Anomaly-1.5.3-Full.2.7z',
            )

    def test_download_use_local_sidecar_when_moddb_unreachable(self) -> None:
        o = ModDBDownloader(moddb_start_url, moddb_page_info)

        with TemporaryDirectory(prefix='gamma-launcher-moddb-offline-test-') as dir:
            pdir = Path(dir)
            cache_dir = pdir / LOCAL_MODDB_CACHE_DIR
            cache_dir.mkdir(parents=True)
            archive_name = 'Anomaly-1.5.3-Full.2.7z'
            copy(data_dir / 'test.rar', pdir / archive_name)
            (cache_dir / '277404.json').write_text(
                json.dumps({'filename': archive_name, 'md5': None}),
                encoding='utf-8',
            )

            with patch(
                'launcher.mods.downloader.moddb.ModDBDownloader._parse_moddb_metadata',
                side_effect=HTTPError(response=None),
            ):
                path = o.download(pdir, use_cached=True)

            self.assertEqual(path.name, archive_name)
            self.assertTrue(path.is_file())

    def test_download_raises_when_offline_and_no_sidecar(self) -> None:
        o = ModDBDownloader(moddb_start_url, moddb_page_info)

        with TemporaryDirectory(prefix='gamma-launcher-moddb-offline-test-') as dir:
            pdir = Path(dir)

            with patch(
                'launcher.mods.downloader.moddb.ModDBDownloader._parse_moddb_metadata',
                side_effect=HTTPError(response=None),
            ):
                with self.assertRaises(ModDBDownloadError):
                    o.download(pdir, use_cached=True)
