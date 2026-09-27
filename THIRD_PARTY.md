# Third-party components

Hey Mint's own code is GPL-3.0.

**Included:** the screen-control engine in `launcher/engine` is adapted from
[jev-use](https://github.com/savka777/jev-use) by Savva Bojko, under the MIT licence
(`launcher/engine/LICENSE`).

It uses, but does not include, the following; each keeps its own license, which you should
review before redistributing a build:

| Component | How it is used | Where it comes from |
|---|---|---|
| openWakeWord (code and feature models) | Speech features for the “Hey Mint” detector | `pip install openwakeword`; models downloaded by `install.sh` |
| 3D-Speaker CAM++ speaker model | The voice lock | Downloaded by `install.sh` from the sherpa-onnx releases |
| Google Gemini API | Voice, reasoning, background work | Your own API key |
| OpenAI / OpenRouter | Background agents | Your own API key |
| TypeSafe Jev | Fast choices, and every action of the screen-control engine | Your own API key |
| PyObjC, NumPy, Pillow, mss, onnxruntime, PyAudio, kaldi-native-fbank | Runtime libraries | `requirements.txt` |
| Google Fonts (Bricolage Grotesque, Atkinson Hyperlegible, JetBrains Mono) | The guide's typography | Loaded from fonts.googleapis.com |

`models/hey_mint.json` and `models/hey_mint_data.npz` were trained for this project from
synthetic “Hey Mint” phrases (macOS system voices) and public speech (LibriSpeech, CC BY 4.0)
features; see `scripts/train_hey_mint.py`.
