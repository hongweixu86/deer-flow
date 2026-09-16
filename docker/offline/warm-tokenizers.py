"""Cache DeerFlow/OpenAI tokenizers, retaining tiktoken's SHA256 validation."""
import requests
import tiktoken
import tiktoken.load


original_read = tiktoken.load.read_file
prefix = "https://openaipublic.blob.core.windows.net/encodings/"
mirror = "https://raw.githubusercontent.com/zurawiki/tiktoken-rs/main/tiktoken-rs/assets/"
allowed = {"cl100k_base.tiktoken", "o200k_base.tiktoken"}


def read_with_fallback(path: str) -> bytes:
    if not path.startswith(prefix) or path[len(prefix):] not in allowed:
        return original_read(path)
    try:
        response = requests.get(path, timeout=(10, 60))
        response.raise_for_status()
    except requests.RequestException:
        name = path[len(prefix):]
        print(f"Using tokenizer mirror for {name}", flush=True)
        response = requests.get(mirror + name, timeout=(10, 60))
        response.raise_for_status()
    # read_file_cached checks these bytes against the expected SHA256 supplied
    # by the official encoding constructor before writing the original-URL cache.
    return response.content


tiktoken.load.read_file = read_with_fallback
for name in ("cl100k_base", "o200k_base"):
    assert tiktoken.get_encoding(name).encode("DeerFlow 离线验证")
    print(f"Tokenizer cached and checksum validated: {name}", flush=True)
