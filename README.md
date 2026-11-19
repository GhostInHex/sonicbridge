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

## Run the demonstration

1. Select **Simulation** and click **Run simulation**. The app generates audio samples, runs them through the receiver’s streaming decoder, and decodes the simulated peer’s ACK. It shows the recovered message, packet log, waveform, and spectrogram.
2. Enable **Drop the first two ACKs** and run again in **Adaptive** mode. The log shows two timeouts, duplicate suppression, and the switch to Robust mode. The receiver displays the message once per transfer.
3. Attach a file up to 2,048 bytes. Run the simulation and download the recovered copy from **Received**.

## Send between two computers

1. Start SonicBridge locally on both computers. Allow microphone access if the OS requests it.
2. Choose **Speaker + microphone** on both. Select the desired devices or use the system defaults.
3. On the receiving computer, choose **Receive** and click **Start listening**. Set both speakers to moderate volume and start with the computers close together in a quiet room.
4. On the sender, enter a short message and click **Transmit through sound**. Keep the channel clear until the sender reports completion.
5. Check **Received** on the other computer. Stop its listener before swapping roles.

Use built-in speakers or other speakers that can be heard in the room. Disable microphone noise suppression or other voice processing if it removes the modem tones. The audio devices need to accept mono audio at 48 kHz through PortAudio. Click **Stop operation** to stop transmitting or listening. Received transfers live in memory until the service restarts.

## Protocol

- **Framing** uses a known sync pattern followed by a common-rate header. The CRC-16-protected header carries packet kind, profile, length, transfer ID, chunk index, and chunk count. The payload ends with a CRC-32 covering the header and payload.
- **Balanced** uses 1,200 Hz and 2,200 Hz tones with 20 ms per bit. **Robust** uses 40 ms per encoded bit and interleaved Hamming(7,4) error correction on the body. Both use the common preamble and header so the receiver can identify the profile before decoding the body.
- **Stop-and-wait** sends one chunk at a time. The receiver sends an acoustic ACK after validation, or a NACK if the header was valid but the body failed CRC. The sender retries a NACK or a timeout, with five attempts per chunk. Transfer IDs and chunk indexes prevent duplicate application delivery within the receiver’s session cache.
- **Adaptive** starts in Balanced. It switches to Robust after two consecutive delivery failures or an ACK below 9 dB of median tone separation. Five consecutive successful packets with at least 16 dB of tone separation switch it back. Tone separation is the ratio of the winning and losing detector energies. It is a heuristic, and the settings need physical tuning.
- **Metrics** show acknowledged chunks, retries, CRC failures, and delivered payload bits per second. Simulation uses generated airtime, including retries and feedback samples. Acoustic mode uses elapsed time including waits.

The raw Balanced symbol rate is 50 bit/s and overhead lowers payload throughput. A maximum-size file can take several minutes. Nearby microphones can hear the link. The CRC checks integrity against accidental corruption, and the link sends plaintext.

## Verify

```sh
python -m unittest discover -s tests -v
npm --prefix web run build
```

The tests exercise synchronization across recording offsets, noise, both profiles, FEC, CRC rejection, file reconstruction, retries, and duplicate suppression. Physical range and delivery-rate measurements require two computers. Run fixed Balanced, fixed Robust, and Adaptive with the same message at each distance and noise level to compare repeatability, delivery, and payload throughput.
