"""Rayleigh-wave dispersion and two-layer pavement inversion."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping
import numpy as np
from scipy.optimize import least_squares

@dataclass(frozen=True)
class DispersionCurve:
    frequencies_hz: np.ndarray
    phase_velocity_mps: np.ndarray
    group_velocity_mps: np.ndarray
    quality_factor: np.ndarray
    attenuation_db_per_m: np.ndarray
    def as_dict(self):
        return self.__dict__.copy()

class RayleighDispersionInverter:
    """Engineering two-layer Rayleigh dispersion model for asphalt over soil."""
    def __init__(self, h=0.08, E_asphalt=4e9, nu_asphalt=.35, rho_asphalt=2400.,
                 E_soil=1e8, nu_soil=.30, rho_soil=1800., Q_asphalt=50., Q_soil=20.):
        self.h=self._positive(h,'h'); self.E_asphalt=self._positive(E_asphalt,'E_asphalt')
        self.nu_asphalt=self._poisson(nu_asphalt,'nu_asphalt'); self.rho_asphalt=self._positive(rho_asphalt,'rho_asphalt')
        self.E_soil=self._positive(E_soil,'E_soil'); self.nu_soil=self._poisson(nu_soil,'nu_soil'); self.rho_soil=self._positive(rho_soil,'rho_soil')
        self.Q_asphalt=self._positive(Q_asphalt,'Q_asphalt'); self.Q_soil=self._positive(Q_soil,'Q_soil')
    @staticmethod
    def _positive(x,n):
        x=float(x)
        if not np.isfinite(x) or x<=0: raise ValueError(f'{n} must be finite and positive')
        return x
    @staticmethod
    def _poisson(x,n):
        x=float(x)
        if not np.isfinite(x) or not -1<x<.5: raise ValueError(f'{n} must be between -1 and 0.5')
        return x
    @staticmethod
    def shear_velocity(E,nu,rho): return float(np.sqrt(E/(2*rho*(1+nu))))
    @staticmethod
    def rayleigh_velocity(vs,nu): return float(vs*(.862+1.14*nu)/(1+nu))
    @property
    def asphalt_vs_mps(self): return self.shear_velocity(self.E_asphalt,self.nu_asphalt,self.rho_asphalt)
    @property
    def soil_vs_mps(self): return self.shear_velocity(self.E_soil,self.nu_soil,self.rho_soil)
    def _phase_velocity(self,f,h,soil_vs):
        ar=self.rayleigh_velocity(self.asphalt_vs_mps,self.nu_asphalt); sr=self.rayleigh_velocity(soil_vs,self.nu_soil)
        p=np.exp(-2*np.pi*h/(sr/f)); w=p/(1+p)
        return ar*(1-w)+sr*w
    def _curve(self,f,h,soil_vs):
        f=np.asarray(f,float)
        if f.ndim!=1 or not f.size or not np.all(np.isfinite(f)) or np.any(f<=0): raise ValueError('frequencies_hz must be a positive one-dimensional sequence')
        o=np.argsort(f); sf=f[o]; phase=self._phase_velocity(sf,h,soil_vs)
        group=phase.copy() if sf.size==1 else phase/(1-sf*np.gradient(phase,sf)/phase)
        p=np.exp(-2*np.pi*h*sf/self.rayleigh_velocity(soil_vs,self.nu_soil)); q=self.Q_asphalt*(1-p)+self.Q_soil*p
        inv=np.argsort(o); return DispersionCurve(f,phase[inv],group[inv],q[inv],(8.686*np.pi*sf/(q*phase))[inv])
    def forward_dispersion(self,frequencies_hz=None):
        f=np.geomspace(5,500,64) if frequencies_hz is None else np.asarray(frequencies_hz,float)
        if np.any((f<5)|(f>500)): raise ValueError('frequencies_hz must lie within 5-500 Hz')
        return self._curve(f,self.h,self.soil_vs_mps).as_dict()
    @staticmethod
    def _pick_arrays(picks,distances,kind):
        rows=list(picks)
        if len(rows)<2: raise ValueError('at least two dispersion picks are required')
        fs=[]; ys=[]; ds=[]
        for i,p in enumerate(rows):
            if isinstance(p,Mapping):
                f=p.get('frequency_hz',p.get('frequency')); d=p.get('distance_m',p.get('distance'))
                if kind=='group': y=p.get('group_velocity_mps',p.get('velocity_mps'))
                elif kind=='phase': y=p.get('phase_velocity_mps',p.get('velocity_mps'))
                elif kind in ('toa','time','phase_delay'): y=p.get('time_s',p.get('delay_s',p.get('phase_delay_s')))
                else: raise ValueError('kind must be phase, group, toa, or phase_delay')
            else:
                if len(p)!=3: raise ValueError('tuple picks must be (frequency_hz, value, distance_m)')
                f,y,d=p
            if d is None and distances is not None: d=distances[i]
            if f is None or y is None or d is None: raise ValueError('each pick needs frequency, observation, and distance')
            fs.append(float(f)); ys.append(float(y)); ds.append(float(d))
        f,y,d=np.asarray(fs),np.asarray(ys),np.asarray(ds)
        if np.any(~np.isfinite(f)) or np.any(f<=0) or np.any(~np.isfinite(y)) or np.any(y<=0) or np.any(~np.isfinite(d)) or np.any(d<=0): raise ValueError('picks must contain finite positive values')
        if kind in ('toa','time','phase_delay'): y=d/y
        return f,y
    def invert(self,picks,distances_m=None,kind='phase',initial_h=None,initial_vs_mps=None):
        f,obs=self._pick_arrays(picks,distances_m,kind); initial=[self.h if initial_h is None else initial_h,self.soil_vs_mps if initial_vs_mps is None else initial_vs_mps]; scale=np.maximum(obs,1.)
        def residual(x):
            c=self._curve(f,x[0],x[1]); pred=c.group_velocity_mps if kind=='group' else c.phase_velocity_mps
            return (pred-obs)/scale
        r=least_squares(residual,initial,bounds=([.005,50],[1,2000])); h,vs=r.x; curve=self._curve(f,h,vs); g=self.rho_soil*vs*vs
        return {'h_m':float(h),'thickness_m':float(h),'V_s_mps':float(vs),'shear_velocity_mps':float(vs),'G_pa':float(g),'shear_modulus_pa':float(g),'success':bool(r.success),'rmse_mps':float(np.sqrt(np.mean((residual(r.x)*scale)**2))),'n_picks':len(f),'dispersion':curve.as_dict()}
    def damping(self,frequencies_hz,h=None,soil_vs=None):
        c=self._curve(frequencies_hz,self.h if h is None else h,self.soil_vs_mps if soil_vs is None else soil_vs)
        return {'frequencies_hz':c.frequencies_hz,'Q':c.quality_factor,'quality_factor':c.quality_factor,'alpha_db_per_m':c.attenuation_db_per_m,'attenuation_db_per_m':c.attenuation_db_per_m}
