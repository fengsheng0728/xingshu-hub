"""T2-2 子进程 worker：独立进程打开 chroma_db 并写入 1 条文档"""
import chromadb, os, pathlib, time, sys
from chromadb.config import Settings
_ROOT = pathlib.Path(__file__).resolve().parent.parent  # 仓库根（tests/ 上一级）
CHROMA = str(_ROOT / "chroma_db")
t0 = time.time()
def log(m):
    print("[%6.1fs] %s" % (time.time() - t0, m), flush=True)
try:
    log("init PersistentClient (pid=%d)..." % os.getpid())
    c = chromadb.PersistentClient(path=CHROMA, settings=Settings(anonymized_telemetry=False))
    log("client ready")
    col = c.get_or_create_collection("sync_hub")
    col.add(ids=["dual-%d-%d" % (os.getpid(), int(time.time() * 1000))],
            documents=["dual process test"],
            metadatas=[{"pid": os.getpid()}])
    log("write ok")
    print("PROC-OK", os.getpid())
except Exception as e:
    log("FAIL %s: %s" % (type(e).__name__, str(e)[:120]))
    print("PROC-FAIL", type(e).__name__)
