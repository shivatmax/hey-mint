# Third-party components

Hey Mint's own code is GPL-3.0.

**Included:** the screen-control engine in `launcher/engine` is adapted from
[jev-use](https://github.com/savka777/jev-use) by Savva Bojko, under the MIT licence
(`launcher/engine/LICENSE`).

**Adapted:** how `mint/tools/video.py` picks a video's caption track (the uploader's own before
automatic ones, and the language it is spoken in) follows `select_caption` in
[claude-video](https://github.com/bradautomates/claude-video) (MIT).

**Adapted design:** notch mode's look and motion - the notch shape, sizes, springs, the music, calendar,
battery and shelf views - follow [boring.notch](https://github.com/TheBoredTeam/boring.notch) by TheBoredTeam
(GPL-3.0, the same licence as Hey Mint), re-implemented in Python.

**Ideas, not code**, from these open-source projects (all MIT or BSD):
- **ChatGPT and Claude app control** (`mint/tools/agentapps.py`): driving the apps through native accessibility - the Stop button and Claude's "Running …" sidebar rows as the busy signal, "Show N more" paging, and checking the pasted prompt before sending - follows
  [m'kay](https://github.com/maxipesfix/mkay)'s `multi-agent-cli` (BSD-2).
- **Meeting recorder** (`launcher/recorder`): the Core Audio process tap follows
  [AudioCap](https://github.com/insidegui/AudioCap) (BSD-2) and
  [Meetily](https://github.com/Zackriya-Solutions/meeting-minutes) (MIT).
- **Teach by showing** (`mint/knowledge/teach.py`): events are compressed into steps the way
  [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) (MIT) and
  [microsoft/skill-recorder](https://github.com/microsoft/skill-recorder) (MIT) do it.
- **Tutor mode** (`mint/ui/tutor.py`): pointing one step at a time and repairing a missing target follow
  [Clicky](https://github.com/farzaa/clicky), [MudrikNow](https://github.com/abdallahmagdy15/mudriknow) and
  [ClickTutor_AI](https://github.com/Nishant8677/ClickTutor_AI) (all MIT).

It uses, but does not include, the following; each keeps its own license, which you should
review before redistributing a build:

| Component | How it is used | Where it comes from |
|---|---|---|
| openWakeWord code (Apache-2.0) and its shared melspectrogram / embedding feature models (Apache-2.0, from Google's speech embedding) | Speech features for the “Hey Mint” detector. None of openWakeWord's own pre-trained wake word models (CC BY-NC-SA 4.0) are used | `pip install openwakeword`; feature models downloaded by `install.sh` |
| 3D-Speaker CAM++ speaker model (Apache-2.0) | The voice lock | Downloaded by `install.sh` from the sherpa-onnx releases |
| Google Gemini API | Voice, reasoning, background work | Your own API key |
| OpenAI API | Background agents (GPT-6 Luna) and Codex | Your own API key or ChatGPT sign-in |
| TypeSafe Jev | Fast choices, and every action of the screen-control engine | Your own API key |
| PyObjC, NumPy, Pillow, mss, onnxruntime, PyAudio, kaldi-native-fbank | Runtime libraries | `requirements.txt` |
| yt-dlp (Unlicense) | Fetching web videos and their captions for `watch_video` | `requirements.txt` |
| openpyxl (MIT) | Writing .xlsx files for `make_spreadsheet` | `requirements.txt` |
| parakeet-mlx (Apache-2.0), optional | On-device transcription for `watch_video`, when installed | `pip install parakeet-mlx` |
| Google Fonts (Bricolage Grotesque, Atkinson Hyperlegible, JetBrains Mono) | The guide's typography | Loaded from fonts.googleapis.com |

`models/hey_mint.json` and `models/hey_mint_data.npz` were trained for this project from
synthetic “Hey Mint” phrases (macOS system voices) and public speech (LibriSpeech, CC BY 4.0)
features; see `scripts/train_hey_mint.py`.

**Wake word training data, note for commercial builds:** the synthetic phrases were spoken by macOS system
voices (`say`). Apple's macOS licence allows those voices for personal, non-commercial use, so any paid,
hosted or commercial build must first retrain the detector from commercially licensed recordings (your own
voice recordings, or a text-to-speech service whose terms allow it). LibriSpeech (CC BY 4.0) only needs
attribution.
