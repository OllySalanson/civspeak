"""Stands in for PyAV in the packaged app. faster-whisper imports PyAV when it loads, but only uses it
to decode audio files; Civspeak hands Whisper its recordings as samples already. Leaving the real
PyAV out saves 67 MB and the GPL x264/x265 codecs it carries."""

CIVSPEAK_STUB = True


def __getattr__(name):
    if name.startswith("__"):
        raise AttributeError(name)  # what hasattr() and the import system ask about
    raise ImportError(f"PyAV is not part of the packaged Civspeak (av.{name} was asked for)")
