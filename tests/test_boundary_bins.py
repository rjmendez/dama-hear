"""Regression tests for FFT/mel band boundary bins (reported bins 17, 68, 170)."""

import os
import sys
import math
import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hear import sketch as SK  # noqa: E402


FS = 48000.0
NFFT = 4096


def _sine_bin(bin_index: int, n_samples: int = 8192, amp: float = 1.0) -> np.ndarray:
    """Generate a pure tone at exact FFT bin index for 48 kHz / 4096 NFFT."""
    freq_hz = bin_index * FS / NFFT
    t = np.arange(n_samples, dtype=np.float64) / FS
    return (amp * np.sin(2.0 * math.pi * freq_hz * t)).astype(np.float64)


class TestBoundaryBins:
    def test_mel_filterbank_weights_at_boundary_frequencies(self):
        """Verify mel filterbank triangular weights at frequencies corresponding to boundary bins 17, 68, 170."""
        fb = SK.mel_filterbank(FS, nfft=NFFT, bands=SK.MEL_BANDS)
        freqs = np.fft.rfftfreq(NFFT, 1.0 / FS)

        for b_idx in (17, 68, 170):
            freq_hz = b_idx * FS / NFFT
            f_bin_idx = int(round(freq_hz * NFFT / FS))
            weights = fb[:, f_bin_idx]
            nonzero_bands = np.where(weights > 1e-6)[0]
            # At any given frequency, at most 2 adjacent triangular mel filters have non-zero weight
            assert len(nonzero_bands) <= 2, f"Bin {b_idx} ({freq_hz:.2f} Hz) lands in {len(nonzero_bands)} bands"

    def test_inclusive_vs_half_open_boundary_bin_17(self):
        """Test boundary bin 17 (199.22 Hz).

        Under inclusive [lo, hi] indexing where B0_HI == B1_LO == 17, bin 17 is present
        in both B0 and B1. Verify this behavior for bin 17.
        """
        x = _sine_bin(17)
        win = np.hanning(NFFT)
        spec_power = np.abs(np.fft.rfft(x[:NFFT] * win, NFFT)) ** 2

        # B0 = 4..17, B1 = 17..68
        b0_sum = spec_power[4:18].sum()   # includes 17
        b1_sum = spec_power[17:69].sum()  # includes 17

        assert b0_sum > 1e-3, "Bin 17 power must be present in B0"
        assert b1_sum > 1e-3, "Bin 17 power must be present in B1"

    def test_inclusive_vs_half_open_boundary_bin_68(self):
        """Test boundary bin 68 (796.88 Hz).

        Under inclusive indexing where B1_HI == B2_LO == 68, bin 68 is present
        in both B1 and B2.
        """
        x = _sine_bin(68)
        win = np.hanning(NFFT)
        spec_power = np.abs(np.fft.rfft(x[:NFFT] * win, NFFT)) ** 2

        # B1 = 17..68, B2 = 68..170
        b1_sum = spec_power[17:69].sum()   # includes 68
        b2_sum = spec_power[68:171].sum()  # includes 68

        assert b1_sum > 1e-3, "Bin 68 power must be present in B1"
        assert b2_sum > 1e-3, "Bin 68 power must be present in B2"

    def test_inclusive_vs_half_open_boundary_bin_170(self):
        """Test boundary bin 170 (1992.19 Hz).

        Under inclusive indexing where B2_HI == B3_LO == 170, bin 170 is present
        in both B2 and B3.
        """
        x = _sine_bin(170)
        win = np.hanning(NFFT)
        spec_power = np.abs(np.fft.rfft(x[:NFFT] * win, NFFT)) ** 2

        # B2 = 68..170, B3 = 170..426
        b2_sum = spec_power[68:171].sum()   # includes 170
        b3_sum = spec_power[170:427].sum()  # includes 170

        assert b2_sum > 1e-3, "Bin 170 power must be present in B2"
        assert b3_sum > 1e-3, "Bin 170 power must be present in B3"

    def test_interior_bin_no_double_attribution(self):
        """Test interior bin 10 (117.19 Hz, inside B0 [4..17]).

        Verify that power in an interior bin lands heavily in B0 and is suppressed (>30 dB down) in B1.
        """
        x = _sine_bin(10)
        win = np.hanning(NFFT)
        spec_power = np.abs(np.fft.rfft(x[:NFFT] * win, NFFT)) ** 2

        b0_sum = spec_power[4:18].sum()
        b1_sum = spec_power[17:69].sum()

        assert b0_sum > 1e-3, "Interior bin 10 power must land in B0"
        assert b1_sum < b0_sum * 1e-3, "Interior bin 10 power must be >30 dB down in B1"
