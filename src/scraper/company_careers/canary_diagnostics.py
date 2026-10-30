"""Bounded metadata for untrusted HTTP contracts; never retain response text.

Only reviewed hosts/routes, a fixed query-key vocabulary, canonical offsets and
empty/nonempty flags are exposed. Credentials, arbitrary paths/query values,
raw Location/header values, HTML, job rows and exception messages are omitted.
These observations do not participate in identity or completion decisions.
"""
from __future__ import annotations

import hashlib
import codecs
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, urljoin, urlparse

from src.scraper.company_careers.models import AdapterError

QUERY_KEYS = frozenset({'startrow','q','locationsearch','sortColumn','sortDirection',
                        'searchby','locale','lang','page','company','country','locationCountry',
                        'token','code','state','email','session','auth','redirect_uri'})
APPROVED_KEYS = frozenset({'startrow','q','locationsearch','sortColumn','sortDirection'})
MIME_TYPES = frozenset({'text/html','application/xhtml+xml','application/json','text/plain'})
MAX_DIAGNOSTIC_BYTES = 3_000_000
STAGES = frozenset({'request_destination','response_destination','pagination_anchor',
                    'destination_validation','employer_identity','record_identity','apply_identity',
                    'prepared_request','redirect_contract','transport_destination','transport'})


def destination_summary(url: str, reviewed_url: str) -> dict:
    """A small, total function; no raw unknown hostname/path or query value."""
    try:
        parsed, reviewed = urlparse(str(url)), urlparse(str(reviewed_url))
        host_ok = parsed.hostname == reviewed.hostname
        try: port = parsed.port
        except ValueError: port = -1
        pairs = parse_qsl(parsed.query,keep_blank_values=True,max_num_fields=128)
        known = [k for k,_ in pairs if k in QUERY_KEYS]
        offsets = [v for k,v in pairs if k=='startrow']
        offset = int(offsets[0]) if len(offsets)==1 and re.fullmatch(r'0|[1-9][0-9]{0,8}',offsets[0]) else None
        path_kind = ('reviewed_search' if parsed.path==reviewed.path else
                     'authentication' if re.search(r'/(login|signin|sign-in|auth|oauth|challenge|captcha)(/|$)',parsed.path,re.I) else
                     'off_contract')
        return {'scheme':parsed.scheme if parsed.scheme in {'http','https'} else 'other',
                'host_is_reviewed':host_ok,'hostname':reviewed.hostname if host_ok else None,
                'port_is_default_https':port in {None,443},
                'userinfo_present':parsed.username is not None or parsed.password is not None,
                'path_kind':path_kind,'reviewed_path':reviewed.path,
                'fragment_present':bool(parsed.fragment),'query_field_count':len(pairs),
                'query_keys':sorted(set(known)),
                'unknown_query_key_count':sum(k not in QUERY_KEYS for k,_ in pairs),
                'unreviewed_query_keys':sorted(set(k for k,_ in pairs if k in QUERY_KEYS-APPROVED_KEYS)),
                'duplicate_query_keys':len(pairs)!=len({k for k,_ in pairs}),
                'startrow':offset,'startrow_count':len(offsets),
                'startrow_valid':not offsets or offset is not None,
                'q_nonempty':any(k=='q' and bool(v) for k,v in pairs),
                'locationsearch_nonempty':any(k=='locationsearch' and bool(v) for k,v in pairs)}
    except Exception:
        return {'parse_status':'malformed','raw_destination_retained':False}


class _Shape(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.counts={'searchresults_tables':0,'headers':0,'data_rows':0,'layout_cells':0,'title_links':0,'pagination_labels':0}
    def handle_starttag(self,tag,attrs):
        a=dict(attrs);classes=set(str(a.get('class','')).casefold().split())
        self.counts['searchresults_tables']+=int(tag=='table' and str(a.get('id','')).casefold()=='searchresults')
        self.counts['headers']+=int(tag=='th')
        self.counts['data_rows']+=int(tag=='tr' and 'data-row' in classes)
        self.counts['layout_cells']+=int(tag=='td' and classes=={'hidden-phone'})
        self.counts['title_links']+=int(tag=='a' and 'jobtitle-link' in classes)
        self.counts['pagination_labels']+=int('paginationlabel' in classes)


def response_summary(response,reviewed_url: str,*,include_body=False) -> dict:
    """Inspect already loaded bytes only; never invoke content/text properties."""
    result={'destination':destination_summary(getattr(response,'url',''),reviewed_url)}
    status=getattr(response,'status_code',None)
    result['status_code']=status if type(status) is int and 100<=status<=599 else None
    headers=getattr(response,'headers',{}) or {}
    mime=str(headers.get('content-type',headers.get('Content-Type',''))).split(';',1)[0].strip().casefold()
    result['content_type']=mime if mime in MIME_TYPES else 'other'
    location=headers.get('Location',headers.get('location'))
    if location:
        result['redirect_destination']=destination_summary(urljoin(str(getattr(response,'url','')),str(location)),reviewed_url)
    body=vars(response).get('_content') if hasattr(response,'__dict__') else None
    result['body_available']=isinstance(body,bytes)
    if include_body and isinstance(body,bytes):
        result['response_bytes']=len(body)
        result['response_sha256']=hashlib.sha256(body).hexdigest()
        if mime in {'text/html','application/xhtml+xml'}:
            parser=_Shape()
            try:
                decoder=codecs.getincrementaldecoder('utf-8')(errors='replace')
                limit=min(len(body),MAX_DIAGNOSTIC_BYTES)
                for start in range(0,limit,65536):
                    parser.feed(decoder.decode(body[start:min(start+65536,limit)],final=False))
                parser.feed(decoder.decode(b'',final=True));parser.close()
                result['html_shape']=parser.counts
                result['shape_prefix_bytes']=limit
                result['shape_truncated']=len(body)>MAX_DIAGNOSTIC_BYTES
            except Exception:
                result['html_shape_status']='unreadable'
    return result


def contract_error(message: str,*,code: str,stage: str,url=None,reviewed_url=None,offset=None) -> AdapterError:
    exc=AdapterError(message)  # Keep the original exception type and message.
    safe={'code':code,'stage':stage if stage in STAGES else 'destination_validation'}
    if url is not None and reviewed_url is not None:safe['destination']=destination_summary(url,reviewed_url)
    if type(offset) is int and 0<=offset<=20000:safe['expected_offset']=offset
    exc.safe_canary_diagnostic=safe
    return exc


def error_summary(exc,reviewed_url: str,metadata: dict) -> dict:
    attached=getattr(exc,'safe_canary_diagnostic',None)
    # Only our fixed diagnostic vocabulary and shapes may cross the artifact boundary.
    allowed_codes={'sf_table_destination_rejected','sf_employer_marker_mismatch',
                   'sf_title_link_disagreement','sf_apply_url_rejected'}
    if isinstance(attached,dict) and attached.get('code') in allowed_codes and attached.get('stage') in STAGES:
        result={'code':attached['code'],'stage':attached['stage']}
        if isinstance(attached.get('destination'),dict):
            result['destination']=safe_destination(attached['destination'],reviewed_url)
        offset=attached.get('expected_offset')
        if type(offset) is int and 0<=offset<=20000:result['expected_offset']=offset
    else:
        result={'code':'unclassified_error','stage':'transport'}
    name=type(exc).__name__
    result['exception_type']=name if name in {'AdapterError','CanaryDestinationRejected','CanaryAccessStopped',
            'CanaryBudgetReached','BatchBoundReached','Timeout','ReadTimeout','ConnectTimeout','ConnectionError',
            'TooManyRedirects','_TransientSuccessFactorsError'} else 'other'
    for key in ('pages_completed','page_size','jobs_normalized','total_available'):
        value=metadata.get(key)
        if type(value) is int and 0<=value<=20000:result[key]=value
    return result


def safe_destination(value,reviewed_url):
    """Validate even internally attached summaries before serialization."""
    reviewed=urlparse(reviewed_url)
    out={}
    bools={'host_is_reviewed','port_is_default_https','userinfo_present','fragment_present',
           'duplicate_query_keys','startrow_valid','q_nonempty','locationsearch_nonempty'}
    ints={'query_field_count','unknown_query_key_count','startrow','startrow_count'}
    for key in bools:
        if type(value.get(key)) is bool:out[key]=value[key]
    for key in ints:
        if type(value.get(key)) is int and 0<=value[key]<=10**9:out[key]=value[key]
    if value.get('scheme') in {'http','https','other'}:out['scheme']=value['scheme']
    if value.get('path_kind') in {'reviewed_search','authentication','off_contract'}:out['path_kind']=value['path_kind']
    out['hostname']=reviewed.hostname if value.get('hostname')==reviewed.hostname else None
    out['reviewed_path']=reviewed.path
    for key in ('query_keys','unreviewed_query_keys'):
        if isinstance(value.get(key),list):out[key]=sorted({v for v in value[key] if isinstance(v,str) and v in QUERY_KEYS})
    if value.get('parse_status')=='malformed':out={'parse_status':'malformed','raw_destination_retained':False}
    return out


class DiagnosticLedger:
    def __init__(self,reviewed_url: str,physical_cap: int):
        self.reviewed_url=reviewed_url;self.limit=3*physical_cap+16
        self.events=[];self.dropped=0;self.observer_errors=0
    def add(self,event: str,attempt: int,**details):
        if len(self.events)>=self.limit:self.dropped+=1;return
        self.events.append({'event':event,'physical_attempt':attempt,**details})
    def export(self):
        return {'schema_version':1,'events':self.events,'event_limit':self.limit,
                'dropped_events':self.dropped,'observer_errors':self.observer_errors,
                'raw_bodies_urls_headers_or_messages_retained':False}
