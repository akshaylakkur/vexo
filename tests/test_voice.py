import asyncio
import json

import websockets

from steady.voice.player import PcmBuffer, Player
from steady.voice.sentences import SentenceSplitter
from steady.voice.speaker import Speaker
from steady.voice.tts import DeepgramTTS


# ---- sentence splitter ---------------------------------------------------------------------

def feed_all(sp, text, step=3):
    out = []
    for i in range(0, len(text), step):
        out += sp.feed(text[i:i + step])
    return out


def test_sentences_are_cut_as_soon_as_they_end():
    sp = SentenceSplitter()
    out = feed_all(sp, "Hi, it's Steady. How are you feeling? I noticed some shaking")
    assert out == ["Hi, it's Steady.", "How are you feeling?"]
    assert sp.flush() == "I noticed some shaking"
    assert sp.flush() is None


def test_numbers_and_abbreviations_do_not_end_a_sentence():
    sp = SentenceSplitter()
    out = feed_all(sp, "Your tremor was at 5.2 hertz. Dr. Patel gets the report. ")
    assert out == ["Your tremor was at 5.2 hertz.", "Dr. Patel gets the report."]


def test_long_sentence_is_cut_at_a_comma():
    sp = SentenceSplitter(soft_chars=40)
    out = feed_all(sp, "I noticed your hand has been shaking for a couple of minutes, so I wanted to check in with you")
    assert out == ["I noticed your hand has been shaking for a couple of minutes,"]


def test_stale_text_is_released_at_a_word_boundary():
    sp = SentenceSplitter(max_wait_s=0.0)
    sp.feed("One moment while I look that up")
    assert sp.take_stale() == "One moment while I look that"
    assert sp.flush() == "up"


# ---- player buffer -------------------------------------------------------------------------

def test_pcm_buffer_reads_exact_blocks_pads_and_clears():
    b = PcmBuffer()
    b.write(b"\x01\x02" * 100)
    b.write(b"\x03\x04" * 100)
    assert b.read(300) == b"\x01\x02" * 100 + b"\x03\x04" * 50
    assert b.read(200) == b"\x03\x04" * 50 + b"\x00" * 100 and b.underruns == 1
    b.write(b"\x05\x06" * 1000)
    assert b.clear() == 2000 and b.seconds == 0 and b.read(4) == b"\x00" * 4


def test_player_carries_an_odd_byte():
    p = Player()
    p.write(b"\x01\x02\x03")
    p.write(b"\x04")
    assert p.buffer.read(4) == b"\x01\x02\x03\x04"


# ---- TTS protocol against a mock Deepgram --------------------------------------------------

class MockDeepgram:
    """Speak -> buffers text; Flush -> 20 ms of PCM per character, in chunks, then Flushed."""

    def __init__(self, chunk_delay=0.005):
        self.received, self.headers, self.paths = [], [], []
        self.chunk_delay = chunk_delay
        self.closed_first = False

    async def handler(self, ws):
        self.headers.append(ws.request.headers.get("Authorization"))
        self.paths.append(ws.request.path)
        pending, job, seq = "", None, 0

        async def stream(text, n, after):
            if after:                              # pieces are spoken one after the other
                await after
            for _ in range(len(text)):
                await ws.send(b"\x10\x00" * 480)
                await asyncio.sleep(self.chunk_delay)
            await ws.send(json.dumps({"type": "Flushed", "sequence_id": n}))

        async for raw in ws:
            msg = json.loads(raw)
            self.received.append(msg)
            if msg["type"] == "Speak":
                pending += msg["text"]
            elif msg["type"] == "Flush":
                job, pending, seq = asyncio.create_task(stream(pending, seq, job)), "", seq + 1
            elif msg["type"] == "Clear":
                if job:
                    job.cancel()
                    job = None
                await ws.send(json.dumps({"type": "Cleared"}))
            elif msg["type"] == "Close":
                break
            if self.closed_first is None:          # test hook: drop the first connection
                self.closed_first = True
                await ws.close()

    async def __aenter__(self):
        self.server = await websockets.serve(self.handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1/speak"
        return self

    async def __aexit__(self, *a):
        self.server.close()
        await self.server.wait_closed()


class FakePlayer(Player):
    """Player without a sound card: 'plays' by emptying the buffer in real time."""

    def start(self):
        self.device_name = "fake"

    async def drain(self, timeout=60.0):
        while self.buffer.seconds > 0:
            self.buffer.read(960)
            if self.first_audio_at is None:
                self.first_audio_at = 1.0
            await asyncio.sleep(0)

    def close(self):
        pass


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_tts_speaks_flushes_and_keeps_the_key_out_of_the_address():
    async def go():
        async with MockDeepgram() as dg:
            audio = bytearray()
            tts = DeepgramTTS("secret-key", audio.extend, url=dg.url)
            await tts.speak("Hello there.")
            await tts.speak("Second.")
            assert await tts.wait_flushed(5)
            await tts.close()
            return dg, bytes(audio)

    dg, audio = run(go())
    assert [m["type"] for m in dg.received] == ["Speak", "Flush", "Speak", "Flush", "Close"]
    assert dg.received[0]["text"] == "Hello there."
    assert len(audio) == (len("Hello there.") + len("Second.")) * 960
    assert dg.headers == ["Token secret-key"]
    assert "secret-key" not in dg.paths[0] and "model=aura-2-thalia-en" in dg.paths[0] \
        and "encoding=linear16" in dg.paths[0] and "sample_rate=24000" in dg.paths[0]


def test_tts_reconnects_when_the_socket_was_closed():
    async def go():
        async with MockDeepgram() as dg:
            audio = bytearray()
            tts = DeepgramTTS("k", audio.extend, url=dg.url)
            await tts.connect()
            await tts._ws.close()                       # Deepgram idled us out
            await asyncio.sleep(0.05)
            await tts.speak("Back again.")
            assert await tts.wait_flushed(5)
            await tts.close()
            return tts.connects, len(audio)

    connects, n = run(go())
    assert connects == 2 and n == len("Back again.") * 960


def test_speaker_streams_sentences_and_reports_timings():
    async def tokens():
        for w in "Hi, it's Steady. I noticed your hand shaking. How are you feeling?".split(" "):
            yield w + " "
            await asyncio.sleep(0.005)

    async def go():
        async with MockDeepgram() as dg:
            player = FakePlayer()
            sp = Speaker(DeepgramTTS("k", player.write, url=dg.url), player)
            sp._noticed = True
            await sp.start()
            res = await sp.say_stream(tokens())
            await sp.close()
            return dg, res

    dg, res = run(go())
    spoken = [m["text"] for m in dg.received if m["type"] == "Speak"]
    assert spoken == ["Hi, it's Steady.", "I noticed your hand shaking.", "How are you feeling?"]
    assert res.pieces == 3 and not res.interrupted and res.first_byte_ms is not None
    assert abs(res.audio_s - sum(len(s) for s in spoken) * 0.02) < 0.01
    # the first sentence was sent before the model had finished the last one
    kinds = [m["type"] for m in dg.received]
    assert kinds.index("Flush") < len(kinds) - 3


def test_interruption_stops_sound_and_drops_the_rest():
    async def go():
        async with MockDeepgram(chunk_delay=0.02) as dg:
            player = FakePlayer()
            sp = Speaker(DeepgramTTS("k", player.write, url=dg.url), player)
            sp._noticed = True
            await sp.start()
            long_text = "This is a long sentence that keeps going for quite a while. " * 4
            task = asyncio.create_task(sp.say(long_text))
            await asyncio.sleep(0.3)
            assert player.buffer.seconds > 0                # sound is queued
            await sp.stop()
            assert player.buffer.seconds == 0               # silent at once
            res = await task
            await asyncio.sleep(0.1)
            left = player.buffer.seconds                    # nothing trickles in afterwards
            res2 = await sp.say("Okay.")                    # and it can speak again
            await sp.close()
            return dg, res, left, res2

    dg, res, left, res2 = run(go())
    assert res.interrupted and left == 0
    assert "Clear" in [m["type"] for m in dg.received]
    assert res2.pieces == 1 and not res2.interrupted and abs(res2.audio_s - len("Okay.") * 0.02) < 0.01
