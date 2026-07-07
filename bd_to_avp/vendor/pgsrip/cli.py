import logging
import os
import re
import typing
from datetime import timedelta
from types import TracebackType

from babelfish import Error as BabelfishError, Language

import click

import pytesseract as tess

from bd_to_avp.vendor.pgsrip import Pgs, __version__, api
from bd_to_avp.vendor.pgsrip.media import Media
from bd_to_avp.vendor.pgsrip.options import Options

logger = logging.getLogger('pgsrip')


T = typing.TypeVar('T')


class DebugProgressBar(typing.Generic[T]):

    def __init__(self, debug: bool, iterable: typing.Iterable[T], **kwargs):
        self.debug = debug
        self.iterable = iterable
        self.progressbar = click.progressbar(iterable, **kwargs)

    def __iter__(self):
        if not self.debug:
            return self.progressbar.__iter__()

        yield from self.iterable

    def __enter__(self):
        if not self.debug:
            return self.progressbar.__enter__()

        return self

    def __exit__(self,
                 exc_type: typing.Optional[typing.Type[BaseException]],
                 exc: typing.Optional[BaseException],
                 traceback: typing.Optional[TracebackType]):
        if not self.debug:
            return self.progressbar.__exit__(exc_type, exc, traceback)

    def update(self, n_steps: int, current_item: typing.Optional[T] = None) -> None:
        if not self.debug:
            return self.progressbar.update(n_steps, current_item)


class LanguageParamType(click.ParamType):
    name = 'language'

    def convert(self, value, param, ctx):
        try:
            return Language.fromietf(value)
        except (BabelfishError, ValueError):
            self.fail(f"{click.style(f'{value}', bold=True)} is not a valid language")


class AgeParamType(click.ParamType):
    name = 'age'

    def convert(self, value, param, ctx):
        match = re.match(r'^(?:(?P<weeks>\d+?)w)?(?:(?P<days>\d+?)d)?(?:(?P<hours>\d+?)h)?$', value)
        if not match:
            self.fail('%s is not a valid age' % value)

        return timedelta(**{k: int(v) for k, v in match.groupdict('0').items()})


LANGUAGE = LanguageParamType()
AGE = AgeParamType()


@click.command()
@click.option('-c', '--config', type=click.Path(), help='cleanit configuration path to be used')
@click.option('-l', '--language', type=LANGUAGE, multiple=True, help='Language as IETF code, '
              'e.g. en, pt-BR (can be used multiple times).')
@click.option('-t', '--tag', required=False, multiple=True, help='Rule tags to be used, '
              'e.g. ocr, tidy, no-sdh, no-style, no-lyrics, no-spam (can be used multiple times). ')
@click.option('-e', '--encoding', help='Save subtitles using the following encoding.')
@click.option('-a', '--age', type=AGE, help='Filter videos newer than AGE, e.g. 12h, 1w2d.')
@click.option('-A', '--srt-age', type=AGE, help='Filter videos which srt subtitles are newer than AGE, e.g. 12h, 1w2d.')
@click.option('-f', '--force', is_flag=True, default=False,
              help='re-rip and overwrite existing srt subtitles, even if they already exist')
@click.option('--all', is_flag=True, default=False,
              help='rip all tracks for a given language, even another track for that language was already ripped')
@click.option('-w', '--max-workers', type=click.IntRange(1, 50), default=None, help='Maximum number of threads to use.')
@click.option('--keep-temp-files', is_flag=True, help='Do not delete temporary files created, '
                                                      'e.g. extracted sup files, generated png files '
                                                      'and other useful debug files')
@click.option('--debug', is_flag=True, help='Print useful information for debugging and for reporting bugs.')
@click.option('-v', '--verbose', count=True, help='Display debug messages')
@click.argument('path', type=click.Path(), required=True, nargs=-1)
@click.version_option(__version__)
def pgsrip(config: typing.Optional[str],
           language: typing.Optional[typing.Tuple[Language]],
           tag: typing.Optional[typing.Tuple[str]],
           encoding: typing.Optional[str],
           age: typing.Optional[timedelta],
           srt_age: typing.Optional[timedelta],
           force: bool,
           all: bool,
           debug: bool,
           max_workers: typing.Optional[int],
           keep_temp_files: bool,
           verbose: int,
           path: typing.Tuple[str]):
    if debug:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(logging.BASIC_FORMAT))
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.info(f'Tesseract version: {tess.get_tesseract_version()}')
        logger.info(f'Tesseract data: {os.getenv("TESSDATA_PREFIX")}')

    if config and (not os.path.isfile(config) or os.path.isdir(config)):
        click.echo(f"Invalid configuration is defined: {click.style(config, bold=True)}")
        return

    options = Options(config_path=config,
                      languages=set(language or []),
                      tags=set(tag or []),
                      encoding=encoding,
                      overwrite=force,
                      one_per_lang=not all,
                      keep_temp_files=keep_temp_files,
                      max_workers=max_workers,
                      age=age,
                      srt_age=srt_age)

    rules = options.config.select_rules(tags=options.tags, languages=options.languages)
    if not rules:
        values = tuple(options.tags) + tuple(str(lang) for lang in options.languages)
        click.echo(f"No rules defined for {click.style(', '.join(values), bold=True)}")
        return

    collected_medias: typing.List[Media] = []
    filtered_out_paths: typing.List[str] = []
    discarded_paths: typing.List[str] = []
    for p in path:
        c, f, d = api.scan_path(p, options)
        collected_medias.extend(c)
        filtered_out_paths.extend(f)
        discarded_paths.extend(d)

    if debug or verbose > 1:
        if verbose > 2:
            for p in filtered_out_paths:
                click.echo(f"{click.style(p, fg='yellow', bold=True)} filtered out")
        for p in discarded_paths:
            click.echo(f"{click.style(p, fg='red', bold=True)} discarded")

    collected_pgs_medias: typing.List[Pgs] = []
    medias_progressbar = DebugProgressBar(debug or verbose > 1,
                                          collected_medias,
                                          label='Collecting pgs subtitles',
                                          item_show_func=lambda item: str(item or ''))

    with medias_progressbar as bar:
        for m in bar:
            collected_pgs_medias.extend(list(m.get_pgs_medias(options)))

    # report collected medias
    report = (f"{click.style(str(len(collected_pgs_medias)), bold=True, fg='green')} "
              f"PGS subtitle{'s' if len(collected_pgs_medias) > 1 else ''} collected "
              f"from {click.style(str(len(collected_medias)), bold=True, fg='green')} "
              f"file{'s' if len(collected_medias) > 1 else ''}")
    if filtered_out_paths:
        report += (f" / {click.style(str(len(filtered_out_paths)), bold=True, fg='yellow')} "
                   f"file{'s' if len(filtered_out_paths) > 1 else ''} filtered out")
    if discarded_paths:
        report += (f" / {click.style(str(len(discarded_paths)), bold=True, fg='red')} "
                   f"path{'s' if len(discarded_paths) > 1 else ''} ignored")
    click.echo(report)

    pgs_progressbar = DebugProgressBar(debug or verbose > 1,
                                       collected_pgs_medias,
                                       label='Ripping subtitles',
                                       update_min_steps=0,
                                       item_show_func=lambda s: click.style(str(s or ''), bold=True))

    ripped_count = 0
    with pgs_progressbar as bar:
        for pgs in bar:
            bar.update(0, pgs)
            ripped_count += api.rip_pgs(pgs, options)

    # report ripped subtitles
    click.echo(f"{click.style(str(ripped_count), bold=True, fg='green')} "
               f"PGS subtitle{'s' if ripped_count > 1 else ''} ripped from "
               f"{click.style(str(len(collected_medias)), bold=True, fg='blue')} "
               f"file{'s' if len(collected_medias) > 1 else ''}")
