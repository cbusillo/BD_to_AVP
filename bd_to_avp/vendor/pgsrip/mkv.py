import json
import logging
import os
import typing
from subprocess import check_output

from babelfish import Language

from trakit.api import trakit

from bd_to_avp.vendor.pgsrip.media import Media, Pgs
from bd_to_avp.vendor.pgsrip.media_path import MediaPath
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.modules.config import config

logger = logging.getLogger(__name__)


class MkvPgs(Pgs):

    @classmethod
    def read_data(cls, media_path: MediaPath, track_id: int, temp_folder: str):
        lang_ext = f'.{media_path.language!s}' if media_path.language else ''
        sup_file = os.path.join(temp_folder, f'{track_id}{lang_ext}.sup')
        cmd = [config.MKVEXTRACT_PATH, str(media_path), 'tracks', f'{track_id}:{sup_file}']
        check_output(cmd)
        with open(sup_file, mode='rb') as f:
            return f.read()

    def __init__(self, media_path: MediaPath, track_id: int, language: Language, number: int, options: Options):
        temp_folder = media_path.create_temp_folder()
        super().__init__(media_path=media_path.translate(language=language, number=number),
                         options=options,
                         data_reader=lambda: self.read_data(
                             media_path=media_path, track_id=track_id, temp_folder=temp_folder),
                         temp_folder=temp_folder)
        self.track_id = track_id

    def __str__(self):
        return (f'{self.media_path.translate(language=Language("und"), number=0)} '
                f'[{self.track_id}:{self.media_path.language}]')


class MkvTrack:

    def __init__(self, track: dict):
        self.id = track['id']
        self.type = track['type']
        self.codec = track['codec']
        self.properties = track.get('properties', {})

    @property
    def enabled(self):
        return self.properties.get('enabled_track')

    @property
    def language(self):
        lang_ietf = self.properties.get('language_ietf')
        lang_alpha = self.properties.get('language')
        track_name = self.properties.get('track_name')

        language = Language.fromcleanit(lang_ietf or lang_alpha or 'und')
        options = {'expected_language': language} if language else {}
        guess = trakit(track_name, options) if track_name else {}

        return guess.get('language') or language

    @property
    def forced(self):
        return bool(self.properties.get('forced_track', False))

    def __repr__(self):
        return f'<{self.__class__.__name__} [{self!s}]>'

    def __str__(self):
        return f'{self.id}:{self.type}:{self.codec}:{self.language}:{self.enabled}:{self.forced}'


class Mkv(Media):

    def __init__(self, path: str):
        metadata = json.loads(check_output([config.MKVMERGE_PATH, '-i', '-F', 'json', path]))
        tracks = [MkvTrack(t) for t in metadata.get('tracks', [])]
        super().__init__(MediaPath(path), languages={t.language for t in tracks})
        self.tracks = tracks

    def get_pgs_medias(self, options: Options):
        tracks = [t for t in self.tracks
                  if t.type == 'subtitles' and t.codec == 'HDMV PGS' and t.enabled]
        tracks.sort(key=lambda x: (x.forced, x.id))
        selected_languages: typing.Dict[Language, int] = {}
        for t in tracks:
            language = t.language
            if options.languages and language not in options.languages:
                logger.debug('Filtering out track %s:%s in %s', t.id, language, self)
                continue

            if options.one_per_lang and language in selected_languages:
                logger.debug('Skipping track %s:%s in %s', t.id, language, self)
                continue

            if not language:
                logger.debug('Skipping unknown language track %s in %s', t.id, self)
                continue

            pgs = MkvPgs(self.media_path, t.id, language, selected_languages.get(language, 0), options=options)
            if pgs.matches(options):
                logger.debug('Selecting track %s:%s in %s', t.id, language, self)
                yield pgs
                selected_languages[language] = selected_languages.get(language, 0) + 1
