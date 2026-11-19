# SonicBridge

SonicBridge sends short messages and files between two computers using their speakers and microphones. Each computer runs a local dashboard. Packet data and replies travel as sound. The built dashboard works offline after installation.

## Quick start

Install Python 3.11+ and Node.js 22. On Linux, install the PortAudio runtime for live audio. Debian and Ubuntu call it `libportaudio2`. See the [sounddevice installation guide](https://python-sounddevice.readthedocs.io/en/latest/installation.html) for other operating systems.

From the project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
npm --prefix web ci
npm --prefix web run build
python -m uvicorn sonicbridge.app:app --host 127.0.0.1 --port 8000
```

On Windows PowerShell, use `py -m venv .venv` and run `.venv\Scripts\python.exe` in place of `python` for installation and startup.

Open `http://127.0.0.1:8000`. Keep the server bound to localhost. Audio plays and records on the computer running Python.

## Present the project at a hackathon

Start with the live link if you have two laptops. Put one in **Receive** mode, then click **Test link** on the other and send a short message. The reply travels through the same audio channel as the message. The probe shows round-trip time, tone separation at the receiving laptop, and a suggested starting profile.

Then show recovery on one laptop:

1. Click **Run loss & recovery demo**. The simulated peer drops its first two replies. Watch the packet log record the two retries and the switch to Robust.
2. When the transfer pauses, click **Resume transfer**. The sender asks which chunks survived. The log shows the peer already has the first chunk and the sender begins at packet two.
3. Open **Received**. The message appears once with **Authenticated** next to its size.
4. In **Channel lab**, compare profiles in **Interference**, then **Quiet**. Each run sends the same message. The interference scenario adds noise, a 12 ms echo, a brief competing tone in each data frame, and two dropped acknowledgments. Export the results with **Export CSV**.

Suggested narration: “We can hear the signal. The laptops can turn it back into a file. Watch what happens when the channel fails partway through.”

The demo and channel lab label simulated results. Use the two-laptop test for a physical demonstration.

## Send between two computers

1. Start SonicBridge locally on both computers. Allow microphone access if the OS requests it.
2. Choose **Speaker + microphone** on both. Select the desired devices or use the system defaults.
3. On the receiving computer, choose **Receive** and click **Start listening**. Set both speakers to moderate volume and start with the computers close together in a quiet room.
4. On the sender, click **Test link**. If the peer replies, enter a short message and click **Transmit through sound**. Keep the channel clear until the sender reports completion.
5. Check **Received** on the other computer. Stop its listener before swapping roles.

Use built-in speakers or other speakers that can be heard in the room. Disable microphone noise suppression or other voice processing if it removes the modem tones. The audio devices need to accept mono audio at 48 kHz through PortAudio. Click **Stop operation** to stop transmitting or listening. Choose **Resume transfer** after an interruption. If the peer restarted, start its listener again first. Use the same shared passphrase as the original session for encrypted transfers.

## Protocol

- **Framing** uses a known sync pattern followed by a common-rate header. The CRC-16-protected header carries packet kind, profile, length, transfer ID, chunk index, and chunk count. The payload ends with a CRC-32 covering the header and payload.
- **Balanced** uses 1,200 Hz and 2,200 Hz tones with 20 ms per bit. **Robust** uses 40 ms per encoded bit and interleaved Hamming(7,4) error correction on the body. **Fast** uses 10 ms body symbols for data and acknowledgments. All profiles use the common preamble and header so the receiver can identify the body profile before decoding it.
- **Stop-and-wait** sends one chunk at a time. The receiver sends an acoustic ACK after validation, or a NACK if the header was valid but the body failed CRC. The sender retries a NACK or a timeout, with five attempts per chunk. Transfer IDs and chunk indexes prevent duplicate application delivery within the receiver’s saved transfer history.
- **Adaptive** starts in Balanced. It switches to Robust after two consecutive delivery failures or an ACK below 9 dB of median tone separation. Five consecutive successful packets with at least 16 dB of tone separation switch it back. Tone separation is the ratio of the winning and losing detector energies. It is a heuristic, and the settings need physical tuning.
- **Resume** sends a control frame requesting the peer’s received-chunk bitmap. The sender keeps one outgoing checkpoint. The receiver retains up to 256 recent transfers and expires chunks after seven days when it processes new data. The 20 most recent messages and incomplete transfers survive service restarts.
- **Encryption** is opt-in. Enter the same passphrase of at least 12 characters on both devices before listening or sending. Each message gets a random salt and nonce. Scrypt derives the AES-GCM key, and AES-GCM authenticates the encrypted name and contents against the transfer ID. A bad passphrase prevents final delivery. Use a strong passphrase shared outside SonicBridge. [Cryptography AEAD reference](https://cryptography.io/en/latest/hazmat/primitives/aead/)
- **Metrics** show acknowledged chunks, retries, CRC failures, and delivered payload bits per second. Simulation uses modeled airtime, turnaround, and timeouts. Acoustic mode uses elapsed time. The channel lab measures a single deterministic trial per profile for the selected scenario.

The maximum payload is 2,048 bytes. A maximum-size file can take several minutes. Nearby microphones can record the acoustic signal. Packet headers and control replies remain visible on the link. The inbox stores received messages in plaintext in the local SQLite database. By default it lives at `~/.local/share/sonicbridge/station.sqlite3`. Set `SONICBRIDGE_STATE_DIR` before launch to choose another directory.

## Verify

```sh
python -m unittest discover -s tests -v
npm --prefix web run build
npm --prefix web exec -- playwright install chromium
npm --prefix web run test:dashboard
```

The browser test restarts the service between pausing and resuming a transfer, downloads an encrypted file, and checks the exported CSV. GitHub Actions runs the Python tests, build, and browser test on pushes and pull requests.

Physical range and throughput need two computers. For a repeatable hardware comparison, send the same message using each profile at each distance, keeping device placement and playback level fixed.
