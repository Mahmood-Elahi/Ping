"""Reproducible local persistence orchestration; explicit in-memory client only.

No SDK client, credentials, SQL server, provider or Sheets connection is created.
The fake implements the same staged RPC protocol already tested by the repository.
"""
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from src.integrations.supabase_job_store import SupabaseJobStore
from tests.test_supabase_job_store import FakeSupabaseClient

COUNTS={'Keyence':11,'TransAlta':10,'Spartan Controls':16,'ARC Resources':22,'Gibson Energy':12,
        'SaskPower':26,'Hydro One':13,'Omron':62,'Canada Life':86,'Hyundai':262}


def benchmark(description_chars=1500):
    now=datetime(2026,10,28,tzinfo=timezone.utc)
    jobs=[dict(job_id=f'{name}:{i}',title='Software Engineering Intern',company=name,source='sap_successfactors',
               source_scope='default',location='Calgary, Canada',url=f'https://benchmark.invalid/job/{index}/{i}',
               updated_at=now.isoformat(),description='x'*description_chars,relevance_score=91,
               relevance_band='high',location_status='eligible_canada')
          for index,(name,count) in enumerate(COUNTS.items()) for i in range(count)]
    client=FakeSupabaseClient();store=SupabaseJobStore(client,now_factory=lambda:now)
    def denied(*a,**k):raise AssertionError('Network is forbidden in offline measurement')
    start=time.perf_counter()
    with patch.object(socket.socket,'connect',denied),patch.object(socket,'create_connection',denied):
        result=store.sync_student_jobs(jobs,successful_companies=COUNTS,started_at=now,companies_checked=10,
                                      jobs_fetched=len(jobs),successful_source_scopes=[(n,'default') for n in COUNTS])
    elapsed=time.perf_counter()-start
    rpc=Counter(n for n,_ in client.rpc_calls)
    return {'jobs':len(jobs),'description_chars':description_chars,'elapsed_cpu_orchestration_seconds':elapsed,
            'rpc_calls':dict(rpc),'rpc_total':len(client.rpc_calls),'table_operations':len(client.select_calls),
            'rpc_payload_bytes':sum(len(json.dumps(p,separators=(',',':')).encode()) for _,p in client.rpc_calls),
            'in_memory_tracked_rows':len(client.tracked_jobs),'external_requests':0,'external_writes':0,
            'client':'tests.test_supabase_job_store.FakeSupabaseClient','network_denied':True,
            'limitation':'Client CPU/serialization/protocol counts; server execution and external latency are not measured'}

if __name__=='__main__':
    print(json.dumps({'normal':benchmark(),'large_description_stress':benchmark(20000)},indent=2))
