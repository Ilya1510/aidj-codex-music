"""Pure approximate relative-stereo pause entry, after stronger incoming tiers.

A measured combined-stereo RMS dip agrees with retained all-confidence ASR and
measured vocal preroll. It neither proves a semantic boundary nor claims quiet
vocals/absence of singing. No model, selection, write or publication occurs.
"""
import bisect
import hashlib
import json
import math
from pathlib import Path

from .fixed_set_repair import covered
from .fixed_set_resolve import read_json
from .fixed_set_soft_entry import _source_valid, _observations_valid, _validated_vocal, source_signature, digest
from .fixed_set_vocal_review import finite, gain_db, quantile
from .vocal_probe import MODEL_SHA256

VERSION = 1
METHOD = 'relative-stereo-valley-entry'
RMS_KIND = 'canonical6s-original-stereo-channel-rms'
RMS_CHANNEL_POLICY = 'max-of-independent-left-and-right-rms'
NORMALIZATION = 'float32-playback-gain-multiply-before-double-channel-mean-squares'
SOURCE_FIELDS = ('id','md5','bytes','duration','lo','hi','manualStart','lufs','peak','loudnessMethod','loudnessStatus')
PARAMETERS = {
    'stereoVariant': 'combinedStereoRms', 'minimumStereoValleyDb': 3.,
    'bodyStartSeconds': -.15, 'bodyEndSeconds': .05, 'bodyStatistic': 'MAX',
    'leftStartSeconds': -1., 'leftEndSeconds': -.25,
    'rightStartSeconds': .35, 'rightEndSeconds': 1., 'neighborStatistic': 'P75',
    'rmsWindowSeconds': 6., 'rmsPointLeadSeconds': 3., 'rmsFrameSamples': 441,
    'rmsSampleRate': 44100, 'rmsWindowPolicy': 'point-centered6s-shift-at-source-edges-no-context-selection',
    'asrCrossingMarginSeconds': .08, 'asrCoverageBeforeSeconds': .3,
    'asrCoverageAfterSeconds': .35, 'futureObservedWordRequired': True,
    'futureWordConfidenceRequired': False, 'minimumObservedPrerollSeconds': .1,
    'maximumFutureWordDelaySeconds': None, 'vocalContextGuardSeconds': .25,
    'preRiseStartSeconds': -.15, 'preRiseEndSeconds': -.02,
    'riseSearchStartSeconds': -.02, 'riseSearchEndSeconds': .35, 'relativeRiseDb': 8.,
    'riseObservationRequired': False, 'maximumFadeInSeconds': .05,
    'minimumFadeInSeconds': .005, 'finishBeforeWordOrRiseSeconds': .03,
    'negativeOwnerRadiusSeconds': .35, 'absoluteVocalOrMixCeilingApplied': False,
    'VADCapApplied': False, 'MAXChannelVariantUsed': False,
}


def centered_rms_bounds(point, duration):
    if not finite(point) or not finite(duration) or not 0 <= point < duration <= 1800:
        raise ValueError('relative-rms-invalid-point-duration')
    begin = max(0., min(point-3., duration-6.))
    return round(begin*44100)/44100, round(min(duration, begin+6.)*44100)/44100


def rms_identity(song, point):
    begin, end = centered_rms_bounds(point, song['duration'])
    return {'version':1, 'kind':RMS_KIND, 'source':{k:song.get(k) for k in SOURCE_FIELDS},
        'sourceOffsetSeconds':begin, 'requestedEndSeconds':end,
        'playbackGainDb':gain_db(song), 'sampleRate':44100, 'channels':2,
        'hopSamples':441, 'channelPolicy':RMS_CHANNEL_POLICY, 'normalizationPolicy':NORMALIZATION}


def _hash64(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def validate_rms(raw, song, point):
    """Replay original-source geometry; combined RMS is derived from both L/R."""
    try:
        pin = rms_identity(song,point)
        if (not isinstance(raw,dict) or type(raw.get('version')) is not int or raw['version']!=1 or
                raw.get('kind')!=RMS_KIND or raw.get('identity')!=pin or raw.get('cacheKey')!=digest(pin)):
            return None
        if (raw.get('sourceOriginalMD5Verified') is not True or raw.get('audioRetained') is not False or
                any(raw.get(k) is not False for k in ('UVRInferred','ASRInferred','SileroInferred',
                    'channelsAveragedBeforeRMS','zeroPaddingApplied')) or
                raw.get('inputHashProvenance')!='native-recorded-source-and-normalized-interleaved-float32-PCM-SHA256' or
                not all(_hash64(raw.get(k)) for k in ('sourcePCMInputSHA256','normalizedInputSHA256'))):
            return None
        decode=raw['decode']; stats=raw['rms']
        if not isinstance(decode,dict) or not isinstance(stats,dict):return None
        n=decode['pcmFrames']; begin=pin['sourceOffsetSeconds']; end=pin['requestedEndSeconds']
        if (type(n) is not int or not 0<n<=6*44100 or decode.get('pcmBytes')!=n*8 or
                decode.get('sampleRate')!=44100 or decode.get('channels')!=2 or
                decode.get('method')!='ffmpeg-xerror-canonical6s-stereo-f32le' or
                decode.get('sourceOffsetSeconds')!=begin or decode.get('requestedEndSeconds')!=end or
                decode.get('actualEndSeconds')!=begin+n/44100 or abs(decode['actualEndSeconds']-end)>.05):
            return None
        names=('frameStartSeconds','frameEndSeconds','leftRms','rightRms','maxChannelRms','combinedStereoRms')
        count=(n+440)//441
        if (stats.get('method')!='independent-stereo-channel-441-sample-RMS' or
                stats.get('channelPolicy')!=RMS_CHANNEL_POLICY or stats.get('normalizationPolicy')!=NORMALIZATION or
                stats.get('combinedStereoDefinition')!='sqrt((meanL2+meanR2)/2)-owner-compatible-diagnostic-not-primary-MAX-channel' or
                stats.get('frameHopSeconds')!=.01 or stats.get('hopSamples')!=441 or
                stats.get('lastFramePartial') is not (n%441!=0) or
                any(not isinstance(stats.get(k),list) or len(stats[k])!=count for k in names)):
            return None
        for i,(a,b,left,right,maximum,combined) in enumerate(zip(*(stats[k] for k in names))):
            if (not all(finite(x) for x in (a,b,left,right,maximum,combined)) or min(left,right,maximum,combined)<0 or
                    abs(a-(begin+i*.01))>1e-8 or abs(b-min(begin+(i+1)*.01,decode['actualEndSeconds']))>1e-8 or
                    abs(maximum-max(left,right))>1e-8 or abs(combined-math.sqrt((left*left+right*right)/2))>1e-8):
                return None
        return stats
    except (ValueError,TypeError,KeyError,IndexError,OverflowError):
        return None


def _rms_region(stats,a,b):
    first=bisect.bisect_right(stats['frameEndSeconds'],a+1e-8)
    last=bisect.bisect_left(stats['frameStartSeconds'],b-1e-8)
    if (first>=last or stats['frameStartSeconds'][first]>a+1e-8 or stats['frameEndSeconds'][last-1]<b-1e-8 or
            any(stats['frameEndSeconds'][i]<stats['frameStartSeconds'][i+1]-1e-8 for i in range(first,last-1))):
        return None
    return first,last


def check_entry(vocal,rms,point,song,words,coverage,*,rejected=(),fade_in=.05):
    """Exact combined-only recipe; owner annotations never exempt a word veto."""
    reject=lambda reason:{'accepted':False,'method':METHOD,'reason':reason}
    if (not _source_valid(song) or not finite(point) or not song['lo']<=point<song['hi'] or
            not finite(fade_in) or fade_in<.005):return reject('invalid-source-point-or-fade')
    if not isinstance(rejected,(list,tuple)) or any(not finite(t) for t in rejected):return reject('invalid-owner-rejections')
    if any(abs(point-t)<.35 for t in rejected):return reject('owner-rejected-entry')
    if not _observations_valid(words,coverage):return reject('invalid-retained-ASR')
    if not covered(coverage,point-.3,point+.35):return reject('ASR-coverage-unknown')
    if any(a<point+.08-1e-7 and b>point-.08+1e-7 for a,b,_ in words):return reject('known-word-crosses-entry-guard')
    future=sorted((list(w) for w in words if w[0]>=point-1e-7),key=lambda w:(w[0],w[1]))
    if not future:return reject('no-observed-future-word')
    next_word=future[0]; next_delay=next_word[0]-point
    if next_delay<.1-1e-7:return reject('observed-word-preroll-under100ms')
    same=[w for w in future if abs(w[0]-next_word[0])<1e-7]
    prepared=_validated_vocal(vocal,song)
    if prepared is None:return reject('invalid-raw-vocal-evidence')
    offset,hop,duration,values=prepared
    if point-.3<max(song['lo'],offset+.25)-1e-7 or point+.35>min(song['hi'],offset+duration-.25)+1e-7:
        return reject('incomplete-vocal-context')
    def region(a,b):
        first=math.floor((a-offset)/hop+1e-8);last=math.ceil((b-offset)/hop-1e-8)
        return (first,last) if 0<=first<last<=len(values[1]) and offset+first*hop<=a+1e-7 and offset+last*hop>=b-1e-7 else None
    before=region(point-.15,point-.02);search=region(point-.02,point+.35)
    if before is None or search is None:return reject('incomplete-vocal-frames')
    pre=quantile(values[1][before[0]:before[1]],.95);threshold=pre*10**.4
    rise=next((i for i in range(*search) if values[1][i]>=threshold),None)
    rise_time=offset+rise*hop if rise is not None else None;rise_delay=rise_time-point if rise_time is not None else None
    if rise_delay is not None and rise_delay<.1-1e-7:return reject('observed-voice-preroll-under100ms')
    cap=min(.05,next_delay-.03,rise_delay-.03 if rise_delay is not None else .05)
    fade=math.floor((min(fade_in,cap)+1e-9)*10000)/10000
    if fade<.005:return reject('fade-cannot-finish-before-word-or-rise')
    stats=validate_rms(rms,song,point)
    if stats is None:return reject('invalid-original-stereo-rms')
    spans=[_rms_region(stats,point+a,point+b) for a,b in ((-.15,.05),(-1.,-.25),(.35,1.))]
    if any(x is None for x in spans):return reject('incomplete-original-stereo-frames')
    body,left,right=[stats['combinedStereoRms'][a:b] for a,b in spans]
    db=lambda v:20*math.log10(max(1e-10,v))
    maximum=db(max(body));left75=db(quantile(left,.75));right75=db(quantile(right,.75));depth=min(left75,right75)-maximum
    if depth<3.-1e-7:return reject('relative-stereo-valley-below3dB')
    return {'accepted':True,'reason':'accepted','method':METHOD,'version':VERSION,'parameters':dict(PARAMETERS),
        'sourceMD5':song['md5'],'sourceStart':point,'sourceSignature':source_signature(song),
        'observationsHash':digest([words,coverage]),'negativeOwnerInputHash':digest(list(rejected)),
        'cacheKey':vocal['cacheKey'],'rmsCacheKey':rms['cacheKey'],
        'vocalEvidenceHash':digest(vocal),'rmsEvidenceHash':digest(rms),'uvrModelSha256':MODEL_SHA256,
        'vocalSourceOffsetSeconds':offset,'vocalSourceEndSeconds':offset+duration,
        'vocalPreFrameFirst':before[0],'vocalPreFrameLastExclusive':before[1],
        'vocalRiseFrameFirst':search[0],'vocalRiseFrameLastExclusive':search[1],
        'preVocalP95RMS':pre,'risingThresholdRMS':threshold,'risingVoiceFrame':rise,
        'risingVoiceTime':rise_time,'risingVoiceDelaySeconds':rise_delay,'riseObservedWithin350ms':rise is not None,
        'nextObservedWord':next_word,'nextObservedWordsAtFirstStart':same,
        'nextObservedWordConfident':all(w[2] for w in same),'nextObservedWordDelaySeconds':next_delay,
        'fadeInCapSeconds':cap,'fadeInSeconds':fade,'asrCoverageStart':point-.3,'asrCoverageEnd':point+.35,
        'stereoBodyStart':point-.15,'stereoBodyEnd':point+.05,
        'stereoLeftStart':point-1.,'stereoLeftEnd':point-.25,'stereoRightStart':point+.35,'stereoRightEnd':point+1.,
        'stereoBodyFrames':list(spans[0]),'stereoLeftFrames':list(spans[1]),'stereoRightFrames':list(spans[2]),
        'stereoBodyMaxDbfs':maximum,'stereoLeftP75Dbfs':left75,'stereoRightP75Dbfs':right75,'stereoValleyDb':depth,
        'rmsSourceOffsetSeconds':rms['identity']['sourceOffsetSeconds'],
        'rmsRequestedEndSeconds':rms['identity']['requestedEndSeconds'],
        'rmsActualEndSeconds':rms['decode']['actualEndSeconds'],'rmsPCMFrames':rms['decode']['pcmFrames'],
        'sourcePCMHashNativeRecorded':rms['sourcePCMInputSHA256'],'normalizedPCMHashNativeRecorded':rms['normalizedInputSHA256'],
        'rmsVariant':'combinedStereoRms','actualOriginalStereoRMS':True,'allConfidenceASRVeto':True,
        'asrCoverageComplete':True,'ASRWordsErased':False,'asrOverrideApplied':False,
        'absoluteVocalOrMixCeilingApplied':False,'VADCapApplied':False,'MAXChannelVariantUsed':False,
        'confidenceKind':'approximate-relative-stereo-pause-between-observed-words',
        'verifiedWordBoundary':False,'naturalSilenceClaimed':False,'voiceAbsenceProven':False}


def selection_error(item,song,*,owner=(),evidence_folder,rejected=None):
    """Reload the exact original-RMS and UVR pair; never choose another context."""
    try:
        from .fixed_set_entry_review import VERSION as ENTRY_VERSION,fingerprint
        if not isinstance(item,dict) or not _source_valid(song):return 'relative-entry-source-mismatch'
        row=item.get('row',{});proof=item.get('entryProof',{})
        if not isinstance(row,dict) or not isinstance(proof,dict):return 'relative-entry-input-changed'
        if (item.get('entryReviewVersion')!=ENTRY_VERSION or proof.get('method')!=METHOD or
                not proof.get('accepted') or item.get('entryReviewInputHash')!=fingerprint(item,song,owner)):
            return 'relative-entry-input-changed'
        if type(proof.get('version')) is not int or proof['version']!=VERSION or proof.get('parameters')!=PARAMETERS:
            return 'relative-entry-policy-version-or-parameters'
        if item.get('trackId')!=song['id'] or item.get('md5')!=song['md5'] or row.get('md5')!=song['md5'] or row.get('trackId')!=song['id']:
            return 'relative-entry-source-mismatch'
        folder=Path(evidence_folder)
        if rejected is None:
            mapping=read_json(folder.parent.parent/'dj1-curation'/'owner-rejected.json')
            if not isinstance(mapping,dict):return 'relative-entry-owner-rejections'
            rejected=mapping.get(item['trackId'],[])
        key=proof.get('cacheKey');rmskey=proof.get('rmsCacheKey')
        if not _hash64(key) or not _hash64(rmskey):return 'relative-entry-key'
        vocal_path=folder/(key+'.json');rms_path=folder.parent/'mix-rms-probes'/(rmskey+'.json')
        if not (0<vocal_path.stat().st_size<=2*1024*1024 and 0<rms_path.stat().st_size<=1024*1024):
            return 'relative-entry-evidence-size'
        vocal=read_json(vocal_path);rms=read_json(rms_path)
        check=check_entry(vocal,rms,row.get('start'),song,item.get('observedWords',[]),
            item.get('observedCoverage',[]),rejected=rejected,fade_in=row.get('fadeIn'))
        if not check['accepted']:return 'relative-entry-'+check['reason']
        if proof!=check:return 'relative-entry-proof-mismatch'
        span=row['end']-row['start']
        if (not 45<=span<=100 or row['fadeIn']!=check['fadeInSeconds'] or
                not song['lo']<=row['start']<row['end']<=song['hi']+.0001 or
                not finite(row.get('fadeOut')) or row['fadeOut']<0 or row['fadeIn']+row['fadeOut']>=span):
            return 'relative-entry-row'
        return None
    except (ValueError,TypeError,KeyError,OSError,RuntimeError,IndexError,OverflowError):
        return 'relative-entry-evidence-unavailable'
