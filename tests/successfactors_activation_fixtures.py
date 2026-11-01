"""Synthetic HTTP only; official identity strings come from retained Astra evidence."""
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import json
import requests

ROOT=Path(__file__).resolve().parents[1]
PACKAGE=ROOT/'reports/successfactors-ten-activation-2026-10-30'
COMPANIES={r['company']:r for r in json.loads((PACKAGE/'astra_evidence.json').read_text())}
CANDIDATES={r['company']:r['candidate_row'] for r in json.loads((PACKAGE/'canary_sources.json').read_text())}

def table(name, ids, *, total, offset=0, page_size=25, layout=True, next_url=None):
    title=COMPANIES[name]['identity_evidence']['observed_public_page_title']
    headers='<th id="hdrTitle">Title</th><th id="hdrLocation">Location</th>'
    layout_cell='<td class="hidden-phone"></td>' if layout else ''
    rows=[]
    for i in ids:
        rows.append(f'<tr class="data-row"><td headers="hdrTitle"><a class="jobTitle-link" href="/job/Fixture/{100000+i}/">Role {i}</a></td><td headers="hdrLocation">Calgary, AB, CA</td>{layout_cell}</tr>')
    start=offset+1 if total else 0
    label=f'<span class="paginationLabel">Results {start} – {offset+len(ids)} of {total}</span>'
    if next_url is None and offset+len(ids)<total:
        next_url=f'?q=&amp;sortColumn=referencedate&amp;sortDirection=desc&amp;startrow={offset+page_size}'
    link=f'<a href="{next_url}">Next</a>' if next_url else ''
    return f'<html><head><title>{escape(title)}</title></head><body>{label}{link}<table id="searchresults"><thead><tr>{headers}{layout_cell}</tr></thead><tbody>{"".join(rows)}</tbody></table></body></html>'

class Response:
    status_code=200
    headers={'content-type':'text/html;charset=UTF-8'}
    history=[]
    def __init__(self,text,url):
        self.text,self.url=text,url
        self.content=text.encode()

class SnapshotSession:
    last_final_url=None
    last_redirect_count=0
    def __init__(self,name,scenario='success',total=None,page_size=25):
        self.name,self.scenario,self.page_size=name,scenario,page_size
        self.total=total
        self.requests_started=0
        self.calls=[]
        self.attempt=-1
    def close(self):pass
    def get(self,url,**kwargs):
        self.requests_started+=1
        self.calls.append(url)
        offset=int(parse_qs(urlparse(url).query).get('startrow',['0'])[0])
        if offset==0:self.attempt+=1
        scenario=self.scenario
        if scenario=='timeout':raise requests.Timeout('fixture timeout')
        total=self.total if self.total is not None else (26 if scenario in {'pagination','limit','premature_short','missing_next','bad_next','mutation_once','second_mutation'} else 3)
        if scenario in {'zero','zero_missing_total'}:total=0
        if scenario=='repeated_page':total=50
        if scenario in {'mutation_once','second_mutation'} and offset and (self.attempt==0 or scenario=='second_mutation'):total+=1
        ids=list(range(offset,min(total,offset+self.page_size)))
        if self.attempt:ids=[i+10000*self.attempt for i in ids]
        if scenario in {'duplicates','conflicting_id'} and ids:ids[-1]=ids[0]
        if scenario=='repeated_page' and offset:ids=list(range(25))
        if scenario=='premature_short' and offset:ids=[]
        text=table(self.name,ids,total=total,offset=offset,page_size=self.page_size)
        if scenario in {'missing_total','zero_missing_total'}:
            text=text.replace('paginationLabel','unprovenLabel')
        if scenario=='missing_next':
            text=text.replace(f'startrow={offset+self.page_size}',f'startrow={offset+self.page_size+1}')
        if scenario=='bad_next':text=text.replace('?q=&amp;', 'https://unapproved.example/search/?q=&amp;')
        if scenario=='malformed':text=text.replace('/job/Fixture/100000/','/job/Fixture/no-id/')
        if scenario=='foreign_apply':text=text.replace('/job/Fixture/100000/','https://unapproved.example/job/Fixture/100000/')
        if scenario=='conflicting_id':text=text.replace('Role 0</a>','Different role</a>',1)
        if scenario=='identity':text=text.replace(COMPANIES[self.name]['identity_evidence']['observed_public_page_title'],'Other Employer Jobs')
        return Response(text,url)
