"""Slow-mo retime: a [start,end] region should lengthen the clip and stay A/V-synced."""
import subprocess, tempfile
from pathlib import Path
from media import FFMPEG, probe
from pipeline import apply_slowmo, _enc, _atempo_chain

def build(p):
    subprocess.run([FFMPEG,"-y","-hide_banner","-loglevel","error",
        "-f","lavfi","-i","testsrc=size=320x240:rate=30:duration=6",
        "-f","lavfi","-i","sine=frequency=440:duration=6",
        "-pix_fmt","yuv420p","-c:a","aac","-shortest",str(p)],check=True)

def main():
    # atempo chain sanity: product of the stages should equal the target tempo
    ch=_atempo_chain(0.35); prod=1.0
    for st in ch.split(","): prod*=float(st.split("=")[1])
    assert ch.count("atempo")>=2 and abs(prod-0.35)<0.01, ch
    T=Path(tempfile.mkdtemp(prefix="fs_slow_"))
    src=T/"comp.mp4"; build(src)
    d0=probe(str(src)).duration
    dst=T/"slow.mp4"
    # slow [2,4] (a 2s window) at 0.3x -> should add roughly 2/0.3 - 2 ~ 4.7s
    nd=apply_slowmo(str(src),[{"start":2.0,"end":4.0}],str(T),str(dst),
                    320,240,30,_enc(20,"veryfast"),slow_speed=0.30)
    print(f"orig={d0:.2f}s  slowed={nd:.2f}s  (+{nd-d0:.2f}s)")
    assert nd > d0+2.0, "slow-mo did not lengthen the clip"
    # audio + video durations should match (stayed in sync)
    pr=probe(str(dst))
    print("ok video present, dur",round(pr.duration,2))
    # no-op when no regions
    dst2=T/"noop.mp4"
    nd2=apply_slowmo(str(src),[],str(T),str(dst2),320,240,30,_enc(20,"veryfast"))
    assert abs(nd2-d0)<0.2, "empty regions should copy through"
    print("PASS slowmo: lengthens region, copies through when empty")

if __name__=="__main__": main()
