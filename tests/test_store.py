"""FileStore: ids, streamed saves with limits, TTL, wipe, decoded-image LRU."""
from __future__ import annotations

import errno
import io
import os
import sys
import threading
import time
import types

import pytest

from app.store import (ID_RE, TOMBSTONE_PREFIX, FileStore, NotFound, StoreError, clean_name, is_valid_id,
                       pixel_bytes)


@pytest.fixture()
def store(tmp_path):
    s = FileStore(tmp_path / "store", ttl=60, cache_items=2)
    s.init()
    return s


def test_save_get_roundtrip(store):
    saved = store.save("IMG_0001.HEIC", io.BytesIO(b"x" * 3000), max_bytes=10_000)
    assert ID_RE.fullmatch(saved.id)
    assert saved.size_bytes == 3000 and saved.name == "IMG_0001.HEIC"
    assert saved.path.read_bytes() == b"x" * 3000
    got = store.get(saved.id)
    assert got.name == "IMG_0001.HEIC" and got.size_bytes == 3000 and got.path == saved.path
    assert store.exists(saved.id) and store.ids() == [saved.id]


def test_save_streams_in_chunks(store, monkeypatch):
    monkeypatch.setattr("app.store.CHUNK_SIZE", 7)
    reads: list[int] = []

    class Reader(io.BytesIO):
        def read(self, n=-1):
            reads.append(n)
            return super().read(n)

    saved = store.save("a.jpg", Reader(b"0123456789" * 5), max_bytes=1000)
    assert saved.size_bytes == 50
    assert all(n == 7 for n in reads)


def test_oversize_is_413_and_partial_removed(store, monkeypatch):
    monkeypatch.setattr("app.store.CHUNK_SIZE", 10)
    with pytest.raises(StoreError) as info:
        store.save("big.jpg", io.BytesIO(b"y" * 101), max_bytes=100)
    assert info.value.status == 413
    assert "limit" in info.value.detail
    assert store.ids() == []
    assert list(store.root.iterdir()) == []


def test_exact_limit_is_accepted(store):
    assert store.save("ok.jpg", io.BytesIO(b"z" * 100), max_bytes=100).size_bytes == 100


def test_empty_file_is_400(store):
    with pytest.raises(StoreError) as info:
        store.save("empty.png", io.BytesIO(b""), max_bytes=100)
    assert info.value.status == 400
    assert store.ids() == []


def test_disk_full_is_507(store, monkeypatch):
    class Full(io.BytesIO):
        def read(self, n=-1):
            raise OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(StoreError) as info:
        store.save("a.jpg", Full(), max_bytes=100)
    assert info.value.status == 507 and info.value.detail == "Server storage full"
    assert store.ids() == []


@pytest.mark.parametrize("bad", ["", "../etc/passwd", "A" * 32, "a" * 31, "a" * 33, "g" * 32,
                                 "..", "/tmp", "a" * 31 + "/", "0" * 31 + "\n"])
def test_invalid_ids_never_touch_filesystem(store, bad):
    assert not is_valid_id(bad)
    with pytest.raises(NotFound):
        store.get(bad)
    with pytest.raises(NotFound):
        store.delete(bad)
    assert not store.exists(bad)


def test_unknown_id_is_not_found(store):
    with pytest.raises(NotFound) as info:
        store.get("0" * 32)
    assert info.value.status == 404


def test_get_touches_access_time(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    meta = store.root / saved.id / "meta.json"
    old = time.time() - 30
    os.utime(meta, (old, old))
    store.get(saved.id)
    assert meta.stat().st_mtime > old + 20


def test_cleanup_expired(store):
    keep = store.save("keep.jpg", io.BytesIO(b"abc"), max_bytes=100)
    gone = store.save("gone.jpg", io.BytesIO(b"abc"), max_bytes=100)
    old = time.time() - 120
    os.utime(store.root / gone.id / "meta.json", (old, old))
    store.cache_put(gone.id, object())  # type: ignore[arg-type]
    assert store.cleanup_expired() == [gone.id]
    assert store.ids() == [keep.id]
    assert store.cache_get(gone.id) is None


def test_expired_get_is_not_found_and_removed(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    old = time.time() - 61
    os.utime(store.root / saved.id / "meta.json", (old, old))
    with pytest.raises(NotFound):
        store.get(saved.id)
    assert not (store.root / saved.id).exists()


def test_delete_removes_files_and_cache(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    store.cache_put(saved.id, object())  # type: ignore[arg-type]
    assert store.delete(saved.id) is True
    assert not (store.root / saved.id).exists()
    assert store.cache_get(saved.id) is None
    assert store.delete(saved.id) is False
    with pytest.raises(NotFound):
        store.get(saved.id)


def test_wipe_only_removes_upload_dirs(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    other = store.root / "keep-me.txt"
    other.write_text("not ours")
    (store.root / "notanid").mkdir()
    assert store.wipe() == 1
    assert not (store.root / saved.id).exists()
    assert other.exists() and (store.root / "notanid").is_dir()


def test_init_wipes_previous_run(tmp_path):
    first = FileStore(tmp_path, ttl=60)
    first.init()
    saved = first.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    second = FileStore(tmp_path, ttl=60)
    second.init()
    assert not second.exists(saved.id)


def test_lru_eviction_order(store):
    a, b, c = object(), object(), object()
    store.cache_put("a" * 32, a)  # type: ignore[arg-type]
    store.cache_put("b" * 32, b)  # type: ignore[arg-type]
    assert store.cache_get("a" * 32) is a  # a becomes most recent
    store.cache_put("c" * 32, c)  # type: ignore[arg-type]
    assert store.cached_ids() == ["a" * 32, "c" * 32]
    assert store.cache_get("b" * 32) is None


def test_cache_disabled(tmp_path):
    s = FileStore(tmp_path, ttl=60, cache_items=0)
    s.cache_put("a" * 32, object())  # type: ignore[arg-type]
    assert s.cache_get("a" * 32) is None


def test_cache_is_thread_safe(store):
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            for i in range(300):
                key = f"{(n * 1000 + i) % 7:032x}"
                store.cache_put(key, object())  # type: ignore[arg-type]
                store.cache_get(key)
                store.evict(key)
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.cached_ids()) <= 2


def test_load_decodes_once_and_caches(store, monkeypatch):
    calls: list[tuple[str, str | None]] = []
    fake = types.SimpleNamespace(load=lambda path, name=None: calls.append((path, name)) or {"decoded": path})
    import app.imaging

    monkeypatch.setitem(sys.modules, "app.imaging.io", fake)
    monkeypatch.setattr(app.imaging, "io", fake, raising=False)
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    first = store.load(saved.id)
    second = store.load(saved.id)
    assert first is second and len(calls) == 1 and calls[0][0].endswith("/source")
    assert calls[0][1] == "a.jpg"  # the upload's name: the decoder's hint for TIFF-based RAW files
    store.delete(saved.id)
    with pytest.raises(NotFound):
        store.load(saved.id)


def test_aux_files(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    assert store.read_aux(saved.id, "preview-240.jpg") is None
    store.write_aux(saved.id, "preview-240.jpg", b"thumb")
    assert store.read_aux(saved.id, "preview-240.jpg") == b"thumb"
    with pytest.raises(ValueError):
        store.write_aux(saved.id, "../escape", b"x")
    with pytest.raises(ValueError):
        store.write_aux(saved.id, "source", b"x")


def test_ttl_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        FileStore(tmp_path, ttl=0)


@pytest.mark.parametrize(("raw", "expected"), [
    ("IMG_0001.HEIC", "IMG_0001.HEIC"),
    ("C:\\fakepath\\photo.jpg", "photo.jpg"),
    ("../../etc/passwd", "passwd"),
    ("bad\x00na\x1fme\u200b.png", "badname.png"),
    ('a<b>c:d"e|f?g*.jpg', "a_b_c_d_e_f_g_.jpg"),
    ("   ..  ", "image"),
    (None, "image"),
    ("CON.jpg", "_CON.jpg"),
    ("été 写真 1.jpg", "été 写真 1.jpg"),
])
def test_clean_name(raw, expected):
    assert clean_name(raw) == expected


def test_clean_name_truncates_keeping_extension():
    name = clean_name("x" * 400 + ".jpeg", max_len=50)
    assert len(name) == 50 and name.endswith(".jpeg")


# ------------------------------------------------------------------ byte budget / eviction
def _decoded(w: int, h: int, mode: str = "RGB") -> types.SimpleNamespace:
    from PIL import Image

    return types.SimpleNamespace(image=Image.new(mode, (w, h)))


def test_lru_is_bounded_by_pixel_bytes(tmp_path):
    s = FileStore(tmp_path, ttl=60, cache_items=6, cache_bytes=4_500_000)
    ids = [f"{i:032x}" for i in range(4)]
    for file_id in ids[:3]:
        s.cache_put(file_id, _decoded(1000, 1000))  # type: ignore[arg-type]  # 4 MB each (RGB is 4 B/px)
    assert s.cached_ids() == [ids[2]] and s.cached_bytes == 4_000_000
    s.cache_put(ids[3], _decoded(10, 10, "RGBA"))  # type: ignore[arg-type]
    assert s.cached_ids() == [ids[2], ids[3]] and s.cached_bytes == 4_000_400


def test_lru_skips_images_larger_than_the_budget(tmp_path):
    """The budget is a real bound — one oversized image is not kept either."""
    s = FileStore(tmp_path, ttl=60, cache_items=6, cache_bytes=50_000)
    s.cache_put("a" * 32, _decoded(100, 100))  # type: ignore[arg-type]  # 40 000 B
    s.cache_put("b" * 32, _decoded(200, 100))  # type: ignore[arg-type]  # 80 000 B > budget
    assert s.cached_ids() == ["a" * 32] and s.cached_bytes == 40_000
    s.cache_put("a" * 32, _decoded(200, 100))  # type: ignore[arg-type]  # replacing with an oversized one drops it
    assert s.cached_ids() == [] and s.cached_bytes == 0


@pytest.mark.parametrize(("mode", "per_pixel"), [("1", 1), ("L", 1), ("P", 1), ("I;16", 2), ("LA", 4),
                                                 ("RGB", 4), ("RGBA", 4), ("CMYK", 4), ("I", 4), ("F", 4)])
def test_pixel_bytes_match_pillow_storage(mode, per_pixel):
    from PIL import Image

    img = Image.new(mode, (30, 20))
    assert pixel_bytes(img) == 600 * per_pixel
    assert pixel_bytes(None) == 0


def test_lru_byte_accounting_on_replace_evict_wipe(tmp_path):
    s = FileStore(tmp_path, ttl=60, cache_items=6, cache_bytes=10**9)
    s.cache_put("a" * 32, _decoded(100, 100))  # type: ignore[arg-type]
    s.cache_put("a" * 32, _decoded(10, 10, "L"))  # type: ignore[arg-type]
    assert s.cached_bytes == 100
    s.cache_put("b" * 32, _decoded(20, 10, "LA"))  # type: ignore[arg-type]
    s.evict("a" * 32)
    assert s.cached_bytes == 800
    s.wipe()
    assert s.cached_bytes == 0 and s.cached_ids() == []


def test_zero_byte_budget_disables_cache(tmp_path):
    s = FileStore(tmp_path, ttl=60, cache_items=6, cache_bytes=0)
    s.cache_put("a" * 32, _decoded(4, 4))  # type: ignore[arg-type]
    assert s.cached_ids() == []


def test_delete_expiry_and_wipe_drop_the_decoded_images(tmp_path):
    s = FileStore(tmp_path / "s", ttl=60)
    s.init()
    a, b, c, d = (s.save(f"{n}.jpg", io.BytesIO(b"abc"), max_bytes=100) for n in "abcd")
    for saved in (a, b, c, d):
        s.cache_put(saved.id, _decoded(10, 10))  # type: ignore[arg-type]
    s.delete(a.id)
    old = time.time() - 120
    os.utime(s.root / b.id / "meta.json", (old, old))
    assert s.cleanup_expired() == [b.id]
    os.utime(s.root / c.id / "meta.json", (old, old))
    with pytest.raises(NotFound):
        s.get(c.id)  # expired on access
    assert s.cached_ids() == [d.id]
    s.wipe()
    assert s.cached_ids() == [] and s.cached_bytes == 0


# ------------------------------------------------------------------ races and long jobs
def test_delete_wins_against_a_racing_aux_write(store, monkeypatch):
    """A thumbnail written while the directory is being removed must not survive the DELETE.

    The write is injected at the worst moment: after rmtree emptied the directory, before its rmdir.
    """
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    real_rmdir = os.rmdir

    def racing_rmdir(path, *args, **kwargs):
        store.write_aux(saved.id, "preview-240.jpg", b"late thumbnail")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", racing_rmdir)
    assert store.delete(saved.id) is True
    monkeypatch.undo()
    assert list(store.root.iterdir()) == []
    store.write_aux(saved.id, "preview-240.jpg", b"later")  # writes after the delete are no-ops
    assert list(store.root.iterdir()) == []


def test_leftover_tombstones_are_removed(store):
    tomb = store.root / f"{TOMBSTONE_PREFIX}{'a' * 32}-1234"
    tomb.mkdir()
    (tomb / "preview-240.jpg").write_bytes(b"x")
    assert store.cleanup_expired() == [] and not tomb.exists()
    tomb.mkdir()
    store.init()
    assert not tomb.exists()


def test_pinned_uploads_do_not_expire(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    meta = store.root / saved.id / "meta.json"
    old = time.time() - 120
    os.utime(meta, (old, old))
    with store.pinned([saved.id]), store.pinned([saved.id]):  # pins nest (refcounted)
        assert store.cleanup_expired() == []
        assert store.get(saved.id).id == saved.id
        os.utime(meta, (old, old))
    assert not store.is_pinned(saved.id)
    assert meta.stat().st_mtime > old + 100  # the idle timer restarts when the job ends
    assert store.cleanup_expired() == []
    os.utime(meta, (old, old))
    assert store.cleanup_expired() == [saved.id]


def test_load_does_not_cache_an_upload_deleted_while_decoding(store, monkeypatch):
    import app.imaging

    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)

    def decode_while_deleted(path, name=None):
        store.delete(saved.id)
        return types.SimpleNamespace(image=None)

    fake = types.SimpleNamespace(load=decode_while_deleted)
    monkeypatch.setitem(sys.modules, "app.imaging.io", fake)
    monkeypatch.setattr(app.imaging, "io", fake, raising=False)
    store.load(saved.id)
    assert store.cached_ids() == []


def test_image_info_is_recorded(store):
    saved = store.save("a.jpg", io.BytesIO(b"abc"), max_bytes=100)
    assert (store.get(saved.id).width, store.get(saved.id).format) == (None, None)
    store.set_info(saved.id, width=4032, height=3024, format="heic")
    got = store.get(saved.id)
    assert (got.width, got.height, got.format, got.name) == (4032, 3024, "heic", "a.jpg")
    store.delete(saved.id)
    store.set_info(saved.id, width=1, height=1, format="png")  # gone: silently ignored
    assert list(store.root.iterdir()) == []


# ------------------------------------------------------------------ kinds, results, hard TTL (PLAN.md §2.1)
def test_uploads_default_to_image_kind_and_carry_v2_fields(store):
    saved = store.save("a.pdf", io.BytesIO(b"%PDF-1.4"), max_bytes=100)
    info = store.get(saved.id).info()
    assert {k: info[k] for k in ("kind", "source", "parent_ids", "tool", "pages", "encrypted", "expires_at")} == \
        {"kind": "image", "source": "upload", "parent_ids": [], "tool": None, "pages": None, "encrypted": False,
         "expires_at": None}
    store.set_info(saved.id, kind="pdf", format="pdf", mime="application/pdf", pages=7, encrypted=True,
                   damaged=True)
    got = store.get(saved.id)
    assert (got.kind, got.pages, got.encrypted, got.mime) == ("pdf", 7, True, "application/pdf")
    assert got.info()["damaged"] is True
    with pytest.raises(ValueError, match="unknown file info"):
        store.set_info(saved.id, owner="me")


def test_results_from_bytes_and_paths(store, tmp_path):
    parent = store.save("in.pdf", io.BytesIO(b"x"), max_bytes=10)
    res = store.save_result(b"%PDF-out", "out.pdf", kind="pdf", fmt="pdf", mime="application/pdf",
                            parent_ids=[parent.id, "../evil"], tool="pdf.merge", info={"pages": 2})
    got = store.get(res.id)
    assert got.source == "result" and got.parent_ids == (parent.id,) and got.tool == "pdf.merge"
    assert got.pages == 2 and got.path.read_bytes() == b"%PDF-out"
    src = tmp_path / "work.bin"
    src.write_bytes(b"moved")
    moved = store.save_result(src, "w.bin", kind="other", fmt=None, mime=None)
    assert not src.exists() and store.get(moved.id).path.read_bytes() == b"moved"
    with pytest.raises(StoreError) as empty:
        store.save_result(b"", "e.txt", kind="other", fmt=None, mime=None)
    assert empty.value.status == 422
    with pytest.raises(StoreError) as big:
        store.save_result(b"x" * 20, "b.txt", kind="other", fmt=None, mime=None, max_bytes=10)
    assert big.value.status == 413
    assert len(store.ids()) == 3  # failed results leave nothing behind


def test_hard_ttl_ends_a_file_whatever_its_activity(tmp_path):
    s = FileStore(tmp_path / "s", ttl=3600, hard_ttl=100)
    s.init()
    saved = s.save("a.png", io.BytesIO(b"x"), max_bytes=10)
    assert saved.hard_deadline == pytest.approx(saved.created + 100)
    result = s.save_result(b"r", "r.png", kind="image", fmt="png", mime="image/png",
                           hard_deadline=saved.hard_deadline)
    assert s.get(result.id).hard_deadline == saved.hard_deadline  # results inherit, never extend
    assert s.cleanup_expired(now=time.time() + 50) == []
    with s.pinned([saved.id]):  # a running job keeps its input until it ends
        assert s.cleanup_expired(now=time.time() + 200) == [result.id]
    assert s.cleanup_expired(now=time.time() + 200) == [saved.id]
    with pytest.raises(ValueError):
        FileStore(tmp_path / "x", ttl=60, hard_ttl=-1)


def test_hard_deadline_is_checked_on_access(tmp_path, monkeypatch):
    s = FileStore(tmp_path / "s", ttl=3600, hard_ttl=10)
    s.init()
    saved = s.save("a.png", io.BytesIO(b"x"), max_bytes=10)
    real = time.time
    monkeypatch.setattr("app.store.time.time", lambda: real() + 60)
    with pytest.raises(NotFound):
        s.get(saved.id)
    assert not s.exists(saved.id)


def test_work_root_is_private_and_wiped(store):
    store.work_root.mkdir()
    (store.work_root / "job-1").mkdir()
    store.wipe()
    assert not store.work_root.exists()
    assert store.work_root.name == ".work" and store.work_root.parent == store.root
