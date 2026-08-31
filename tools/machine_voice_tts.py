#!/usr/bin/env python3
"""Small, low-compute TTS lab for JianYing, iFLYTEK, and macOS voices.

The tool deliberately keeps synthesis and audio post-processing separate:

* ``jianying`` uses JianYing's ``BV001_fast_streaming`` voice by default.
* ``xfyun`` sends text to iFLYTEK's streaming WebSocket TTS API.
* ``say`` uses the built-in macOS speech synthesizer without a model download.
* ``process`` applies lightweight FFmpeg pitch, tempo, band-limit, compression,
  and optional bit-crush effects.

Credentials are read from environment variables first, then from the repo's
ignored ``api_key/科大讯飞TTS.txt`` file. Secret values are never printed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlencode, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CREDENTIALS = REPO_ROOT / "api_key" / "科大讯飞TTS.txt"
DEFAULT_ENDPOINT = "wss://tts-api.xfyun.cn/v2/tts"
OFFICIAL_XFYUN_HOSTS = {"tts-api.xfyun.cn", "tts-api-sg.xf-yun.com"}
JIANYING_ENDPOINT = "wss://sami.bytedance.com/internal/api/v2/ws"
JIANYING_APP_ID = "3704"
JIANYING_APP_KEY = "IZjhUeAYwP"
JIANYING_DEFAULT_DEVICE_ID = "1053764930506284"
JIANYING_DEFAULT_IID = "2314914062247833"
JIANYING_DEFAULT_SPEAKER = "BV001_fast_streaming"
JIANYING_MAX_AUDIO_BYTES = 50 * 1024 * 1024


class MachineVoiceError(RuntimeError):
    """A user-facing synthesis or processing error."""


@dataclass(frozen=True)
class XfyunCredentials:
    app_id: str
    api_secret: str
    api_key: str


def _value_after_label(text: str, label: str) -> str | None:
    """Read either ``LABEL: value`` or a value on the next non-empty line."""
    lines = [line.strip() for line in text.splitlines()]
    inline = re.compile(
        rf"{re.escape(label)}\s*[：:=]\s*([A-Za-z0-9_\-]+)", re.IGNORECASE
    )
    label_only = re.compile(rf"^{re.escape(label)}\s*[：:=]?\s*$", re.IGNORECASE)
    for index, line in enumerate(lines):
        match = inline.search(line)
        if match:
            return match.group(1)
        if label_only.match(line):
            for candidate in lines[index + 1 :]:
                if candidate:
                    return candidate
    return None


def load_xfyun_credentials(path: Path) -> XfyunCredentials:
    app_id = os.environ.get("XFYUN_APP_ID") or os.environ.get("XFYUN_APPID")
    api_secret = os.environ.get("XFYUN_API_SECRET") or os.environ.get(
        "XFYUN_APISECRET"
    )
    api_key = os.environ.get("XFYUN_API_KEY") or os.environ.get("XFYUN_APIKEY")

    if not (app_id and api_secret and api_key):
        if not path.is_file():
            raise MachineVoiceError(
                "iFLYTEK credentials were not found. Set XFYUN_APP_ID, "
                "XFYUN_API_SECRET, and XFYUN_API_KEY, or provide --credentials."
            )
        text = path.read_text(encoding="utf-8-sig")
        app_id = app_id or _value_after_label(text, "APPID")
        api_secret = api_secret or _value_after_label(text, "APISecret")
        api_key = api_key or _value_after_label(text, "APIKey")

    if not (app_id and api_secret and api_key):
        raise MachineVoiceError(
            "The credential source is missing APPID, APISecret, or APIKey."
        )
    if not all(re.fullmatch(r"[A-Za-z0-9_\-]+", value) for value in (
        app_id,
        api_secret,
        api_key,
    )):
        raise MachineVoiceError("The credential source has an unexpected format.")
    return XfyunCredentials(app_id=app_id, api_secret=api_secret, api_key=api_key)


def build_xfyun_auth_url(
    endpoint: str, credentials: XfyunCredentials, date: str | None = None
) -> str:
    parsed = urlparse(endpoint)
    try:
        port = parsed.port
    except ValueError as exc:
        raise MachineVoiceError("--endpoint contains an invalid port.") from exc
    if (
        parsed.scheme != "wss"
        or parsed.hostname not in OFFICIAL_XFYUN_HOSTS
        or parsed.path != "/v2/tts"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        hosts = ", ".join(sorted(OFFICIAL_XFYUN_HOSTS))
        raise MachineVoiceError(
            "--endpoint must be the official wss /v2/tts endpoint on: " + hosts
        )
    request_date = date or formatdate(usegmt=True)
    signature_origin = (
        f"host: {parsed.netloc}\n"
        f"date: {request_date}\n"
        f"GET {parsed.path} HTTP/1.1"
    )
    digest = hmac.new(
        credentials.api_secret.encode("utf-8"),
        signature_origin.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    signature = base64.b64encode(digest).decode("ascii")
    authorization_origin = (
        f'api_key="{credentials.api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'
    )
    authorization = base64.b64encode(
        authorization_origin.encode("utf-8")
    ).decode("ascii")
    query = urlencode(
        {
            "authorization": authorization,
            "date": request_date,
            "host": parsed.netloc,
        }
    )
    return f"{endpoint}?{query}"


def _redact_xfyun_error(error: Exception, credentials: XfyunCredentials) -> str:
    """Keep connection errors useful without echoing credentials or signed URLs."""
    detail = str(error) or type(error).__name__
    detail = re.sub(
        r"(?i)(authorization=)[^&\s]+", r"\1[REDACTED]", detail
    )
    for value in (credentials.app_id, credentials.api_key, credentials.api_secret):
        detail = detail.replace(value, "[REDACTED]")
    return detail[:1000]


def synthesize_xfyun(
    *,
    text: str,
    output: Path,
    credentials_path: Path,
    endpoint: str,
    voice: str,
    speed: int,
    pitch: int,
    volume: int,
    sample_rate: int,
    timeout: float,
) -> None:
    if not text.strip():
        raise MachineVoiceError("Text must not be empty.")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise MachineVoiceError("--timeout must be finite and between 0 and 300.")
    text_bytes = text.encode("utf-8")
    if len(text_bytes) >= 8000:
        raise MachineVoiceError(
            "The UTF-8 text is too long for one iFLYTEK request (8000 bytes)."
        )
    encoded_text = base64.b64encode(text_bytes)

    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MachineVoiceError(
            "The xfyun command requires websocket-client. Install "
            "requirements-machine-voice.txt first."
        ) from exc

    credentials = load_xfyun_credentials(credentials_path)
    auth_url = build_xfyun_auth_url(endpoint, credentials)
    request = {
        "common": {"app_id": credentials.app_id},
        "business": {
            "aue": "lame",
            "sfl": 1,
            "auf": f"audio/L16;rate={sample_rate}",
            "vcn": voice,
            "speed": speed,
            "volume": volume,
            "pitch": pitch,
            "bgs": 0,
            "tte": "UTF8",
            "reg": "0",
            "rdn": "0",
        },
        "data": {
            "status": 2,
            "text": encoded_text.decode("ascii"),
        },
    }

    connection = None
    chunks: list[bytes] = []
    sid: str | None = None
    try:
        connection = websocket.create_connection(auth_url, timeout=timeout)
        connection.send(json.dumps(request, ensure_ascii=False))
        while True:
            message = connection.recv()
            if not message:
                raise MachineVoiceError(
                    "iFLYTEK closed the connection before returning the final audio frame."
                )
            if isinstance(message, bytes):
                message = message.decode("utf-8")
            response = json.loads(message)
            code = int(response.get("code", -1))
            sid = response.get("sid") or sid
            if code != 0:
                detail = response.get("message") or "unknown API error"
                suffix = f" (sid: {sid})" if sid else ""
                raise MachineVoiceError(f"iFLYTEK error {code}: {detail}{suffix}")
            data = response.get("data") or {}
            audio = data.get("audio")
            if audio:
                chunks.append(base64.b64decode(audio))
            if int(data.get("status", -1)) == 2:
                break
    except MachineVoiceError:
        raise
    except Exception as exc:  # websocket-client exposes several exception types
        detail = _redact_xfyun_error(exc, credentials)
        raise MachineVoiceError(f"iFLYTEK request failed: {detail}") from exc
    finally:
        if connection is not None:
            connection.close()

    if not chunks:
        raise MachineVoiceError("iFLYTEK returned no audio data.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"".join(chunks))


def synthesize_jianying(
    *,
    text: str,
    output: Path,
    speaker: str,
    timeout: float,
) -> None:
    """Synthesize an Ogg/Opus file with JianYing's undocumented SAMI route.

    The fixed identifiers mirror JianYing's client request as documented by the
    upstream ``jianying-editor-skill`` project.  No local JianYing files,
    account cookies, or user credentials are read.
    """
    if not text.strip():
        raise MachineVoiceError("Text must not be empty.")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise MachineVoiceError("--timeout must be finite and between 0 and 300.")
    if len(text.encode("utf-8")) >= 8000:
        raise MachineVoiceError(
            "The UTF-8 text is too long for one JianYing request (8000 bytes)."
        )
    if not re.fullmatch(r"[A-Za-z0-9_-]+", speaker):
        raise MachineVoiceError("--speaker contains unsupported characters.")
    if output.suffix.lower() not in {".ogg", ".opus"}:
        raise MachineVoiceError("The jianying output path must end in .ogg or .opus.")

    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MachineVoiceError(
            "The jianying command requires websocket-client. Install "
            "requirements-machine-voice.txt first."
        ) from exc

    request_url = JIANYING_ENDPOINT + "?" + urlencode(
        {
            "device_id": JIANYING_DEFAULT_DEVICE_ID,
            "iid": JIANYING_DEFAULT_IID,
        }
    )
    user_agent = (
        "JianyingPro/5.9.0.11632 (Windows 10.0.19045; "
        f"app_id:{JIANYING_APP_ID}; device_id:{JIANYING_DEFAULT_DEVICE_ID})"
    )
    task_id = f"ai_gen_{os.urandom(4).hex()}"
    request = {
        "app_id": JIANYING_APP_ID,
        "appkey": JIANYING_APP_KEY,
        "event": "StartTask",
        "namespace": "TTS",
        "task_id": task_id,
        "message_id": f"{task_id}_0",
        "payload": json.dumps(
            {
                "text": text,
                "speaker": speaker,
                "audio_config": {
                    "format": "ogg_opus",
                    "sample_rate": 24000,
                    "bit_rate": 64000,
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    finish_request = {
        "appkey": JIANYING_APP_KEY,
        "event": "FinishTask",
        "namespace": "TTS",
    }

    connection = None
    audio = bytearray()
    try:
        connection = websocket.create_connection(
            request_url,
            header=[f"User-Agent: {user_agent}"],
            timeout=timeout,
        )
        connection.send(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
        connection.send(json.dumps(finish_request, separators=(",", ":")))
        while True:
            message = connection.recv()
            if not message:
                raise MachineVoiceError(
                    "JianYing closed the connection before finishing the audio."
                )
            if isinstance(message, bytes):
                audio.extend(message)
                if len(audio) > JIANYING_MAX_AUDIO_BYTES:
                    raise MachineVoiceError("JianYing returned more than 50 MiB of audio.")
                continue
            response = json.loads(message)
            event = response.get("event")
            if event == "TaskFailed":
                code = response.get("status_code", "unknown")
                detail = response.get("status_text") or "unknown API error"
                raise MachineVoiceError(f"JianYing error {code}: {detail}")
            if event == "TaskFinished":
                break
    except MachineVoiceError:
        raise
    except Exception as exc:  # websocket-client exposes several exception types
        detail = (str(exc) or type(exc).__name__)[:1000]
        raise MachineVoiceError(f"JianYing request failed: {detail}") from exc
    finally:
        if connection is not None:
            connection.close()

    if not audio:
        raise MachineVoiceError("JianYing returned no audio data.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio)


def _require_command(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise MachineVoiceError(f"Required command was not found: {name}")
    return executable


def synthesize_macos_say(*, text: str, output: Path, voice: str, rate: int) -> None:
    if not text.strip():
        raise MachineVoiceError("Text must not be empty.")
    say = _require_command("say")
    ffmpeg = _require_command("ffmpeg")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="machine_voice_", suffix=".aiff", dir=output.parent, delete=False
    ) as handle:
        aiff = Path(handle.name)
    try:
        subprocess.run(
            [say, "-v", voice, "-r", str(rate), "-o", str(aiff), text],
            check=True,
        )
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(aiff),
                str(output),
            ],
            check=True,
        )
    finally:
        aiff.unlink(missing_ok=True)


def _probe_sample_rate(path: Path) -> int:
    ffprobe = _require_command("ffprobe")
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        return int(result.stdout.strip())
    except ValueError as exc:
        raise MachineVoiceError(f"Could not determine sample rate for {path}.") from exc


def _atempo_filters(factor: float) -> list[str]:
    if not math.isfinite(factor) or factor <= 0:
        raise MachineVoiceError("Tempo must be finite and greater than zero.")
    filters: list[str] = []
    while factor < 0.5:
        filters.append("atempo=0.5")
        factor /= 0.5
    while factor > 2.0:
        filters.append("atempo=2.0")
        factor /= 2.0
    if not math.isclose(factor, 1.0, rel_tol=1e-9, abs_tol=1e-9):
        filters.append(f"atempo={factor:.8f}")
    return filters


def process_audio(
    *,
    source: Path,
    output: Path,
    pitch_semitones: float,
    tempo: float,
    highpass: int,
    lowpass: int,
    crush_bits: float,
    crush_mix: float,
    carrier_hz: float,
    carrier_mix: float,
) -> None:
    if not source.is_file():
        raise MachineVoiceError(f"Input audio does not exist: {source}")
    if source.resolve() == output.resolve():
        raise MachineVoiceError("Input and output audio paths must be different.")
    if not math.isfinite(pitch_semitones) or not -24 <= pitch_semitones <= 24:
        raise MachineVoiceError("--pitch-semitones must be finite and between -24 and 24.")
    if not math.isfinite(tempo) or not 0.25 <= tempo <= 4:
        raise MachineVoiceError("--tempo must be finite and between 0.25 and 4.")
    if highpass < 0 or lowpass < 0:
        raise MachineVoiceError("--highpass and --lowpass must not be negative.")
    if lowpass and highpass and lowpass <= highpass:
        raise MachineVoiceError("--lowpass must be greater than --highpass.")
    if not math.isfinite(crush_bits) or not 1 <= crush_bits <= 64:
        raise MachineVoiceError("--crush-bits must be finite and between 1 and 64.")
    if not math.isfinite(crush_mix) or not 0 <= crush_mix <= 1:
        raise MachineVoiceError("--crush-mix must be between 0 and 1.")
    if not math.isfinite(carrier_hz) or carrier_hz < 0:
        raise MachineVoiceError("--carrier-hz must be finite and not negative.")
    if not math.isfinite(carrier_mix) or not 0 <= carrier_mix <= 1:
        raise MachineVoiceError("--carrier-mix must be between 0 and 1.")

    ffmpeg = _require_command("ffmpeg")
    sample_rate = _probe_sample_rate(source)
    if highpass >= sample_rate / 2 or lowpass >= sample_rate / 2:
        raise MachineVoiceError(
            "--highpass and --lowpass must be below the input Nyquist frequency."
        )
    if carrier_hz >= sample_rate / 2:
        raise MachineVoiceError("--carrier-hz must be below the input Nyquist frequency.")
    pitch_factor = 2 ** (pitch_semitones / 12)
    filters = [
        "aformat=channel_layouts=mono",
        f"asetrate={sample_rate * pitch_factor:.8f}",
        f"aresample={sample_rate}",
        *_atempo_filters(tempo / pitch_factor),
    ]
    if highpass:
        filters.append(f"highpass=f={highpass}")
    if lowpass:
        filters.append(f"lowpass=f={lowpass}")
    filters.append(
        "acompressor=threshold=0.125:ratio=3:attack=5:release=80:makeup=1.5"
    )
    if crush_mix:
        filters.append(
            f"acrusher=bits={crush_bits:.3f}:mix={crush_mix:.3f}:mode=lin:aa=0.7"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
    ]
    if carrier_hz and carrier_mix:
        dry_mix = 1 - carrier_mix
        complex_filter = (
            f"[0:a]{','.join(filters)}[base];"
            "[base]asplit=2[dry][wet];"
            f"[wet]aeval=exprs=val(0)*sin(2*PI*{carrier_hz:.6f}*t):c=mono[mod];"
            f"[dry][mod]amix=inputs=2:weights={dry_mix:.6f} {carrier_mix:.6f}:"
            "normalize=0,loudnorm=I=-16:TP=-1.5:LRA=7[out]"
        )
        command.extend(["-filter_complex", complex_filter, "-map", "[out]"])
    else:
        filters.append("loudnorm=I=-16:TP=-1.5:LRA=7")
        command.extend(["-af", ",".join(filters)])
    command.extend(["-ar", str(sample_rate), str(output)])
    subprocess.run(command, check=True)


def _add_text_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--text", help="Text to synthesize.")
    group.add_argument("--text-file", type=Path, help="UTF-8 text file to synthesize.")


def _read_text(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    return args.text_file.read_text(encoding="utf-8-sig")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Low-compute JianYing/iFLYTEK/macOS TTS and audio processing."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    jianying = commands.add_parser(
        "jianying",
        help="Synthesize Ogg/Opus through JianYing's experimental SAMI route.",
    )
    _add_text_arguments(jianying)
    jianying.add_argument("--output", type=Path, required=True)
    jianying.add_argument("--speaker", default=JIANYING_DEFAULT_SPEAKER)
    jianying.add_argument("--timeout", type=float, default=30.0)

    xfyun = commands.add_parser("xfyun", help="Synthesize MP3 through iFLYTEK.")
    _add_text_arguments(xfyun)
    xfyun.add_argument("--output", type=Path, required=True)
    xfyun.add_argument("--credentials", type=Path, default=DEFAULT_CREDENTIALS)
    xfyun.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    xfyun.add_argument("--voice", default="xiaoyan")
    xfyun.add_argument("--speed", type=int, choices=range(0, 101), default=50)
    xfyun.add_argument("--pitch", type=int, choices=range(0, 101), default=50)
    xfyun.add_argument("--volume", type=int, choices=range(0, 101), default=50)
    xfyun.add_argument("--sample-rate", type=int, choices=(8000, 16000), default=16000)
    xfyun.add_argument("--timeout", type=float, default=30.0)

    say = commands.add_parser("say", help="Synthesize with macOS say.")
    _add_text_arguments(say)
    say.add_argument("--output", type=Path, required=True)
    say.add_argument("--voice", default="Tingting")
    say.add_argument("--rate", type=int, default=278)

    process = commands.add_parser(
        "process", help="Apply lightweight FFmpeg machine-voice effects."
    )
    process.add_argument("--input", type=Path, required=True)
    process.add_argument("--output", type=Path, required=True)
    process.add_argument("--pitch-semitones", type=float, default=0.0)
    process.add_argument("--tempo", type=float, default=1.0)
    process.add_argument("--highpass", type=int, default=160)
    process.add_argument("--lowpass", type=int, default=5000)
    process.add_argument("--crush-bits", type=float, default=12.0)
    process.add_argument("--crush-mix", type=float, default=0.06)
    process.add_argument(
        "--carrier-hz",
        type=float,
        default=0.0,
        help="Optional ring-modulation carrier frequency; 480 approximates reference 1.",
    )
    process.add_argument(
        "--carrier-mix",
        type=float,
        default=0.0,
        help="Wet ring-modulation mix from 0 to 1.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "jianying":
            synthesize_jianying(
                text=_read_text(args),
                output=args.output,
                speaker=args.speaker,
                timeout=args.timeout,
            )
        elif args.command == "xfyun":
            if args.output.suffix.lower() != ".mp3":
                raise MachineVoiceError("The xfyun output path must end in .mp3.")
            synthesize_xfyun(
                text=_read_text(args),
                output=args.output,
                credentials_path=args.credentials,
                endpoint=args.endpoint,
                voice=args.voice,
                speed=args.speed,
                pitch=args.pitch,
                volume=args.volume,
                sample_rate=args.sample_rate,
                timeout=args.timeout,
            )
        elif args.command == "say":
            synthesize_macos_say(
                text=_read_text(args),
                output=args.output,
                voice=args.voice,
                rate=args.rate,
            )
        else:
            process_audio(
                source=args.input,
                output=args.output,
                pitch_semitones=args.pitch_semitones,
                tempo=args.tempo,
                highpass=args.highpass,
                lowpass=args.lowpass,
                crush_bits=args.crush_bits,
                crush_mix=args.crush_mix,
                carrier_hz=args.carrier_hz,
                carrier_mix=args.carrier_mix,
            )
    except (
        MachineVoiceError,
        OSError,
        UnicodeError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
