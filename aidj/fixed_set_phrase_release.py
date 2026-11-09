"""OPTIONAL phrase-finish release research; no runner or publication integration.

A generated linear fade may begin under the end of an observed phrase, provided
that phrase finishes while gain is still at least.75 (preferred), or.65, and the
entire remaining tail is independently vocal-quiet. Instrumental sound may stay
loud: this is explicitly NOT a claim of a natural quiet boundary or verified word
alignment. Consider only after strict and hard review; this module enables none.
"""
import hashlib
import json
import math

from .fixed_set_repair import covered
from .fixed_set_vocal_review import _vectors, finite, gain_db, quantile
from .vocal_probe import MODEL_SHA256

VERSION = 1
METHOD = 'phrase-finish-generated-release'
PARAMETERS = {'fadeSeconds':(2.4,3.2,4.),'preferredVoiceFraction':.25,
              'secondaryVoiceFraction':.35,'wordEndMarginSeconds':.3,
              'fadeMarginSeconds':.2,'excerptGuardSeconds':.25,
              'terminalVocalMaxDbfs':-43.,'minimumTerminalSeconds':1.56,
              'frameHopSeconds':.01,'equalityEpsilonSeconds':1e-7}


def source_signature(source):
    """Persist current identity, manual range and level profile for future replay."""
    fields=('md5','duration','lo','hi','lufs','peak','loudnessMethod','loudnessStatus')
    payload={key:source.get(key) for key in fields}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _bounds(point):
    end=point.get('time',point.get('end'));fade=point.get('fade',point.get('fadeOut'))
    if not finite(end) or not finite(fade):return None
    end=round(end,4);fade=round(fade,4)
    if not any(abs(fade-choice)<1e-7 for choice in PARAMETERS['fadeSeconds']):return None
    start=round(end-fade,4)
    if 'fadeStart' in point and (not finite(point['fadeStart']) or abs(point['fadeStart']-start)>.00011):return None
    return end,fade,start


def _source_valid(source,vocal,end,start):
    digest=source.get('md5')
    if not isinstance(digest,str) or len(digest)!=32 or any(c not in '0123456789abcdef' for c in digest):return False
    if vocal.get('md5')!=digest:return False
    if any(not finite(source.get(key)) for key in ('duration','lo','hi')):return False
    if not 0<=source['lo']<source['hi']<=source['duration']+.001:return False
    if start<source['lo']-1e-7 or end>source['hi']+1e-7:return False
    if any(source.get(key) is not None and not finite(source[key]) for key in ('lufs','peak')):return False
    return True


def _numeric_observations(words,coverage):
    if not isinstance(words,(tuple,list)) or not isinstance(coverage,(tuple,list)):return False
    for rows in (words,coverage):
        for row in rows:
            if not isinstance(row,(tuple,list)) or len(row)<2:return False
            if not finite(row[0]) or not finite(row[1]) or not 0<=row[0]<=row[1]:return False
    return True


def check_release(vocal,point,words,coverage,source):
    """Pure validator. Never ignores a known word or trusts a frame percentile.

    vocal: pinned10ms numerical evidence, with md5/sourceOffsetSeconds.
    point: source end/time, fade/fadeOut, optional fadeStart.
    words/coverage: actual numeric ASR observations (all confidence values count).
    source: CURRENT verified MD5, duration, lo/hi and PlaybackLevel profile.
    """
    fail=lambda reason:{'accepted':False,'reason':reason,'method':METHOD,'enabled':False}
    bounds=_bounds(point)
    if bounds is None:return fail('unsupported-phrase-release-fade')
    end,fade,start=bounds
    if not _source_valid(source,vocal,end,start):return fail('phrase-release-source-mismatch')
    if not _numeric_observations(words,coverage):return fail('invalid-ASR-observations')
    prepared=_vectors(vocal)
    if prepared is None:return fail('invalid-vocal-evidence')
    offset,hop,duration,values=prepared
    a=round(start-PARAMETERS['fadeMarginSeconds'],4)
    b=round(end+PARAMETERS['fadeMarginSeconds'],4)
    guard=PARAMETERS['excerptGuardSeconds'];epsilon=PARAMETERS['equalityEpsilonSeconds']
    if a<offset+guard-epsilon or b>offset+duration-guard+epsilon:return fail('phrase-release-context-edge')
    if a<source['lo']-epsilon or b>source['hi']+epsilon:return fail('phrase-release-outside-manual-range')
    if not covered(coverage,a,b):return fail('unknown-ASR-coverage')
    first=max(0,math.floor((a-offset)/hop+1e-8));last=min(len(values[0]),math.ceil((b-offset)/hop-1e-8))
    if first>=last or offset+first*hop>a+epsilon or offset+last*hop<b-epsilon:return fail('incomplete-vocal-evidence')
    level=gain_db(source)
    normalized=[20*math.log10(max(1e-10,v))+level for v in values[1]]
    active=[i for i in range(first,last) if normalized[i]>PARAMETERS['terminalVocalMaxDbfs']]
    last_active_end=max((offset+(i+1)*hop for i in active),default=None)
    # Include known words that touch the measured outgoing window. Confidence
    # never excuses a late word; words crossing the endpoint veto this release.
    retained=[row for row in words if row[0]<b+epsilon and row[1]>a-epsilon]
    last_word=max((row[1] for row in retained),default=None)
    attempts=[]
    for fraction in (PARAMETERS['preferredVoiceFraction'],PARAMETERS['secondaryVoiceFraction']):
        quiet_start=start+fraction*fade
        if last_word is not None and last_word+PARAMETERS['wordEndMarginSeconds']>quiet_start+epsilon:
            attempts.append('late-ASR-word');continue
        if last_active_end is not None and last_active_end>quiet_start+epsilon:
            attempts.append('late-model-voice');continue
        terminal_first=max(first,math.floor((quiet_start-offset)/hop+1e-8))
        tail=normalized[terminal_first:last]
        # Every10ms frame touching the terminal interval must pass. One late
        # syllable is a veto even if99% of this interval is otherwise quiet.
        if not tail or max(tail)>PARAMETERS['terminalVocalMaxDbfs']:
            attempts.append('nonquiet-terminal-frame');continue
        required=fade*(1-fraction)
        if required<PARAMETERS['minimumTerminalSeconds']-epsilon:
            attempts.append('brief-terminal-release');continue
        mix=values[0][first:last];instrumental=values[2][first:last]
        return {'accepted':True,'reason':'accepted','method':METHOD,'enabled':False,
            'version':VERSION,'parameters':dict(PARAMETERS),'modelSha256':MODEL_SHA256,
            'md5':source['md5'],'sourceSignature':source_signature(source),
            'time':end,'fade':fade,'fadeStart':start,'proofStart':a,'proofEnd':b,
            'sourceOffsetSeconds':offset,'sourceEndSeconds':offset+duration,
            'frameFirst':first,'frameLastExclusive':last,'terminalFrameFirst':terminal_first,
            'frameHopSeconds':hop,'voiceFraction':fraction,
            'preference':'preferred' if fraction==.25 else 'secondary',
            'lastRetainedWordEnd':last_word,'lastAboveThresholdVocalFrameEnd':last_active_end,
            'lastWordGain':None if last_word is None else max(0.,min(1.,(end-last_word)/fade)),
            'lastActiveVocalGain':None if last_active_end is None else max(0.,min(1.,(end-last_active_end)/fade)),
            'minimumProtectedGain':1-fraction,'terminalStart':quiet_start,
            'terminalDurationBeforeEnd':required,'terminalDurationIncludingMargin':b-quiet_start,
            'normalizedTerminalVocalMaxDbfs':max(tail),
            'normalizedTerminalVocalP95Dbfs':quantile(tail,.95),
            'normalizedMixMaxDbfs':20*math.log10(max(1e-10,max(mix)))+level,
            'normalizedMixP95Dbfs':20*math.log10(max(1e-10,quantile(mix,.95)))+level,
            'instrumentalMaxRms':max(instrumental),'playbackGainDb':level,
            'asrCoverageComplete':True,'asrIgnored':False,'asrClearInTerminal':True,
            'naturalSilenceClaimed':False,'verifiedWordBoundary':False,
            'rampKind':'linear-generated-release','rampGainAtStart':.7,'rampGainAtEnd':0.,
            'incomingEntryVerifiedHere':False}
    return {**fail(attempts[-1] if attempts else 'no-protected-phrase-end'),'attempts':attempts}


def find_release_candidates(vocal,words,coverage,source,*,target_end=None,limit=12):
    """Pure source-coordinate proposals only; no pairing, order changes or jobs."""
    if type(limit) is not int or not 1<=limit<=32:return []
    if any(not finite(source.get(key)) for key in ('duration','lo','hi')):return []
    prepared=_vectors(vocal)
    if prepared is None:return []
    offset,hop,duration,_=prepared
    if target_end is None:target_end=min(source.get('hi',0),offset+duration-.45)
    if not finite(target_end):return []
    candidates=[]
    for fade in PARAMETERS['fadeSeconds']:
        lower=max(source.get('lo',0)+fade+.2,offset+.25+fade+.2)
        upper=min(source.get('hi',0)-.2,offset+duration-.45)
        for tick in range(math.ceil(lower*10-1e-8),math.floor(upper*10+1e-8)+1):
            end=round(tick/10,4)
            point={'time':end,'fade':fade,'fadeStart':round(end-fade,4)}
            check=check_release(vocal,point,words,coverage,source)
            if not check['accepted']:continue
            score=(0 if check['preference']=='preferred' else 10)+abs(end-target_end)*.1+(4-fade)*.05
            candidates.append({'score':score,'point':point,'releaseCheck':check})
    return sorted(candidates,key=lambda row:row['score'])[:limit]


def replay_release(vocal,point,words,coverage,current_source,stored_check):
    """Recompute from raw vectors/current gain; stale source/range/profile fails."""
    try:signature=source_signature(current_source)
    except (TypeError,ValueError):return {'accepted':False,'reason':'invalid-current-source','method':METHOD,'enabled':False}
    if not isinstance(stored_check,dict) or stored_check.get('sourceSignature')!=signature:
        return {'accepted':False,'reason':'phrase-release-source-changed','method':METHOD,'enabled':False}
    result=check_release(vocal,point,words,coverage,current_source)
    if result.get('accepted') and abs(result['playbackGainDb']-stored_check.get('playbackGainDb',math.inf))>1e-6:
        return {'accepted':False,'reason':'phrase-release-level-changed','method':METHOD,'enabled':False}
    return result
