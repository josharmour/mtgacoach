"""Explicit one-time download into the optional runtime's own cache; never run by Coach."""

import argparse

PIN = "749d1c1a46eb10492095d68fbcf55691ccf137cd"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", required=True)
    args = parser.parse_args()
    from huggingface_hub import snapshot_download

    print(
        snapshot_download(
            "ResembleAI/chatterbox-turbo",
            revision=PIN,
            cache_dir=args.cache_dir,
            allow_patterns=["*.safetensors", "*.json", "*.txt", "*.pt", "*.model"],
        )
    )
