# FightSync — Live Streaming Setup (manual steps)

Everything in FightSync's **🔴 Live** tab is built (go-live, simulcast, live tracking, auto round
graphics, auto-director). These are the one-time **manual** steps on your end to wire it to OBS +
your cameras. Open the Live tab at **http://127.0.0.1:8765/live** while you do this.

## 0. Decisions already locked
- **Engine:** OBS (already installed) — FightSync drives it over obs-websocket.
- **Platforms:** YouTube + Twitch (TikTok skipped — its live access is follower-gated/restricted).
- **VR:** Quest standalone, cast wirelessly to the PC. Your **body** is tracked from a **room webcam**
  (USB), not the Quest — so you're never tethered to the PC.

## 1. Pages to open in the browser (Edge)
- **Twitch stream key:** https://dashboard.twitch.tv/settings/stream
- **YouTube stream key:** https://studio.youtube.com  → Go Live → Stream
- **Quest casting:** https://www.oculus.com/casting  (cast headset here; OBS window-captures this tab)
- **OBS NDI plugin (DistroAV):** https://github.com/DistroAV/DistroAV/releases/latest
- **iPhone camera app:** search "NDI HX Camera" in the App Store (install on the phone)

## 2. In OBS (no URL — done in the app)
1. **Tools → WebSocket Server Settings** → tick *Enable*, set a **password** (paste it into the Live tab).
2. **Start Virtual Camera** (required for live body tracking).
3. Install **DistroAV** (the NDI plugin), then **restart OBS**.
4. Build sources + a **scene per camera angle**:
   - **Gameplay** = *Window Capture* of the Quest casting browser tab.
   - **Webcam** = your room camera (this is what tracks your body for the skeleton/stats).
   - **Phone** = *+ → NDI Source* (after NDI HX Camera is running on the phone).
   - **Overlay** = *+ → Browser source* → the `/overlay` URL shown on the Live page (transparent
     skeleton + stats + round cards).
   - Name scenes clearly, e.g. `Cam: Front`, `Cam: Side`, `Cam: Phone`, `Gameplay`.

## 3. Quest gameplay → PC (wireless, no cable)
- Easiest: **oculus.com/casting** in the browser → OBS *Window Capture* that tab (~720p, ~0.5–2s lag).
- Sharper: **scrcpy** over wireless ADB (more setup, lower latency).

## 4. Cameras (phone now, more later)
- **iPhone:** NDI HX Camera app → broadcasts over Wi-Fi → appears as an OBS **NDI Source**.
  (Apple's built-in *Continuity Camera* is Mac-only; on Windows use the app. Simpler single-phone
  alternatives: **Camo** / **EpocCam**.)
- **More later:** more phones (NDI), or a real camera via an HDMI→USB capture card (Elgato Cam Link)
  or HDMI→NDI encoder — all show up as OBS sources.
- **Network:** 5 GHz Wi-Fi, **PC on Ethernet**, phones near the router and **plugged in**.

## 5. Encoder (for simulcast)
Set OBS's encoder to settings both platforms accept — **~1080p, 6000 kbps, 2 s keyframe interval** —
since simulcast shares one encode (FightSync's local relay copies it to both, no re-encode).

## 6. Going live — all in the Live tab
1. Save your **Twitch/YouTube keys** + **OBS password** → the OBS pill goes green, scenes appear.
2. **Start tracking** (skeleton appears on the overlay).
3. **Start auto-director** (tick the camera scenes to rotate) — optional.
4. **Start match** (autonomous round clock + between-round cards) — press at the opening bell.
5. Tick **Simulcast** (or pick one platform) → **● Go Live**.

Controls while live: ⏭ **Cut** (next angle), ⏭ **Next** / ⏸ (round controller), ■ **Stop**.
