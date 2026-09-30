import pytest

from riscosconv.riscos_path import (
    MAX_LEAFNAME_LEN, PureRiscOsPath, as_ro_path,
)


P = PureRiscOsPath


# --- parsing and display ---

@pytest.mark.parametrize('path_str', [
    '$.Apps.Report',
    '$',
    '$.Apps',
    'Apps.Report',
    'ReadMe',
    '^.Shared.Config',
    '^.^',
    '@.Apps',
    '!RunImage',
])
def test_str_round_trip(path_str):
    assert str(P(path_str)) == path_str


def test_parts():
    assert P('$.Apps.Report').parts == ('$', 'Apps', 'Report')
    assert P('$').parts == ('$',)
    assert P('Apps.Report').parts == ('Apps', 'Report')
    assert P('!RunImage').parts == ('!RunImage',)
    assert P('').parts == ()


def test_name():
    assert P('$.Apps.Report').name == 'Report'
    assert P('!RunImage,ff8').name == '!RunImage,ff8'
    assert P('$').name == ''
    assert P('Apps').name == 'Apps'


def test_parent():
    assert P('$.Apps.Report').parent == P('$.Apps')
    assert P('$.Apps').parent == P('$')
    assert P('$').parent == P('$')
    assert P('Apps.Report').parent == P('Apps')
    assert P('!RunImage').parent == P('')


def test_absolute():
    assert P('$.Apps.Report').is_absolute()
    assert P('$').is_absolute()
    assert not P('Apps.Report').is_absolute()
    assert not P('!RunImage').is_absolute()
    assert not P('^.Apps').is_absolute()


def test_empty_path():
    assert str(P('')) == '.'
    assert P('').parts == ()
    # pathlib renders the empty path as '.'
    assert str(P('') / 'Apps') == 'Apps'


def test_dot_is_separator_not_curdir():
    # '.' is purely a separator; empty components are dropped like '/' in pathlib
    assert P('$.Apps..Report') == P('$.Apps.Report')
    assert P('$.Apps.').parts == ('$', 'Apps')


def test_reserved_dir_symbols_are_literal_components():
    # unlike pathlib on posix, '@' and '^' are ordinary components here;
    # '^' (parent) is kept literal like pathlib keeps '..'
    assert P('^.Shared.Config').parts == ('^', 'Shared', 'Config')
    assert P('@.Apps').parts == ('@', 'Apps')


# --- case handling ---

def test_case_insensitive_equality():
    assert P('$.Apps.Report') == P('$.apps.report')
    assert P('!RunImage') == P('!runimage')
    assert hash(P('$.Apps')) == hash(P('$.apps'))


def test_case_insensitive_containment():
    files = {P('!App.!Run'): 'meta', P('ReadMe'): 'meta2'}
    assert P('!app.!run') in files
    assert files[P('README')] == 'meta2'


def test_case_preserving_display():
    assert str(P('$.Apps.Report')) == '$.Apps.Report'
    assert str(P('$.apps.report')) == '$.apps.report'


# --- joining ---

def test_joinpath():
    assert P('$.Apps') / 'Report' == P('$.Apps.Report')
    assert P('Apps') / 'Report' == P('Apps.Report')
    assert P('') / 'Report' == P('Report')


def test_joinpath_with_absolute_replaces():
    assert (P('Apps.Report') / P('$.Other')).is_absolute()
    assert P('Apps.Report') / '$.Other' == P('$.Other')


# --- zipname conversion ---

@pytest.mark.parametrize('zipname, ro_path', [
    ('!App/!RunImage', '!App.!RunImage'),
    ('!WorkTop/!Boot', '!WorkTop.!Boot'),
    ('ReadMe', 'ReadMe'),
    ('!App/Resources/UK/Messages', '!App.Resources.UK.Messages'),
])
def test_from_zipname(zipname, ro_path):
    assert P.from_zipname(zipname) == P(ro_path)
    # zip entry names are always relative to the archive root
    assert not P.from_zipname(zipname).is_absolute()


@pytest.mark.parametrize('path_str, zipname', [
    ('$.!App.!RunImage', '!App/!RunImage'),
    ('Apps.Report', 'Apps/Report'),
    ('!RunImage', '!RunImage'),
])
def test_as_zipname(path_str, zipname):
    assert P(path_str).as_zipname() == zipname


def test_zipname_round_trip():
    # '$' root is stripped, so round trips stay relative
    p = P('$.!App.!RunImage,ff8')
    assert P.from_zipname(p.as_zipname()) == P('!App.!RunImage,ff8')


def test_root_zipname_is_empty():
    assert P('$').as_zipname() == ''


# --- leafname validation ---

@pytest.mark.parametrize('name', ['ReadMe', '!Run', 'a', 'App$Dir', 'x@y', 'x' * 255])
def test_valid_leafname(name):
    assert P(name).is_valid_leafname()


@pytest.mark.parametrize('path_str, why', [
    ('a*b', 'wildcard'),
    ('a#b', 'single-char wildcard'),
    ('x."y', 'quote'),
    ('a\nb', 'control char'),
    ('a\x7fb', 'control char'),
    ('x' * 256, 'too long'),
])
def test_invalid_leafname(path_str, why):
    assert not P(path_str).is_valid_leafname()


def test_dots_always_split():
    # '.' can never appear inside a leafname; parsing always splits on it
    assert P('a.b.c').parts == ('a', 'b', 'c')
    assert P('a.b.c').name == 'c'


def test_max_leafname_len():
    assert MAX_LEAFNAME_LEN == 255
    assert not P('x' * MAX_LEAFNAME_LEN + 'y').is_valid_leafname()


# --- unsupported syntax ---

@pytest.mark.parametrize('path_str', [
    'ADFS::HardDisc4.$.Work',
    'ADFS:$.Work',
    'RAM::RamDisc.$.temp',
    'Run:leafname',          # path variable lookup
    'Net#Server::Disc.$.x',  # special field
])
def test_filing_system_prefixes_rejected(path_str):
    with pytest.raises(ValueError, match='not supported'):
        P(path_str)


def test_hash_special_field_rejected():
    with pytest.raises(ValueError, match='not supported'):
        P('ADFS::0.$.x')


# --- coercion helper ---

def test_as_ro_path():
    p = P('$.Apps')
    assert as_ro_path(p) is p
    assert as_ro_path('$.Apps') == p
    assert isinstance(as_ro_path('ReadMe'), PureRiscOsPath)
