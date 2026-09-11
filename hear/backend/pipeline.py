#!/usr/bin/env python3
"""The backend chain: bytes in, geometry out. receive -> decode -> attribute -> associate -> solve
-> publish.

Transport-agnostic BY CONSTRUCTION. It takes a frame as bytes plus the name of the interface it
arrived on, and imports nothing that knows what LoRa, Meshtastic or MQTT is. Changing radio is a
change to the caller, never to this file.

It composes and reimplements nothing: decoding is `hear/wire.py`, attribution is
`hear/backend/survey.py`, grouping is `hear/backend/associate.py`, geometry is `hear/solve/`, and
the speed of sound reaches all of them from `shockwave.sound_speed()`.

⚠️IT DOES NOT CLASSIFY. A module owns the sound (docs/architecture.md:19-20). `source_class` is
the class the CALLER asserts; `classify` is an injected per-event override. All this file does with
it is pick the model, cone or point. Asserting the wrong one is the 67.4 deg error
docs/findings-2026-09-05.md:42-43 measures, and no residual here will reveal it.

⚠️IT REFUSES v1 FRAMES FOR EVENTS. A v1 sketch carries no node id and only microseconds within the
PPS second (hear/sketch.py:84-93), so it can be neither attributed nor absolutely timestamped, and
a one-second mislabel is 343 m and undetectable. v1 frames are counted, never guessed at.

⚠️`ingest()` NEVER RAISES. A radio delivers corruption; a backend that dies on a bad frame takes
the whole array down. Every refusal is a counted reason carrying its byte count.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .. import wire as WR
from ..node import telemetry as TL
from ..solve import point as PT
from ..solve import shockwave as SW
from . import associate as AS
from . import survey as SV


def to_dama_event(ev: Dict, array_id: str = "hear") -> Dict:
    """Shape one solved event as a dama fleet payload.

    Same field convention as hear/node/telemetry.py:98-100, so a hear array is not a special case
    downstream -- it is a node that publishes geometry instead of a thermometer.

    A quantity the chosen model does not have is ABSENT from the payload. Bearing on a point fit
    and easting on a cone fit are not None-valued, they are not quantities; spelling both the same
    way would let a consumer read a missing model as a failed solve.
    """
    sol = ev.get("solution") or {}
    body = {
        "event_id": ev["event_id"],
        "model": ev["model"],
        "source_class": ev["source_class"],
        "n_nodes": ev["n_nodes"],
        "n_equations": ev["n_equations"],
        "contributing_node_ids": list(ev["node_ids"]),
        "rms_residual_ms": sol.get("rms_residual_ms"),
        "residual_is_meaningful": sol.get("residual_is_meaningful"),
        # ⚠️BESIDE residual_is_meaningful BECAUSE IT ANSWERS THE OTHER HALF. That flag says the
        # residual cannot falsify the fit at this node count; this one says the arrivals could
        # not have come from one point source at all, whatever the fit. A consumer that reads
        # only the residual sees nothing wrong with either.
        # ⚠️SUBSCRIPTED, NOT `.get`. It shipped as `.get` and the one caller that actually runs
        # in the cluster -- tools/hear_tdoa.py, which hand-builds this dict -- did not put the
        # key in, so every published payload carried `point_source_possible: null` while
        # associate() had computed True or False for that same event. A field that is always
        # null is worse than an absent one: it reads as "not stated / probably fine". A caller
        # that omits it is now a KeyError here rather than a silent null downstream.
        "point_source_possible": ev["point_source_possible"],
        # The magnitude behind the flag, so False is actionable rather than only alarming.
        "worst_pair_excess_s": ev["worst_pair_excess_s"],
        "sound_speed_mps": sol.get("sound_speed_mps"),
        "note": sol.get("note"),
    }
    for k in ("bearing_deg", "offset_m", "offset_observable", "mach_angle_deg",
              "east_m", "north_m", "position_observable", "range_m"):
        if k in sol:
            body[k] = sol[k]
    return {"node_id": array_id, "ts_utc_ms": int(ev["t0_utc_s"] * 1000),
            "node_type": "hear", "event": body}


class Backend:
    """Frames from any number of interfaces, buffered until `flush()` groups and solves them.

    ingest/flush rather than a callback reactor: WHEN you flush decides which detections can group,
    so the buffer boundary is a decision the caller makes explicitly rather than one hidden in an
    event loop.
    """

    def __init__(self, survey: SV.Survey, temp_c: float = 20.0, v_mps: float = 900.0,
                 source_class: str = "crack",
                 classify: Optional[Callable[[Dict], str]] = None,
                 publish: Optional[Callable[[Dict], None]] = None,
                 margin_s: float = AS.MARGIN_S, min_nodes: int = 3,
                 window_s: Optional[float] = None,
                 array_id: str = "hear") -> None:
        """`window_s` None means computed from the survey's own diameter, which is the only correct
        production setting. It is exposed so a test can prove what a too-wide one costs."""
        self.survey = survey
        self.temp_c = float(temp_c)
        self.v_mps = float(v_mps)
        self.source_class = source_class
        self.classify = classify
        self.publish = publish
        self.margin_s = float(margin_s)
        self.min_nodes = int(min_nodes)
        self.window_s = window_s
        self.array_id = array_id
        self._detections: List[Dict] = []
        self._decode_errors: List[Dict] = []
        self._unknown_nodes: List[Dict] = []
        self._n_frames = 0
        self._n_published = 0

    def ingest(self, frame: bytes, iface: str, rx_utc_s: float) -> Dict:
        """Take one frame off one interface. Never raises.

        Routing is BY LENGTH: 14 B is telemetry (hear/node/telemetry.py:92), anything else goes to
        `wire.decode`. The three frame sizes this repo emits -- 14, 172, 173 -- do not collide.

        ⚠️`rx_utc_s` PICKS THE DAY AND NOTHING ELSE. The wire carries microseconds of day;
        `wire.unwrap_utc` resolves which day against this frame's own receive time. It never enters
        the geometry, so radio latency, retries and mesh hops cannot move a solution.

        ⚠️The telemetry frame carries no node id (hear/node/telemetry.py:52-74), so that path
        publishes under the interface name and says so in `reason`. Closing the hole needs a node
        id in `telemetry.pack`, which this module does not own.
        """
        self._n_frames += 1
        b = bytes(frame)
        if len(b) == TL.wire_size():
            return self._ingest_telemetry(b, iface, rx_utc_s)
        try:
            d = WR.decode(b)
        except Exception as e:                       # a radio delivers arbitrary bytes
            return self._decode_error(b, iface, "decode_error: %s" % e, kind=None)
        if d.get("version") != 2:
            return self._decode_error(b, iface, "v1_frame_has_no_node_id", kind="event")
        node_id = d["node_id"]
        if node_id not in self.survey:
            self._unknown_nodes.append({"iface": iface, "node_id": node_id})
            return {"ok": False, "kind": "event", "reason": "unknown_node",
                    "detection": None, "iface": iface}
        det = {
            "node_id": node_id,
            "seq": d["seq"],
            "t_utc_s": WR.unwrap_utc(d["us_of_day"], rx_utc_s),
            "peak": d["peak"],
            "ref_db": d["ref_db"],
            "retrigger": d["retrigger"],
            "profile_id": d["profile_id"],
            "q": d["q"],
            "iface": iface,
        }
        self._detections.append(det)
        return {"ok": True, "kind": "event", "reason": None, "detection": det, "iface": iface}

    def flush(self) -> Dict:
        """Associate, solve and publish everything buffered, then clear the buffer.

        A solver ValueError becomes `solve_error` on that one event and the flush continues: one
        refused event must not cost the others their answer.
        """
        grouped = AS.associate(self._detections, self.survey, temp_c=self.temp_c,
                               margin_s=self.margin_s, min_nodes=self.min_nodes,
                               window_s=self.window_s)
        events: List[Dict] = []
        for ev in grouped["events"]:
            cls = self.classify(ev) if self.classify is not None else self.source_class
            model = "cone" if cls in PT.CONE_CLASSES else "point"
            # Called WITH ev['node_ids'] so rows pair with ev['arrivals'] by index.
            # The point solver is 3D and takes node height as a distance; the cone solver is not
            # yet, and projecting for it is done HERE and named, rather than by handing both the
            # same silently-flattened array.
            P = self.survey.positions(ev["node_ids"])
            sol: Optional[Dict] = None
            err: Optional[str] = None
            try:
                if model == "cone":
                    sol = SW.solve(P[:, :2], ev["arrivals"], v_mps=self.v_mps, temp_c=self.temp_c)
                else:
                    sol = PT.solve(P, ev["arrivals"], cls, temp_c=self.temp_c)
            except ValueError as e:
                err = str(e)
            r = {
                "event_id": ev["event_id"], "t0_utc_s": ev["t0_utc_s"],
                "node_ids": list(ev["node_ids"]), "arrivals": list(ev["arrivals"]),
                "n_nodes": ev["n_nodes"], "n_equations": ev["n_equations"],
                "span_s": ev["span_s"], "source_class": cls, "model": model,
                # ⚠️CARRIED, NOT RECOMPUTED. associate() admits on d/c + MARGIN_S; this is the
                # zero-margin verdict. Three of the four events the live array delivered on
                # 2026-09-11 were 2.81-9.01 m past any bound it has, and this emitter had no way
                # to say so. A solution fitted to an impossible group is not a source.
                "point_source_possible": ev["point_source_possible"],
                "worst_pair_excess_s": ev["worst_pair_excess_s"],
                "solution": sol, "solve_error": err, "published": None,
            }
            r["published"] = to_dama_event(r, self.array_id)
            self._emit(r["published"])
            events.append(r)
        out = {
            "events": events,
            "rejected": grouped["rejected"],
            "duplicates": grouped["duplicates"],
            "decode_errors": list(self._decode_errors),
            "unknown_nodes": list(self._unknown_nodes),
            "window_s": grouped["window_s"], "margin_s": grouped["margin_s"],
            "sound_speed_mps": grouped["sound_speed_mps"], "diameter_m": grouped["diameter_m"],
            # Still reported, but it now describes ONLY the cone path: the point solver consumes
            # `up` as a real distance, so vertical spread is information there rather than an
            # unmodelled error. A single flag covering both would have been wrong for one of them.
            "vertical_assumption": self.survey.validate_2d_assumption(),
            "n_frames": self._n_frames, "n_published": self._n_published,
        }
        self._detections = []
        self._decode_errors = []
        self._unknown_nodes = []
        self._n_frames = 0
        self._n_published = 0
        return out

    def run(self, frames: Sequence[Tuple[bytes, str, float]]) -> Dict:
        for frame, iface, rx_utc_s in frames:
            self.ingest(frame, iface, rx_utc_s)
        return self.flush()

    def _ingest_telemetry(self, b: bytes, iface: str, rx_utc_s: float) -> Dict:
        try:
            tel = TL.unpack(b)
        except Exception as e:
            return self._decode_error(b, iface, "decode_error: %s" % e, kind="telemetry")
        self._emit(TL.to_dama(node_id=iface, t_utc=rx_utc_s, tel=tel))
        return {"ok": True, "kind": "telemetry", "reason": "node_id_taken_from_iface",
                "detection": None, "iface": iface}

    def _decode_error(self, b: bytes, iface: str, reason: str, kind: Optional[str]) -> Dict:
        self._decode_errors.append({"iface": iface, "n_bytes": len(b), "reason": reason})
        return {"ok": False, "kind": kind, "reason": reason, "detection": None, "iface": iface}

    def _emit(self, payload: Dict) -> None:
        """n_published counts sink calls, not payloads built: with no sink nothing was published."""
        if self.publish is None:
            return
        self._n_published += 1
        self.publish(payload)
