"""Sanity checks for the perceptual hashing helpers.

These tests don't need any I/O; they exercise the pure numeric paths.
"""
import numpy as np
import pytest

from image_proc.image_processing import Img_Proc


@pytest.fixture
def proc():
    return Img_Proc.__new__(Img_Proc)


def _ramp(h=16, w=16):
    """A deterministic 2D image in [0,1]."""
    yy, xx = np.mgrid[0:h, 0:w]
    return ((yy + xx) / (h + w - 2)).astype(np.float32)


def test_bits_to_int_roundtrip(proc):
    bits = np.array([[1, 0, 1, 1], [0, 0, 1, 0]], dtype=bool)
    n = proc._bits_to_int(bits)
    # MSB first, row-major: 10110010 = 0xB2 = 178
    assert n == 0b10110010


def test_hamming_distance(proc):
    assert proc._hamming(0b1010, 0b1010) == 0
    assert proc._hamming(0b1010, 0b0101) == 4
    assert proc._hamming(0b1111, 0b0000) == 4


@pytest.mark.parametrize("method", ["phash", "ahash", "dhash"])
def test_identical_inputs_have_zero_distance(proc, method):
    img = _ramp()
    h1 = proc.compute_hash(img, method=method, hash_size=8)
    h2 = proc.compute_hash(img, method=method, hash_size=8)
    assert h1 == h2
    assert proc._hamming(h1, h2) == 0


@pytest.mark.parametrize("method", ["phash", "ahash", "dhash"])
def test_different_inputs_diverge(proc, method):
    a = _ramp()
    b = np.fliplr(_ramp())  # mirrored ramp differs in dhash/ahash structure
    ha = proc.compute_hash(a, method=method, hash_size=8)
    hb = proc.compute_hash(b, method=method, hash_size=8)
    # At least one method-dependent bit should differ for these two patterns.
    assert proc._hamming(ha, hb) > 0


def test_orient_top_left_is_idempotent(proc):
    img = _ramp()
    oriented, _, _ = proc.orient_top_left(img)
    oriented2, _, _ = proc.orient_top_left(oriented)
    np.testing.assert_array_equal(oriented, oriented2)
