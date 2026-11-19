# SonicBridge hackathon field guide

**What you’ll show.** A judge chooses a short word. Laptop A plays it as sound. Laptop B decodes it and replies through its own speaker. Then you deliberately interrupt a simulated transfer and let a judge resume it from the missing packets.

**Bring** two laptops with built-in speakers and microphones, both chargers, a display adapter if you need one, and a local copy of this guide. Assign one person to present and one person to operate laptop B. If you present alone, place both screens within reach.

## 1. Set up both laptops before the event

Repeat this entire section on **each** laptop. They can run different operating systems. Each copy serves its own dashboard at `http://127.0.0.1:8000`. The browser on each laptop talks to its local Python service. The two Python services exchange packets through speakers and microphones.

### Get the tools and project

1. Install Python 3.12 or 3.13 and Node.js 22. Reopen your terminal after installing them. Check that `node --version` prints `v22.x` and `npm --version` prints a version. [Python downloads](https://www.python.org/downloads/) · [Node.js downloads](https://nodejs.org/en/download)
2. Sign into GitHub with access to the private repository. On the repository page, choose **Code → Download ZIP** and extract it. Use this route if you have no Git setup. If your Git client already has access, run `git clone https://github.com/GhostInHex/sonicbridge.git`. [SonicBridge repository](https://github.com/GhostInHex/sonicbridge)
3. Open a terminal in the extracted or cloned folder. You should see `requirements.txt`, the `sonicbridge` folder, and the `web` folder. Use the same revision on both laptops. On cloned copies, `git rev-parse --short HEAD` should print the same value.

### Windows PowerShell

Run one line at a time from the project folder. Use the virtual environment’s Python directly.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm.cmd --prefix web ci
npm.cmd --prefix web run build
.\.venv\Scripts\python.exe -m sounddevice
.\.venv\Scripts\python.exe -m uvicorn sonicbridge.app:app --host 127.0.0.1 --port 8000
```

If you installed Python 3.13, change `py -3.12` to `py -3.13`. If `py` is unavailable, check `python --version` and use `python -m venv .venv` when it points to your intended Python. `npm.cmd` works without running PowerShell’s `npm.ps1` wrapper.

### macOS or Linux terminal

On Debian or Ubuntu, install the audio runtime and Python virtual environment package first. On macOS, pip normally supplies PortAudio with `sounddevice`. [sounddevice install guide](https://python-sounddevice.readthedocs.io/en/latest/installation.html)

```sh
sudo apt-get update
sudo apt-get install -y python3-venv libportaudio2
```

Run the following on macOS or Linux. Check that `python3 --version` points to your intended Python before creating the environment.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix web ci
npm --prefix web run build
.venv/bin/python -m sounddevice
.venv/bin/python -m uvicorn sonicbridge.app:app --host 127.0.0.1 --port 8000
```

### Check each laptop before moving on

- The dashboard build creates `web/dist/index.html`. The sounddevice command should list a usable input and output for the live demo.
- Leave Uvicorn running. Open `http://127.0.0.1:8000` in that laptop’s browser. Check for the **SonicBridge** heading and an **idle** status.
- Run a local smoke test. Set **Channel** to **Simulation**, leave the passphrase blank, type `Ready`, and click **Run simulation**. Check **Received** for the exact text and **Packet log** for **All packets acknowledged**. Do this on both laptops.
- On show day, reopen a terminal in the project folder and run only the final Uvicorn command for your OS. Reopen the local URL. The installed app can run offline.

## 2. Rehearse the live link before relying on it

1. **Prepare the room.** Place laptops about 30 cm apart as a starting point. Use built-in speakers at moderate volume and unmute both laptops. Disconnect Bluetooth headsets and close call or recording apps that are using audio. If projecting laptop A, confirm its speaker still outputs locally.
2. **Start laptop B first.** Choose **Speaker + microphone → Receive**. Select the built-in microphone and speakers, or leave **System default** if you verified the defaults. Leave **Shared passphrase** blank. Click **Start listening** and confirm **listening**.
3. **Probe from laptop A.** Choose **Speaker + microphone → Send**. Match the device choices, leave the passphrase blank, and click **Test link**. Wait for **Peer replied**. The probe tests both directions because B has to decode the request and A has to decode the reply.
4. **Send a short rehearsal message.** On A, use **Adaptive** or click the profile suggested by the probe. Send `HELLO` with **Transmit through sound**. Wait while both laptops play their tones. On B, check **Received** for the exact new text. On A, check **All packets acknowledged**. Repeat once with different text.
5. **Rehearse the optional encryption demo.** Stop B’s listener before changing its settings. Enter the same demo passphrase of at least 12 characters on A and B, then start B listening again. Send another short message. B should mark it **Authenticated**. Use a throwaway phrase off the projector. For the main stage demo, leave this step to the built-in recovery demo.
6. **Check the offline claim.** Once the app is installed and the physical link works, disconnect both laptops from Wi-Fi and Bluetooth and repeat the live transfer. Use your phones or a separate device for any event network needs.

Both laptops need working audio in both directions. A useful rehearsal tests the same laptops, devices, and placement you will use on stage. Keep the payload to a few characters for the live demonstration. Use the simulator for a longer message or a file.

## 3. Present it in three minutes

| Time | What to do | What to say |
| --- | --- | --- |
| 0:00–0:25 | Show laptop A’s dashboard and point to B. Have your teammate start B listening before you begin. | “We turned two ordinary laptops into an acoustic data link. The sender plays bits as two pitches. The receiver decodes the tones and plays an acknowledgment back.” |
| 0:25–1:05 | Ask for a 4–8 character word or number. Send it live after a successful backstage probe. Show B’s new inbox entry and A’s ACK. | “Listen for the message from this speaker and the reply from the other laptop. Here is the exact text we recovered.” |
| 1:05–2:10 | On A, click **Run loss & recovery demo**. Point out both retries and the switch to Robust. When the saved transfer banner appears, ask a judge to click **Resume transfer**. | “This next run is simulated so we can repeat the same failure. The simulated peer drops two replies. Then we interrupt the transfer. When we resume, SonicBridge finds the packet the peer already has and sends the rest. The receiver authenticates the decoded message and shows it once.” |
| 2:10–2:45 | Choose **Interference** in **Channel Lab**, then click **Compare profiles**. Show the delivery labels and the throughput bars. If time permits, run **Quiet**. | “A faster symbol helps on a clean link. Under this modeled interference, longer symbols and error correction can keep the transfer alive. These are simulated results. We would measure range and throughput separately on physical hardware.” |
| 2:45–3:00 | Close on the received message or a clean view of the log. | “SonicBridge makes short offline exchanges possible with audio hardware people already have.” |

**If you only have 60 seconds**, run the recovery demo, show the two retries, let the judge click Resume, and highlight that the app sends only the missing packets. Call it a simulation before pressing the button.

### Questions judges may ask

- **How does it work?** Two tones represent zero and one. The receiver finds a known start pattern, checks packet integrity, and replies. Robust mode adds longer symbols and error correction.
- **What happens when replies are lost?** The sender retries the same packet. The receiver recognizes the repeat and acknowledges it without delivering the message twice.
- **What makes resume work after a restart?** The sender saves one outgoing transfer checkpoint. The receiver saves verified chunks. On resume, the sender asks for the peer’s received-chunk bitmap before sending more data.
- **Does it use Wi-Fi or Bluetooth?** After installation, each browser uses its own laptop’s local service. Data packets and replies between the laptops travel acoustically.
- **How secure is it?** Optional shared-passphrase encryption protects the message contents in transit. The receiver verifies the authentication tag before delivery. Control packets are visible, and the recipient’s inbox stores the recovered contents locally in plaintext.
- **How big a file can it send?** The current payload limit is 2,048 bytes. Present live with text. File receipt and byte-for-byte download are covered by the browser test.
- **How far does it work?** Report only the distance and conditions you actually rehearsed. This implementation’s simulated test results do not establish physical range.

## 4. Recover without losing stage time

| What you see | What to do |
| --- | --- |
| Browser cannot connect | Check that Uvicorn is still running on that laptop. Open `http://127.0.0.1:8000` locally. If the port is occupied, start Uvicorn with `--port 8001` and open `http://127.0.0.1:8001` on that laptop. The laptops can use different HTTP ports. |
| Dashboard root shows a 404 | Build the web app from the project folder, stop Uvicorn with **Ctrl+C**, then start it again. The service looks for `web/dist` at startup. |
| `PortAudio library not found` | On Debian or Ubuntu install `libportaudio2`, then restart Uvicorn. On Windows or macOS reinstall the requirements inside the project virtual environment. Use the simulator if audio setup will take too long. |
| Microphone is silent or unavailable | Grant microphone access to the terminal or Python process in OS privacy settings. Restart the service if permission changed. Select the built-in mic explicitly. Python opens the mic. |
| Probe times out | Confirm B is **listening**. Check both output volumes and input devices. Move the laptops closer. Try the probe once more. If it still fails, pivot to the labeled simulation. |
| A reports ACK timeout but B shows the message | The data made it to B. Check B’s speaker and A’s microphone for the return path. Wait for confirmation on A before announcing that both directions worked. |
| A says the peer could not unlock the message | Stop B, correct the passphrase, start B listening, then click **Resume transfer** on A. |
| Demo button is disabled | Stop any active operation. If there’s a saved transfer, resume it or discard it if you no longer need it. Discarding clears the sender checkpoint and gives up resume for that transfer. |
| Venue audio is too noisy or only one laptop works | Stop any active operation. If a failed live send left a checkpoint you still need, run the demo on the other laptop after stopping its listener. Otherwise discard the checkpoint and click **Run loss & recovery demo**. Say “We’re switching to controlled simulation for the recovery sequence.” Show the same dashboard events and comparison without claiming that packets crossed the room. |

**Final check before walking on stage:** Both laptops are plugged in. Laptop B is listening. Laptop A passed the probe. The projected dashboard is visible from the back. Your simulated recovery path is rehearsed and there’s no leftover checkpoint blocking it. The [GitHub repository](https://github.com/GhostInHex/sonicbridge) is private, so confirm judge or teammate access if required.
