#!/usr/bin/env python3
"""Apply the two reviewed Frigate source fixes to an exact local image base."""

from __future__ import annotations

import hashlib
import pathlib
import sys

MEDIA_SHA256 = "5546d3e6abf99bb837caf0e7ac2f2025da55e414279bf620babf29f6a1dcbb5b"
MEDIA_PATCHED_SHA256 = "c777136f04fe5ac788f86507bfeaf49da2a93abf86e67f187ae6d4ed3c9ef3ef"
NGINX_SHA256 = {
    "4471cb4a68503d808418db4c73d861463ccd8c009b4f1d0b9280dcb9f4e97aad",
    "563a6e4b5219b7d3a3b9d6992ebf1f5e1ac27b5be570be903a26fe5697d9a7fd",
}
NGINX_PATCHED_SHA256 = {
    "4a55248802612bc5e19998063018a4ad239e2186fd0b1eafbc22c28bd8b767b7",
    "b6e4345d4b952ae7d718e4de1ea72f458379a1454f675cb689d73e83d499fdf2",
}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def replace_once(data: bytes, old: bytes, new: bytes, filename: str) -> bytes:
    count = data.count(old)
    if count != 1:
        raise ValueError(f"{filename}: attendu une occurrence, trouvé {count}")
    return data.replace(old, new, 1)


def patch_media(path: pathlib.Path) -> None:
    original = path.read_bytes()
    if digest(original) == MEDIA_PATCHED_SHA256:
        return
    if digest(original) != MEDIA_SHA256:
        raise ValueError(f"{path}: empreinte source Frigate inattendue")

    updated = replace_once(
        original,
        b"    if thumbnail_bytes is None:\n        # see if the object is currently being tracked",
        b"    if not thumbnail_bytes:\n        # see if the object is currently being tracked",
        str(path),
    )
    updated = replace_once(
        updated,
        b"    if thumbnail_bytes is None:\n        return JSONResponse(\n            content={\"success\": False, \"message\": \"Event not found\"},",
        b"    if not thumbnail_bytes:\n        return JSONResponse(\n            content={\"success\": False, \"message\": \"Event not found\"},",
        str(path),
    )
    updated = replace_once(
        updated,
        b"    img_as_np = np.frombuffer(thumbnail_bytes, dtype=np.uint8)\n    img = cv2.imdecode(img_as_np, flags=1)\n\n    # android notifications prefer a 2:1 ratio",
        b"    img_as_np = np.frombuffer(thumbnail_bytes, dtype=np.uint8)\n    try:\n        img = cv2.imdecode(img_as_np, flags=1)\n    except cv2.error:\n        img = None\n    if img is None:\n        return JSONResponse(\n            content={\"success\": False, \"message\": \"Event not found\"},\n            status_code=404,\n        )\n\n    # android notifications prefer a 2:1 ratio",
        str(path),
    )
    path.write_bytes(updated)


def patch_nginx(path: pathlib.Path) -> None:
    original = path.read_bytes()
    if digest(original) in NGINX_PATCHED_SHA256:
        return
    if digest(original) not in NGINX_SHA256:
        raise ValueError(f"{path}: empreinte nginx Frigate inattendue")
    updated = replace_once(
        original,
        b"    map_hash_bucket_size 256;\n",
        b"    map_hash_bucket_size 256;\n\n"
        b"    map $uri $go2rtc_stream_proxy {\n        default 0;\n    }\n\n",
        str(path),
    )
    path.write_bytes(updated)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: patch.py /path/media.py /path/nginx.conf")
    patch_media(pathlib.Path(sys.argv[1]))
    patch_nginx(pathlib.Path(sys.argv[2]))


if __name__ == "__main__":
    main()
