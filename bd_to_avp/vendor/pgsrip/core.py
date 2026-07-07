import logging
import os
import typing

from bd_to_avp.vendor.pgsrip.media import Media, Pgs
from bd_to_avp.vendor.pgsrip.mkv import Mkv
from bd_to_avp.vendor.pgsrip.options import Options
from bd_to_avp.vendor.pgsrip.ripper import PgsToSrtRipper
from bd_to_avp.vendor.pgsrip.sup import Sup


logger = logging.getLogger(__name__)

MEDIAS: typing.Dict[str, typing.Union[typing.Type[Sup], typing.Type[Mkv]]] = {
    '.sup': Sup,
    '.mkv': Mkv,
    '.mks': Mkv
}
EXTENSIONS = tuple(MEDIAS.keys())


def scan_path(path: str,
              collected: typing.List[Media],
              filtered_out: typing.List[str],
              discarded: typing.List[str],
              options: Options):
    if not os.path.exists(path):
        logger.debug('Non existent path %s discarded', path)
        discarded.append(path)

    elif os.path.isfile(path):
        if path.lower().endswith(EXTENSIONS):
            if path.lower().endswith(EXTENSIONS):
                # noinspection PyBroadException
                try:
                    ext = os.path.splitext(path.lower())[1]
                    media = MEDIAS[ext](path)
                    if media.matches(options):
                        collected.append(media)
                    else:
                        filtered_out.append(path)
                except Exception as exc:
                    logger.debug('Path %s discarded: <%s> %s', path, type(exc).__name__, exc)
                    discarded.append(path)

    elif os.path.isdir(path):
        for dir_path, dir_names, file_names in os.walk(path):
            for filename in file_names:
                file_path = os.path.join(dir_path, filename)
                scan_path(file_path, collected, filtered_out, discarded, options)


def rip(media: Media, options: Options):
    counter = 0
    for pgs in media.get_pgs_medias(options):
        counter += rip_pgs(pgs, options)

    return counter


def rip_pgs(pgs: Pgs, options: Options):
    # noinspection PyBroadException
    try:
        with pgs as p:
            if not p.matches(options):
                return False

            rules = options.config.select_rules(tags=options.tags, languages={p.language})
            srt = PgsToSrtRipper(p, options).rip(lambda t: rules.apply(t, '')[0])
            srt.save(encoding=options.encoding)
            return True
    except Exception as e:
        logger.warning('Error while trying to rip %s: <%s> [%s]',
                       pgs.media_path, type(e).__name__, e,
                       exc_info=logger.isEnabledFor(logging.DEBUG))

    return False
