"""Isolated behavior tests against the supplied, unmodified Frigate source."""

import ast
import asyncio
import os
import pathlib
import tempfile
import unittest

HERE = pathlib.Path(__file__).parent
DEFAULT_SOURCE = HERE.parent.parent / ".state/video-fixes-20261101/source"


def source_dir():
    configured = os.environ.get("FRIGATE_VIDEO_SOURCE_DIR")
    candidate = pathlib.Path(configured) if configured else DEFAULT_SOURCE
    if not (candidate / "media.py").is_file() or not (candidate / "nginx.conf").is_file():
        raise unittest.SkipTest(
            "sources natives Frigate absentes : fournir FRIGATE_VIDEO_SOURCE_DIR "
            "(media.py et nginx.conf), ou le fixture .state/video-fixes-20261101/source"
        )
    return candidate


class Response:
    def __init__(self, content=None, status_code=200, **kwargs):
        self.content = content
        self.status_code = status_code
        self.kwargs = kwargs


class Event:
    id = object()

    def __init__(self, camera="front"):
        self.camera = camera
        self.end_time = 1

    @classmethod
    def get(cls, _query):
        if cls.missing:
            raise DoesNotExist()
        return cls()


class DoesNotExist(Exception):
    pass


class Extension:
    value = "jpg"
    jpg = jpeg = webp = "jpg"

    @staticmethod
    def get_mime_type():
        return "image/jpeg"


class FakeCV2:
    IMWRITE_JPEG_QUALITY = 1
    IMWRITE_WEBP_QUALITY = 2
    BORDER_CONSTANT = 0
    error = type("error", (Exception,), {})
    decoded = object()

    @classmethod
    def imdecode(cls, _arr, flags):
        if cls.decoded == "error":
            raise cls.error("bad image")
        return cls.decoded

    @staticmethod
    def copyMakeBorder(*args):
        return args[0]

    @staticmethod
    def imencode(*args):
        return True, Encoded(b"encoded-jpeg")


class Encoded(bytes):
    def tobytes(self):
        return bytes(self)


def make_handler(source_path):
    tree = ast.parse(source_path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "event_thumbnail")
    node.decorator_list = []
    node.returns = None
    node.args.args = node.args.args[:3]
    for arg in node.args.args:
        arg.annotation = None
    node.args.defaults = []
    node.args.kwonlyargs = []
    node.args.kw_defaults = []
    node.args.args[0].arg = "request"
    node.args.args[1].arg = "event_id"
    node.args.args[2].arg = "extension"

    class Numpy:
        uint8 = object()

        @staticmethod
        def frombuffer(buf, dtype=None):
            return buf

    globals_ = {
        "DoesNotExist": DoesNotExist,
        "Event": Event,
        "Extension": Extension,
        "Query": lambda **kwargs: None,
        "JSONResponse": Response,
        "Response": Response,
        "np": Numpy,
        "cv2": FakeCV2,
        "require_camera_access": require_camera_access,
        "get_event_thumbnail_bytes": lambda event: get_thumbnail(event),
        "_resolve_cache_age": lambda age: age,
        "max_cache_age": 2592000,
        "format": "ios",
    }
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(source_path), "exec"), globals_)
    return globals_["event_thumbnail"]


async def require_camera_access(camera, request=None):
    if getattr(request, "denied", False):
        raise PermissionError(camera)


def get_thumbnail(_event):
    return get_thumbnail.value


class PatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = source_dir()

    def test_patch_and_idempotence(self):
        with tempfile.TemporaryDirectory() as temp:
            media = pathlib.Path(temp) / "media.py"
            nginx = pathlib.Path(temp) / "nginx.conf"
            media.write_bytes((self.source / "media.py").read_bytes())
            nginx.write_bytes((self.source / "nginx.conf").read_bytes())
            import patch

            patch.patch_media(media)
            patch.patch_nginx(nginx)
            result = (media.read_bytes(), nginx.read_bytes())
            patch.patch_media(media)
            patch.patch_nginx(nginx)
            self.assertEqual(result, (media.read_bytes(), nginx.read_bytes()))
            self.assertIn(b"map $uri $go2rtc_stream_proxy", nginx.read_bytes())
            self.assertEqual(nginx.read_bytes().count(b"set $go2rtc_stream_proxy 1;"), 3)
            self.assertLess(
                nginx.read_bytes().index(b"map_hash_bucket_size 256;"),
                nginx.read_bytes().index(b"map $uri $go2rtc_stream_proxy"),
            )
            compile(media.read_text(), str(media), "exec")

    def test_unknown_source_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "media.py"
            path.write_text(
                "if not thumbnail_bytes:\n"
                "except cv2.error:\n"
                "# changed source with copied patch markers\n"
            )
            import patch

            with self.assertRaises(ValueError):
                patch.patch_media(path)

            nginx = pathlib.Path(temp) / "nginx.conf"
            nginx.write_text(
                "http {\n    map $uri $go2rtc_stream_proxy {\n"
                "        default 0;\n    }\n}\n"
            )
            with self.assertRaises(ValueError):
                patch.patch_nginx(nginx)

    def test_thumbnail_cases(self):
        with tempfile.TemporaryDirectory() as temp:
            import patch

            media = pathlib.Path(temp) / "media.py"
            media.write_bytes((self.source / "media.py").read_bytes())
            patch.patch_media(media)
            handler = make_handler(media)

            class Request:
                denied = False

            request = Request()
            Event.missing = False
            request.app = type("App", (), {"detected_frames_processor": type("Processor", (), {"camera_states": type("States", (), {"values": lambda self: []})()})()})()

            for value in (None, b""):
                get_thumbnail.value = value
                FakeCV2.decoded = object()
                response = asyncio.run(handler(request, "evt", Extension()))
                self.assertEqual(response.status_code, 404)

            get_thumbnail.value = b"corrupt"
            FakeCV2.decoded = "error"
            response = asyncio.run(handler(request, "evt", Extension()))
            self.assertEqual(response.status_code, 404)

            FakeCV2.decoded = None
            response = asyncio.run(handler(request, "evt", Extension()))
            self.assertEqual(response.status_code, 404)

            get_thumbnail.value = b"valid"
            FakeCV2.decoded = object()
            response = asyncio.run(handler(request, "evt", Extension()))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"encoded-jpeg")
            self.assertEqual(response.kwargs["headers"]["Cache-Control"], "private, max-age=2592000")

            request.denied = True
            with self.assertRaises(PermissionError):
                asyncio.run(handler(request, "evt", Extension()))

    def test_empty_event_falls_back_to_tracked_object(self):
        with tempfile.TemporaryDirectory() as temp:
            import patch

            media = pathlib.Path(temp) / "media.py"
            media.write_bytes((self.source / "media.py").read_bytes())
            patch.patch_media(media)
            handler = make_handler(media)

            class Tracked:
                def get_thumbnail(self, _extension):
                    return b"tracked"

            camera_state = type("CameraState", (), {"name": "front", "tracked_objects": {"evt": Tracked()}})()
            request = type("Request", (), {"denied": False, "app": type("App", (), {"detected_frames_processor": type("P", (), {"camera_states": type("S", (), {"values": lambda self: [camera_state]})()})()})()})()
            Event.missing = False
            get_thumbnail.value = b""
            FakeCV2.decoded = object()
            response = asyncio.run(handler(request, "evt", Extension()))
            self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
