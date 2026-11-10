"""Explicit residual100--120s range contract, not an entry certificate.

The helper checks only typed metadata, current source/range and its relationship
to a claimed accepted ENTRY proof. Final assembly MUST separately replay the
actual current ENTRY (including current owner marks) and outgoing proof. It
does not change selectors, acoustic thresholds, word guards, or default100s.
"""
import math
import re

VERSION = 1
KIND = 'reviewed-entry-residual-100to120'
MAXIMUM_SECONDS = 120.
MINIMUM_SOURCE_START_FRACTION = .25
ENTRY_METHODS = frozenset({'owner-approved','manual-range-start','physical-source-start',
    'vocal-free-entry-breath','vocal-free-entry-with-measured-preroll',
    'vocal-breath-before-new-word','low-voice-corroborated-entry',
    'relative-stereo-valley-entry','corroborated-instrumental-entry'})


def _finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def declared(item):
    return any(key.startswith('durationException') for key in item)


def _expected(item,source):
    row=item.get('row');proof=item.get('entryProof')
    if not isinstance(source,dict) or not isinstance(row,dict) or not isinstance(proof,dict):
        raise ValueError('duration-exception-shape')
    aid=source.get('id');md5=source.get('md5');size=source.get('bytes')
    duration=source.get('duration');lo=source.get('lo');hi=source.get('hi')
    if (not isinstance(aid,str) or not re.fullmatch('[a-f0-9]{24}',aid) or
            not isinstance(md5,str) or not re.fullmatch('[a-f0-9]{32}',md5) or
            type(size) is not int or not 0<size<=40*1024**2 or
            not all(_finite(v) for v in (duration,lo,hi)) or
            not 0<=lo<hi<=duration+.0001 or not 0<duration<=1800):
        raise ValueError('duration-exception-current-source')
    start=row.get('start');end=row.get('end');method=proof.get('method')
    if (item.get('trackId')!=aid or row.get('trackId')!=aid or item.get('md5')!=md5 or
            row.get('md5')!=md5 or proof.get('sourceMD5')!=md5 or
            type(item.get('entryReviewVersion')) is not int or item['entryReviewVersion']!=3 or
            proof.get('accepted') is not True or method not in ENTRY_METHODS or
            not _finite(start) or not _finite(end) or proof.get('sourceStart')!=start):
        raise ValueError('duration-exception-matching-entry-required')
    if (not lo<=start<end<=hi+.0001 or not 100<end-start<=MAXIMUM_SECONDS or
            start<duration*MINIMUM_SOURCE_START_FRACTION):
        raise ValueError('duration-exception-range-or-prefix')
    return {'version':VERSION,'kind':KIND,'maximumSeconds':MAXIMUM_SECONDS,
        'minimumSourceStartFraction':MINIMUM_SOURCE_START_FRACTION,
        'sourceId':aid,'sourceMD5':md5,'sourceBytes':size,'sourceDuration':duration,
        'allowedStart':lo,'allowedEnd':hi,'sourceStart':start,'sourceEnd':end,'entryMethod':method}


def make_exception(item,source):
    """Return metadata only, without modifying/promoting the supplied item."""
    if declared(item):raise ValueError('duration-exception-already-declared')
    return _expected(item,source)


def maximum_span(item,source):
    """Unmarked is exactly100; marked must match the one fixed literal policy."""
    if not declared(item):return 100.
    if {key for key in item if key.startswith('durationException')}!={'durationException'}:
        raise ValueError('duration-exception-unknown-marker')
    value=item.get('durationException')
    if (not isinstance(value,dict) or type(value.get('version')) is not int or
            value['version']!=VERSION or type(value.get('sourceBytes')) is not int):
        raise ValueError('duration-exception-version-or-shape')
    expected=_expected(item,source)
    if set(value)!=set(expected) or value!=expected:
        raise ValueError('duration-exception-metadata-changed')
    # Policy fields cannot exploit Python's equal bool/numeric values.
    for key in ('maximumSeconds','minimumSourceStartFraction','sourceDuration',
                'allowedStart','allowedEnd','sourceStart','sourceEnd'):
        if not _finite(value[key]):raise ValueError('duration-exception-invalid-number')
    return MAXIMUM_SECONDS
