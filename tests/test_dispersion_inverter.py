import numpy as np
import pytest
from hear.dispersion import RayleighDispersionInverter

def test_forward_dispersion_and_damping_band():
    inv=RayleighDispersionInverter(h=.10,E_soil=1.2e8)
    out=inv.forward_dispersion()
    assert out["frequencies_hz"][0]==pytest.approx(5)
    assert out["frequencies_hz"][-1]==pytest.approx(500)
    assert len(out["phase_velocity_mps"])==64
    assert np.all(out["phase_velocity_mps"]>0)
    assert np.all(out["group_velocity_mps"]>0)
    assert np.all(out["quality_factor"]>0)
    assert np.all(out["attenuation_db_per_m"]>0)

def test_phase_velocity_inversion_recovers_thickness_vs_and_modulus():
    true=RayleighDispersionInverter(h=.12,E_soil=1.35e8)
    f=np.array([8,15,30,60,120,240,450.])
    c=true.forward_dispersion(f)["phase_velocity_mps"]
    picks=[{"frequency_hz":x,"phase_velocity_mps":y,"distance_m":3.0} for x,y in zip(f,c)]
    got=RayleighDispersionInverter(h=.07,E_soil=8e7).invert(picks)
    assert got["h_m"]==pytest.approx(.12,rel=.03)
    assert got["V_s_mps"]==pytest.approx(true.soil_vs_mps,rel=.03)
    assert got["G_pa"]==pytest.approx(1.35e8/(2*(1+true.nu_soil)),rel=.06)

def test_toa_and_phase_delay_picks_are_supported():
    inv=RayleighDispersionInverter(h=.09,E_soil=1.1e8)
    f=np.array([10,25,55,110,220,400.]); c=inv.forward_dispersion(f)["phase_velocity_mps"]; d=4.0
    picks=[{"frequency_hz":x,"time_s":d/y,"distance_m":d} for x,y in zip(f,c)]
    got=RayleighDispersionInverter(h=.08,E_soil=9e7).invert(picks,kind="toa")
    assert got["h_m"]==pytest.approx(.09,rel=.04)
    delays=[{"frequency_hz":x,"phase_delay_s":d/y,"distance_m":d} for x,y in zip(f,c)]
    assert RayleighDispersionInverter().invert(delays,kind="phase_delay")["success"]

def test_validation_and_damping_formula():
    with pytest.raises(ValueError): RayleighDispersionInverter(h=0)
    inv=RayleighDispersionInverter(Q_soil=10)
    f=np.array([20.,100.]); out=inv.damping(f)
    c=inv.forward_dispersion(f)["phase_velocity_mps"]
    assert out["Q"].shape==(2,)
    assert np.allclose(out["alpha_db_per_m"],8.686*np.pi*f/(out["Q"]*c))
    with pytest.raises(ValueError): inv.forward_dispersion([1.0,10.0])
