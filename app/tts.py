import asyncio

import edge_tts

VOICES = {
    "hi-IN-MadhurNeural": "Hindi (male)",
    "hi-IN-SwaraNeural": "Hindi (female)",
    "en-IN-NeerjaNeural": "English-India (female)",
    "en-US-GuyNeural": "English-US (male)",
    "en-US-JennyNeural": "English-US (female)",
}


async def _go(text: str, voice: str, path: str):
    await edge_tts.Communicate(text, voice).save(path)


def synth(text: str, voice: str, path: str):
    """Free neural TTS (edge-tts). Must be called from a thread without a running event loop."""
    asyncio.run(_go(text, voice if voice in VOICES else "hi-IN-MadhurNeural", path))
