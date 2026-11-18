"""Run in an isolated 512 MiB container before enabling local embeddings."""
import json
import os
import resource
import tempfile
import threading
import time
from pathlib import Path

from vault.core.crypto import KeyEnvelope
from vault.search.vectors import VectorIndex
from vault.web.app import create_app


index = VectorIndex(os.environ.get("VAULT_EMBEDDING_DIR", "/models"))
application = create_app(start_bot=False)
application.state.bot.engine.vectors = index
records = [
    {"id":"oa","title":"公司 OA","service":"","description":"日常办公审批和考勤","tags":["工作"]},
    {"id":"bastion","title":"堡垒机","service":"","description":"登录服务器，审计运维会话","tags":["工作"]},
    {"id":"vpn","title":"VPN","service":"","description":"远程连接公司内部网络","tags":["工作"]},
    {"id":"billing","title":"对账系统","service":"","description":"核对结算账面数据","tags":["工作"]},
]
begin=time.monotonic()
ranked=index.rank("远程连公司内网",records)
cold_ms=round((time.monotonic()-begin)*1000)
assert ranked[0]["id"] == "vpn"
times=[]
for query in ("OA","堡垒机","苹果","账目核对","在家连内网"):
    begin=time.monotonic(); index.rank(query,records); times.append(round((time.monotonic()-begin)*1000))
rss_line=next(line for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmRSS:'))
warm_rss_mib=round(int(rss_line.split()[1])/1024,1)

# Worst legal descriptor sizes and concurrent production Argon2id login.
long_rows=[dict(row,description="中文场景说明。"*285) for row in records]
errors=[]
def search_during_login():
    try:
        for _ in range(3): index.rank("测试长描述查询",long_rows)
    except Exception as exc: errors.append(type(exc).__name__)
with tempfile.TemporaryDirectory() as temp:
    envelope=KeyEnvelope(Path(temp)/"vault.json")
    key,_=envelope.create("synthetic benchmark master password")
    worker=threading.Thread(target=search_during_login)
    worker.start()
    assert envelope.unlock("synthetic benchmark master password")==key
    worker.join()
assert not errors
peak_file=Path('/sys/fs/cgroup/memory.peak')
peak=int(peak_file.read_text()) if peak_file.exists() else resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
assert peak < 480*1024*1024, "Insufficient memory headroom under the 512 MiB limit"
print(json.dumps({"embedding_model":"bge-small-zh-v1.5 int8","cold_query_ms":cold_ms,"warm_query_ms":times,
                  "peak_memory_mib":round(peak/1024/1024,1),"container_limit_mib":512,
                  "warm_app_rss_mib":warm_rss_mib,"concurrent_argon2id_and_long_descriptors":"passed","cpu_threads":1}))
