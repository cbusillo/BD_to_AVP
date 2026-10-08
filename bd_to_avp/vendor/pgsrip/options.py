import typing
import json
import os
import re
import stat
from datetime import timedelta

from babelfish import Error as BabelfishError, Language

from cleanit import Config
from cleanit import config as cleanit_config
from cleanit.rule import Rule
from cleanit.utils import ensure_list, validate
from jsonschema import ValidationError
from yaml import YAMLError, safe_load


class CustomConfigurationError(Exception):
    """A failure attributable to the explicitly requested cleanit file."""


def validate_mapping_keys(data: typing.Any) -> None:
    # YAML scalars remain non-string keys; jsonschema patternProperties assumes
    # JSON object keys. Check before entering that string-only boundary.
    pending = [data]
    seen = set()
    while pending:
        value = pending.pop()
        if not isinstance(value, (dict, list)) or id(value) in seen:
            continue
        seen.add(id(value))
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise CustomConfigurationError(f'Mapping keys must be strings; found {key!r}')
                pending.append(child)
        else:
            pending.extend(value)


def validate_replacement(rule: Rule) -> None:
    if rule.replacement is not None:
        for regex in rule.regexes:
            # sub parses group references even when the input does not match.
            # Do not call Rule.apply: it recursively cleans actual subtitle text.
            regex.sub(rule.replacement, '')


def load_custom_config(path: str) -> Config:
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise CustomConfigurationError('Configuration must be a regular file')
        # Load this file directly: Config.from_path silently ignores vanished
        # files and non-regular paths, then constructs default rules instead.
        with open(path) as source:
            data = json.load(source) if path.endswith('.json') else safe_load(source.read())
        validate_mapping_keys(data)
        validate(data)
    except (CustomConfigurationError, OSError, UnicodeDecodeError, json.JSONDecodeError, YAMLError, ValidationError) as error:
        detail = error.message if isinstance(error, ValidationError) else str(error)
        raise CustomConfigurationError(f'{path}: {detail}') from error

    defaults = cleanit_config.default_config
    merged = cleanit_config.merge_options(defaults, data)
    aliases = merged.get('aliases', {})
    for name, rule in merged.get('rules', {}).items():
        try:
            if 'patterns' not in rule:
                raise CustomConfigurationError('Missing patterns')
            if 'locale' in ensure_list(rule.get('flags')):
                raise CustomConfigurationError('The locale flag cannot be used with text patterns')
            for language in ensure_list(rule.get('languages')):
                try:
                    Language.fromietf(language)
                except (BabelfishError, ValueError) as error:
                    raise CustomConfigurationError(f'Invalid language {language!r}') from error
            constructed = Rule(name=name, aliases=aliases, **rule)
            check_replacement = name in data.get('rules', {})
            if not check_replacement and aliases != defaults.get('aliases', {}):
                try:
                    inherited = Rule(name=name, aliases=defaults.get('aliases', {}), **rule)
                except (re.error, OverflowError, RecursionError):
                    # A custom alias may repair an originally invalid pattern.
                    check_replacement = True
                else:
                    check_replacement = constructed.regexes != inherited.regexes
            if check_replacement:
                try:
                    validate_replacement(constructed)
                except (re.error, IndexError) as error:
                    raise CustomConfigurationError(f'Invalid replacement: {error}') from error
        except (CustomConfigurationError, re.error, OverflowError, RecursionError, BabelfishError) as error:
            original = defaults.get('rules', {}).get(name)
            if original is not None:
                # If this rule was already broken in the defaults, preserve
                # that exception rather than blaming the custom file.
                validate_replacement(Rule(name=name, aliases=defaults.get('aliases', {}), **original))
            raise CustomConfigurationError(f'{path}: Rule {name!r}: {error}') from error

    # Keep unexpected constructor errors outside the custom diagnostic boundary.
    return Config(data)


class Options:

    def __init__(self,
                 config_path: typing.Optional[str] = None,
                 languages: typing.Optional[typing.Set[Language]] = None,
                 tags: typing.Optional[typing.Set[str]] = None,
                 encoding: typing.Optional[str] = None,
                 overwrite=False,
                 one_per_lang=True,
                 keep_temp_files=False,
                 max_workers: typing.Optional[int] = None,
                 confidence: typing.Optional[int] = None,
                 ocr_width: typing.Optional[int] = None,
                 ocr_backend: typing.Optional[typing.Any] = None,
                 age: typing.Optional[timedelta] = None,
                 srt_age: typing.Optional[timedelta] = None):
        self.config = load_custom_config(config_path) if config_path is not None else Config()
        self.languages = languages or set()
        self.tags = tags or {'default'}
        self.encoding = encoding
        self.overwrite = overwrite
        self.one_per_lang = one_per_lang
        self.keep_temp_files = keep_temp_files
        self.max_workers = max_workers
        self.confidence = confidence
        self.ocr_width = ocr_width
        self.ocr_backend = ocr_backend
        self.age = age
        self.srt_age = srt_age

    def __repr__(self):
        return f'<{self.__class__.__name__} [{self}]>'

    def __str__(self):
        return (f'languages:{self.languages}, '
                f'tags:{self.tags}, '
                f'encoding:{self.encoding}, '
                f'overwrite:{self.overwrite}, '
                f'one_per_lang:{self.one_per_lang}, '
                f'keep_temp_files:{self.keep_temp_files}, '
                f'max_workers:{self.max_workers}, '
                f'confidence:{self.confidence}, '
                f'ocr_width:{self.ocr_width}, '
                f'age:{self.age}, '
                f'srt_age:{self.srt_age}')
