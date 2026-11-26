"""A channel source whose newest videos are all used goes back through the channel's older uploads (paged listing)."""
import os, sys, tempfile
d = tempfile.mkdtemp(); os.environ["DATA_DIR"] = d; os.environ["DATABASE_URL"] = "sqlite:///" + d.replace("\\", "/") + "/t.db"; os.environ["APP_PASSWORD"] = "owner-test-pass-88"
sys.path.insert(0, r"D:/integ-final")
from app import main, autopilot, youtube, quota
from app.db import Base, engine, migrate, SessionLocal, Channel, Destination
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m, flush=True)
Base.metadata.create_all(engine); migrate()
ALL = [f"vid{i:08d}" for i in range(130)]  # newest first
pages = []


class Req:
    def __init__(self, kw): self.kw = kw
    def execute(self):
        start = int(self.kw.get("pageToken") or 0); n = self.kw["maxResults"]; pages.append((start, n))
        items = [{"contentDetails": {"videoId": v}} for v in ALL[start:start + n]]
        out = {"items": items}
        if start + n < len(ALL): out["nextPageToken"] = str(start + n)
        return out


class YT:
    def playlistItems(self): return self
    def list(self, **kw): return Req(kw)


youtube.client = lambda *a, **k: YT()
quota.spend = lambda *a, **k: None
url = "https://www.youtube.com/channel/UC" + "a" * 22
ok(youtube.api_list_video_ids(url, 20) == ALL[:20] and pages == [(0, 20)], "the newest 20: one page")
pages.clear()
got = youtube.api_list_video_ids(url, 300)
ok(got == ALL and [p[0] for p in pages] == [0, 50, 100], f"300 asked, the channel has 130: three pages, all of them ({len(got)})")
pages.clear()
ok(youtube.api_list_video_ids(url, 120) == ALL[:120] and pages[-1] == (100, 20), "120 asked: stops at 120")
# the source: newest 20 all used -> takes older ones
made = []
autopilot._process_video = lambda s, ch, vid, log, *a, **k: made.append(vid) or True
with SessionLocal() as s:
    dst = Destination(channel_id="UCx", name="Main", token="x"); s.add(dst); s.commit()
    ch = Channel(kind="channel", query="q", name="Src", mode="cc", auto=True, destination_id=dst.id, resolved_url=url,
                 videos_per_run=5, clips_per_video=3, seen=ALL[:20]); s.add(ch); s.commit(); chid = ch.id
autopilot.run_channel(chid)
with SessionLocal() as s:
    st = s.get(Channel, chid).last_status or ""
ok(made == ALL[20:25], f"the newest 20 are used: the next 5 older videos are taken ({made})")
ok("older videos" in st, f"the source says so ({st[:70]})")
made.clear()
with SessionLocal() as s:
    c = s.get(Channel, chid); c.seen = list(ALL); s.commit()
autopilot.run_channel(chid)
with SessionLocal() as s:
    st = s.get(Channel, chid).last_status or ""
ok(made == [] and st == "no new videos", f"every video of the channel used: nothing, said plainly ({st})")
made.clear()
with SessionLocal() as s:
    c = s.get(Channel, chid); c.seen = ALL[5:]; s.commit()
autopilot.run_channel(chid)
ok(made == ALL[:5], "new videos still come first")
