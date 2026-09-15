"""Low-frequency acoustic and seismic surface-wave tagging."""
from __future__ import annotations
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
import numpy as np

class InfrasoundSeismicTagger:
    """Feature-based tagger for 0.1--100 Hz audio and phone motion streams."""
    name = "infrasound-seismic"
    version = "fft-features-1"
    wants_time = False
    verified = {"ok": True, "model_sha256": "builtin-infrasound-features-1", "model_bytes": 0, "problems": []}

    def __init__(self, analysis_rate_hz: float = 400.0, low_hz: float = 0.1, high_hz: float = 100.0) -> None:
        self.analysis_rate_hz, self.low_hz, self.high_hz = float(analysis_rate_hz), float(low_hz), float(high_hz)
        if not np.isfinite(self.analysis_rate_hz) or self.analysis_rate_hz <= 0 or not (0 < self.low_hz < self.high_hz):
            raise ValueError("invalid infrasound analysis parameters")

    @staticmethod
    def _signal(signal: Any, fs_hz: Optional[float]) -> Tuple[np.ndarray, float, str]:
        source = "audio"
        if isinstance(signal, Mapping):
            for key in ("imu_z", "accel_mag", "audio", "samples", "values", "data"):
                if key in signal:
                    source = "imu" if key in ("imu_z", "accel_mag") else "audio"
                    if fs_hz is None: fs_hz = signal.get("fs_hz") or signal.get("sample_rate")
                    signal = signal[key]; break
        if fs_hz is None: raise ValueError("fs_hz is required for infrasound tagging")
        fs = float(fs_hz)
        if not np.isfinite(fs) or fs <= 0: raise ValueError("fs_hz must be positive and finite")
        return np.nan_to_num(np.asarray(signal, dtype=float).reshape(-1), nan=0, posinf=0, neginf=0), fs, source

    @staticmethod
    def _resample(x: np.ndarray, fs: float, target: float) -> np.ndarray:
        if x.size < 2 or abs(fs-target) < 1e-9: return x.copy()
        n=max(2, int(round(x.size*target/fs)))
        return np.interp(np.linspace(0, x.size-1, n), np.arange(x.size), x)

    def _features(self, x: np.ndarray, fs: float, source: str) -> Dict[str, Any]:
        if x.size < 2:
            return {"source":source,"duration_s":x.size/fs,"rms":0.0,"psd":{},"spectral_slope":0.0,"band_energy_ratios":{},"microbarom_peak_hz":None,"microbarom_peak_ratio":0.0,"surface_wave_dispersion":0.0}
        y=x-float(np.mean(x)); freqs=np.fft.rfftfreq(max(256,2**int(np.ceil(np.log2(y.size)))),1/fs)
        nfft=len(freqs)*2-2; spec=np.abs(np.fft.rfft(y*np.hanning(y.size),n=nfft))**2
        valid=(freqs>=self.low_hz)&(freqs<=min(self.high_hz,fs/2)); total=float(np.sum(spec[valid]))+1e-18
        def band(lo,hi): return float(np.sum(spec[valid&(freqs>=lo)&(freqs<hi)]))/total
        bands={"0.1_0.5_hz":band(.1,.5),"0.5_2_hz":band(.5,2),"2_10_hz":band(2,10),"10_30_hz":band(10,30),"30_100_hz":band(30,100)}
        fit=valid&(spec>max(float(np.max(spec[valid]))*1e-8,1e-18)) if np.any(valid) else valid
        slope=float(np.polyfit(np.log10(freqs[fit]),np.log10(spec[fit]),1)[0]) if np.count_nonzero(fit)>=2 else 0.0
        mb=(freqs>=.1)&(freqs<=.5)&valid; peak_hz=None; peak_ratio=0.0
        if np.any(mb):
            idx=np.flatnonzero(mb)[int(np.argmax(spec[mb]))]; peak_hz=float(freqs[idx]); peak_ratio=float(spec[idx]/(np.median(spec[valid])+1e-18))
        centroid=float(np.sum(freqs[valid]*spec[valid])/total) if np.any(valid) else 0.0
        dispersion=float(np.clip((bands["0.5_2_hz"]+bands["2_10_hz"])*(1+max(0,-slope)/8),0,1))
        return {"source":source,"duration_s":x.size/fs,"rms":float(np.sqrt(np.mean(y*y))),"psd":{"frequencies_hz":freqs[valid].tolist(),"power":spec[valid].tolist()},"spectral_slope":slope,"spectral_centroid_hz":centroid,"band_energy_ratios":bands,"microbarom_peak_hz":peak_hz,"microbarom_peak_ratio":peak_ratio,"surface_wave_dispersion":dispersion}

    def tag(self, signal: Any, fs_hz: Optional[float]=None, *, imu_z: Optional[Sequence[float]]=None, accel_mag: Optional[Sequence[float]]=None, floor: float=0.0) -> Dict[str, Any]:
        forced="imu" if imu_z is not None or accel_mag is not None else None; signal=imu_z if imu_z is not None else accel_mag if accel_mag is not None else signal
        x, source_fs, inferred=self._signal(signal, fs_hz); source=forced or inferred
        fs=min(self.analysis_rate_hz,max(2.0,source_fs)); features=self._features(self._resample(x,source_fs,fs),fs,source)
        if x.size < 2: return {"scores":{},"confidence":{},"feature_metrics":features,"max_unstored_score":0.0,"n_classes_scored":0,"n_passes":1,"embedding":[],"embedding_dim":0}
        b=features["band_energy_ratios"]; rms=features["rms"]; crest=float(np.max(np.abs(x-np.mean(x)))/(rms+1e-12)); pr=features["microbarom_peak_ratio"]
        scores={"infrasound.microbarom":float(np.clip((pr-3)/12,0,1)),"infrasound.footstep_thump":float(np.clip((crest-3)/8*(b["2_10_hz"]+b["10_30_hz"]),0,1)),"infrasound.vehicle_rumble":float(np.clip(2*b["2_10_hz"]+b["10_30_hz"]-.15,0,1)),"infrasound.machinery":float(np.clip(1.5*b["10_30_hz"]+b["30_100_hz"]-.2,0,1)),"infrasound.wind_buffet":float(np.clip(b["0.1_0.5_hz"]+b["0.5_2_hz"]-.25,0,1)),"seismic.rayleigh_wave":float(np.clip(features["surface_wave_dispersion"] if source=="imu" else 0,0,1))}
        scores={k:v for k,v in scores.items() if v>=float(floor)}; features.update({"crest_factor":crest,"source_fs_hz":source_fs,"analysis_fs_hz":fs,"dc_offset":float(np.mean(x))})
        return {"scores":scores,"confidence":dict(scores),"feature_metrics":features,"max_unstored_score":0.0,"n_classes_scored":len(scores),"n_passes":1,"embedding":[],"embedding_dim":0}

    process=tag
    classify=tag
