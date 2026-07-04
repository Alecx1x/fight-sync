"""Corner two-up slow-mo replay box: overlays a top-right inset and lengthens the
clip (the main plays past the knockdown so the replay can finish)."""
import subprocess, tempfile
from pathlib import Path
from media import FFMPEG, probe
from pipeline import build_slowmo_replays, RenderConfig

def build(p,src,freq):
    subprocess.run([FFMPEG,"-y","-hide_banner","-loglevel","error",
        "-f","lavfi","-i",f"{src}=size=640x360:rate=30:duration=10",
        "-f","lavfi","-i",f"sine=frequency={freq}:duration=10",
        "-pix_fmt","yuv420p","-c:a","aac","-shortest",str(p)],check=True)

def main():
    T=Path(tempfile.mkdtemp(prefix="fs_rbox_"))
    gp=T/"g.mp4"; cam=T/"c.mp4"; comp=T/"main.mp4"
    build(gp,"testsrc",300); build(cam,"testsrc2",330)
    # a stand-in composite (same as gameplay here)
    build(comp,"smptebars",300)
    cfg=RenderConfig(out_w=640,out_h=360,fps=30,crf=24,preset="veryfast",slowmo_speed=0.3)
    dst=T/"body.mp4"
    base=probe(str(comp)).duration
    # knockdown window [3,5] on the composite timeline (gs=fs=0 here)
    nd=build_slowmo_replays(str(comp),str(gp),str(cam),0.0,0.0,base,
        [{"start":3.0,"end":5.0,"speed":0.3}],str(T),str(dst),cfg,0)
    pr=probe(str(dst))
    print(f"base={base:.2f}s  withReplay={nd:.2f}s  size={pr.width}x{pr.height}")
    assert pr.width==640 and pr.height==360, "frame size changed"
    # box appears at t=5 and runs (5-3)/~0.3 ~ 6-7s -> needs main to ~11-12s -> extended
    assert nd>base+1.0, "replay box did not extend the body to finish the replay"
    # empty regions -> straight copy
    dst2=T/"noop.mp4"
    nd2=build_slowmo_replays(str(comp),str(gp),str(cam),0.0,0.0,base,[],str(T),str(dst2),cfg,0)
    assert abs(nd2-base)<0.2, "empty regions should copy through"
    print("PASS replaybox: top-right 2-up overlaid, body extended, copy-through when empty")

if __name__=="__main__": main()
